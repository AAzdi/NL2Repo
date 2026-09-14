"""One versioned, formatted JSON conversation; raw CLI events are consumed in memory."""

import argparse
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)


def now():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def final_result_error(event):
    if event is None:
        return 'No final result received'
    if event.get('subtype') != 'success' or event.get('is_error'):
        return 'CLI reported a generation error'
    if event.get('api_error_status') or event.get('terminal_reason') == 'api_error':
        return 'CLI reported an API error'
    if event.get('stop_reason') not in ('end_turn', 'stop_sequence'):
        return f"Incomplete final response: stop_reason={event.get('stop_reason')!r}"
    return None


class TrajectoryReader:
    """Aggregate events by message ID/block index, including unfinished blocks.

    refresh() exists for importing old raw files; new runs use consume() on a pipe.
    Fragment lists avoid quadratic string copying for token-by-token output.
    """

    def __init__(self, source, *, stream=None, prompt=None, live=False):
        self.source = Path(source)
        self.stream = stream
        self.live = live
        self.offset = 0
        self.messages = []
        if prompt is not None:
            self.messages.append({'role': 'user', 'content': [{'type': 'text', 'text': prompt}]})
        self.diagnostics = []
        self.session_id = None
        self.model = None
        self.generation_status = None
        self.tools = {}
        self.events = self.progress_events = self.invalid_lines = 0
        self.final_event = None
        self.active_stream = {}
        self.by_id = {}
        self.blocks = {}
        self.fragments = {}
        self.echoed = set()
        self.streamed = set()
        self.last_activity_at = None
        self.last_content_at = None
        self.phase = 'starting'
        self.lock = threading.RLock()

    def message(self, role, key, metadata=None):
        if key is None or key not in self.by_id:
            message = {'role': role, 'content': []}
            self.messages.append(message)
            if key is not None:
                self.by_id[key] = message
        else:
            message = self.by_id[key]
        if metadata:
            message.update(metadata)
        return message

    def content(self, blocks):
        if not isinstance(blocks, list):
            return [{'type': 'text', 'text': blocks}]
        parts = []
        for block in blocks:
            if not isinstance(block, dict):
                parts.append({'type': 'unknown_content', 'content': block})
                continue
            block = dict(block)
            if block.get('type') == 'tool_use':
                self.tools[block.get('id', '')] = block.get('name', 'unknown')
            elif block.get('type') == 'tool_result':
                block['name'] = self.tools.get(block.get('tool_use_id', ''), 'unknown')
            parts.append(block)
        return parts

    def materialize(self, block_key, target):
        for field, fragments in self.fragments.get(block_key, {}).items():
            value = ''.join(fragments)
            if field == 'partial_json':
                try:
                    target['input'] = json.loads(value)
                    target['input_json_valid'] = True
                    target.pop('partial_input', None)
                except ValueError:
                    target['input'] = None
                    target['partial_input'] = value
                    target['input_json_valid'] = False
            else:
                target[field] = value

    def add(self, event):
        with self.lock:
            self._add(event)

    def _add(self, event):
        self.events += 1
        timestamp = event.get('timestamp') or (now() if self.live else None)
        self.last_activity_at = timestamp or self.last_activity_at
        session = event.get('session_id')
        if session:
            self.session_id = session
        kind = event.get('type', 'unknown')
        stream_key = (session, event.get('parent_tool_use_id'))
        if kind == 'system' and event.get('subtype') == 'thinking_tokens':
            self.progress_events += 1
            key = self.active_stream.get(stream_key)
            if key in self.by_id:
                self.by_id[key]['estimated_thinking_tokens'] = event.get('estimated_tokens')
            return
        if kind == 'stream_event':
            data = event.get('event') or {}
            typ = data.get('type')
            if typ == 'message_start':
                raw = data.get('message') or {}
                key = (*stream_key, raw.get('id'))
                self.active_stream[stream_key] = key
                self.streamed.add(key)
                self.message('assistant', key, {
                    'id': raw.get('id'), 'model': raw.get('model'), 'started_at': timestamp,
                    'ttft_ms': event.get('ttft_ms'), 'stream_complete': False,
                    'usage': None, 'usage_source': 'unknown'})
                self.phase = 'responding'
                return
            key = self.active_stream.get(stream_key)
            if key is None:
                return
            message = self.by_id[key]
            block_key = (key, data.get('index'))
            if typ == 'content_block_start':
                block = self.content([data.get('content_block', {})])[0]
                block.update(complete=False, started_at=timestamp)
                self.blocks[block_key] = block
                message['content'].append(block)
            elif typ == 'content_block_delta' and block_key in self.blocks:
                delta = data.get('delta') or {}
                field = {'text_delta': 'text', 'thinking_delta': 'thinking',
                         'input_json_delta': 'partial_json', 'signature_delta': 'signature'}.get(delta.get('type'))
                if field:
                    fragments = self.fragments.setdefault(block_key, {})
                    if field not in fragments:
                        fragments[field] = [self.blocks[block_key].get(field, '')]
                    fragments[field].append(delta.get(field, ''))
                    self.last_content_at = timestamp or self.last_content_at
                    self.phase = {'thinking': 'thinking', 'partial_json': 'tool_arguments'}.get(field, 'responding')
            elif typ == 'content_block_stop' and block_key in self.blocks:
                block = self.blocks[block_key]
                self.materialize(block_key, block)
                self.fragments.pop(block_key, None)
                block.update(complete=True, finished_at=timestamp)
            elif typ == 'message_delta':
                if data.get('usage'):
                    message['usage'] = {**(message.get('usage') or {}), **data['usage']}
                    message['usage_source'] = 'gateway_reported_unverified'
                stop = (data.get('delta') or {}).get('stop_reason')
                if stop is not None:
                    message['stop_reason'] = stop
            elif typ == 'message_stop':
                message.update(stream_complete=True, finished_at=timestamp)
            return
        if kind in ('assistant', 'user'):
            raw = event.get('message') or {}
            key = (*stream_key, raw['id']) if raw.get('id') else None
            metadata = {k: event[k] for k in ('timestamp', 'parent_tool_use_id', 'error', 'is_api_error_message')
                        if event.get(k) is not None}
            metadata.update({k: raw[k] for k in ('id', 'model') if raw.get(k) is not None})
            if key not in self.streamed:
                metadata.update({k: raw[k] for k in ('stop_reason', 'usage') if raw.get(k) is not None})
                if 'usage' in metadata:
                    metadata['usage_source'] = 'cli_reported_unverified'
            message = self.message(kind, key, metadata)
            for block in self.content(raw.get('content', [])):
                # The CLI re-emits each completed stream block as an assistant
                # event, sometimes separated by tool results. Do not duplicate it.
                candidates = [(k, b) for k, b in self.blocks.items()
                              if k[0] == key and k not in self.echoed and b.get('type') == block.get('type')]
                if key in self.streamed and candidates:
                    match = next(((k, b) for k, b in candidates
                                  if block.get('type') != 'tool_use' or b.get('id') == block.get('id')), None)
                    if match:
                        self.echoed.add(match[0])
                        continue
                message['content'].append(block)
            self.last_content_at = timestamp or self.last_content_at
            if kind == 'user':
                self.phase = 'tool_result'
        elif kind == 'result':
            self.final_event = event
            self.phase = 'finished'
        elif kind == 'system' and event.get('subtype') == 'init':
            self.model = event.get('model')
        elif kind == 'system' and event.get('subtype') == 'status':
            self.phase = event.get('status', self.phase)
        else:
            # Retain errors/retries and unknown events, not thousands of progress UUIDs.
            self.diagnostics.append({k: v for k, v in event.items() if k not in ('uuid', 'session_id')})

    def consume(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            event = None
        if isinstance(event, dict):
            self.add(event)
        elif line.strip():
            with self.lock:
                self.invalid_lines += 1
                self.diagnostics.append({'type': 'unparsed_line', 'text': line.decode('utf-8', errors='replace')[:2000]
                                         if isinstance(line, bytes) else line[:2000]})

    def refresh(self, final=False):
        if self.stream is None:
            stream = self.source.open('rb')
            stream.seek(self.offset)
        else:
            chunks, cursor = [], self.offset
            while chunk := os.pread(self.stream.fileno(), 1024 * 1024, cursor):
                chunks.append(chunk)
                cursor += len(chunk)
            stream = io.BytesIO(b''.join(chunks))
        with stream:
            while line := stream.readline():
                if not line.endswith(b'\n') and not final:
                    break
                self.offset += len(line)
                self.consume(line)

    def snapshot(self):
        with self.lock:
            status = ('no_final_result' if self.final_event is None else
                      'success' if final_result_error(self.final_event) is None else 'failed')
            result = {'schema_version': 2, 'type': 'conversation', 'task_id': self.source.parent.name,
                      'generation_status': self.generation_status or status,
                      'messages': copy.deepcopy(self.messages), 'events': self.events,
                      'hidden_progress_events': self.progress_events, 'unparsed_lines': self.invalid_lines,
                      'progress': {'phase': self.phase, 'last_activity_at': self.last_activity_at,
                                   'last_content_at': self.last_content_at}}
            copies = {id(block): copied for original, message in zip(self.messages, result['messages'])
                      for block, copied in zip(original['content'], message['content'])}
            for block_key, original_block in self.blocks.items():
                self.materialize(block_key, copies[id(original_block)])
            if self.session_id:
                result['session_id'] = self.session_id
            if self.model:
                result['model'] = self.model
            if self.final_event is not None:
                error = final_result_error(self.final_event)
                if error:
                    result['generation_error'] = error
                result['result'] = {k: self.final_event[k] for k in (
                    'subtype', 'is_error', 'result', 'errors', 'num_turns', 'duration_ms', 'total_cost_usd',
                    'usage', 'stop_reason', 'terminal_reason', 'api_error_status') if k in self.final_event}
            if self.diagnostics:
                result['diagnostics'] = copy.deepcopy(self.diagnostics)
            return result

    def write(self, destination):
        if Path(destination).resolve() == self.source.resolve():
            raise ValueError('Readable destination must differ from the raw trajectory')
        atomic_json(destination, self.snapshot())


def render_trajectory(source, destination=None, *, prompt=None, generation_status=None):
    reader = TrajectoryReader(source, prompt=prompt)
    reader.generation_status = generation_status
    reader.refresh(final=True)
    destination = Path(destination) if destination else reader.source.with_suffix('.json')
    reader.write(destination)
    return destination


@contextmanager
def capture_trajectory(destination, *, prompt=None, interval=2):
    """Yield an in-memory accumulator and a pipe accepted by subprocess stdout.

    No raw temporary file is created. The only persisted trajectory is an atomic
    JSON snapshot, refreshed periodically and after EOF, including on timeout.
    """
    destination = Path(destination)
    reader = TrajectoryReader(destination.with_suffix('.pipe'), prompt=prompt, live=True)
    reader.generation_status = 'running'
    read_fd, write_fd = os.pipe()
    source = os.fdopen(read_fd, 'rb')
    writer = os.fdopen(write_fd, 'w', encoding='utf-8', buffering=1)
    stop = threading.Event()
    errors = []

    def consume():
        try:
            with source:
                for line in source:
                    reader.consume(line)
        except BaseException as exc:
            errors.append(exc)

    def save():
        while not stop.wait(interval):
            try:
                reader.write(destination)
            except Exception as exc:
                errors.append(exc)
                return

    reader.write(destination)
    ingest = threading.Thread(target=consume, daemon=True)
    persist = threading.Thread(target=save, daemon=True)
    ingest.start()
    persist.start()
    try:
        yield reader, writer
    finally:
        writer.close()
        ingest.join(timeout=5)
        stop.set()
        persist.join()
        if ingest.is_alive():
            raise RuntimeError('Trajectory pipe did not close after process exit')
        if reader.generation_status == 'running':
            reader.generation_status = None
        reader.write(destination)
        if errors:
            raise RuntimeError('Trajectory capture failed') from errors[0]


@contextmanager
def readable_trajectory(source, destination, interval=5, *, stream=None, prompt=None):
    """Compatibility importer/watcher for historical raw trajectories."""
    reader = TrajectoryReader(source, stream=stream, prompt=prompt)
    stop = threading.Event()

    def update(final=False):
        reader.refresh(final=final)
        reader.write(destination)

    def watch():
        while not stop.wait(interval):
            update()

    update()
    thread = threading.Thread(target=watch, daemon=True)
    thread.start()
    try:
        yield reader
    finally:
        stop.set()
        thread.join()
        update(final=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Historical raw trajectory.jsonl to import')
    parser.add_argument('--output', type=Path, help='Default: trajectory.json')
    parser.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    destination = args.output or args.source.with_suffix('.json')
    if destination.resolve() == args.source.resolve():
        parser.error('output must differ from source')
    if args.watch:
        with readable_trajectory(args.source, destination):
            try:
                threading.Event().wait()
            except KeyboardInterrupt:
                pass
    else:
        print(render_trajectory(args.source, destination))


if __name__ == '__main__':
    main()
