import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from claude_code.trajectory import TrajectoryReader, readable_trajectory, render_trajectory, capture_trajectory


class TrajectoryTests(unittest.TestCase):
    def test_api_terminal_reason_overrides_success_flags_without_http_status(self):
        reader = TrajectoryReader(self.source)
        reader.add({'type': 'result', 'subtype': 'success', 'is_error': False,
                    'stop_reason': 'stop_sequence', 'terminal_reason': 'api_error',
                    'api_error_status': None})
        self.assertEqual(reader.snapshot()['generation_status'], 'failed')

    def stream(self, data):
        return {'type': 'stream_event', 'session_id': 's', 'event': data}

    def test_partial_stream_is_visible_and_echoes_do_not_duplicate_messages(self):
        reader = TrajectoryReader(self.source)
        reader.add(self.stream({'type': 'message_start', 'message': {'id': 'm'}}))
        reader.add(self.stream({'type': 'content_block_start', 'index': 0,
                                'content_block': {'type': 'thinking', 'thinking': ''}}))
        for text in ('Plan ', '中文'):
            reader.add(self.stream({'type': 'content_block_delta', 'index': 0,
                                    'delta': {'type': 'thinking_delta', 'thinking': text}}))
        current = reader.snapshot()['messages'][0]
        self.assertEqual(current['content'][0]['thinking'], 'Plan 中文')
        self.assertFalse(current['stream_complete'])
        self.assertFalse(current['content'][0]['complete'])
        self.assertIsNone(current['usage'])
        reader.add({'type': 'assistant', 'session_id': 's', 'message': {'id': 'm',
            'content': [{'type': 'thinking', 'thinking': 'Plan 中文'}]}})
        reader.add(self.stream({'type': 'content_block_stop', 'index': 0}))
        reader.add({'type': 'user', 'session_id': 's', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 't', 'content': 'ok'}]}})
        reader.add(self.stream({'type': 'content_block_start', 'index': 1,
            'content_block': {'type': 'tool_use', 'id': 't', 'name': 'Write', 'input': {}}}))
        reader.add(self.stream({'type': 'content_block_delta', 'index': 1,
            'delta': {'type': 'input_json_delta', 'partial_json': '{"file_path":'}}))
        partial = reader.snapshot()['messages'][0]['content'][1]
        self.assertEqual(partial['partial_input'], '{"file_path":')
        self.assertFalse(partial['input_json_valid'])
        reader.add(self.stream({'type': 'content_block_delta', 'index': 1,
            'delta': {'type': 'input_json_delta', 'partial_json': '"a", "content": "b"}'}}))
        reader.add({'type': 'assistant', 'session_id': 's', 'message': {'id': 'm',
            'content': [{'type': 'tool_use', 'id': 't', 'name': 'Write',
                         'input': {'file_path': 'a', 'content': 'b'}}]}})
        reader.add(self.stream({'type': 'content_block_stop', 'index': 1}))
        reader.add(self.stream({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
                                'usage': {'output_tokens': 12}}))
        reader.add(self.stream({'type': 'message_stop'}))
        messages = reader.snapshot()['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(messages[0]['content']), 2)
        self.assertEqual(messages[0]['content'][1]['input'], {'file_path': 'a', 'content': 'b'})
        self.assertTrue(messages[0]['stream_complete'])
        self.assertEqual(messages[0]['usage']['output_tokens'], 12)

    def test_live_pipe_persists_single_json_without_raw_file(self):
        destination = self.source.with_suffix('.json')
        persisted = threading.Event()
        original = TrajectoryReader.write

        def write(reader, path):
            original(reader, path)
            if reader.events:
                persisted.set()

        with patch.object(TrajectoryReader, 'write', write):
            with capture_trajectory(destination, interval=.01) as (reader, pipe):
                pipe.write(json.dumps({'type': 'assistant', 'message': {'content': 'live 中文'}}) + '\n')
                self.assertTrue(persisted.wait(2))
                self.assertEqual(json.loads(destination.read_text())['generation_status'], 'running')
                reader.generation_status = 'interrupted'
        self.assertEqual(list(destination.parent.iterdir()), [destination])
        result = json.loads(destination.read_text())
        self.assertEqual(result['generation_status'], 'interrupted')
        self.assertEqual(result['messages'][0]['content'][0]['text'], 'live 中文')

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = Path(temp.name) / 'trajectory.jsonl'

    def save(self, events):
        self.source.write_text(''.join(json.dumps(e, ensure_ascii=False) + '\n' for e in events), encoding='utf-8')

    def test_tools_multiline_unicode_and_noise(self):
        self.save([
            {'type': 'system', 'subtype': 'thinking_tokens', 'estimated_tokens': 10},
            {'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'id': 'call1', 'name': 'Bash',
                 'input': {'command': 'echo 中文\nprintf done'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 'call1', 'is_error': True,
                 'content': [{'type': 'text', 'text': '中文\n```python\ncode\n```'}]}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'API Error'},
        ])
        original = self.source.read_bytes()
        destination = render_trajectory(self.source)
        self.assertEqual(destination.name, 'trajectory.json')
        self.assertFalse(self.source.with_suffix('.md').exists())
        output = destination.read_text(encoding='utf-8')
        self.assertGreater(len(output.splitlines()), 1)
        conversation = json.loads(output)
        records = conversation['messages']
        self.assertEqual(len(records), 2)
        self.assertEqual(conversation['hidden_progress_events'], 1)
        self.assertNotIn('estimated_tokens', output)
        self.assertIn('中文', output)
        call, result = records[0]['content'][0], records[1]['content'][0]
        self.assertEqual(call['type'], 'tool_use')
        self.assertEqual(records[0]['role'], 'assistant')
        self.assertEqual(call['input']['command'], 'echo 中文\nprintf done')
        self.assertEqual(result['name'], 'Bash')
        self.assertTrue(result['is_error'])
        self.assertEqual(result['tool_use_id'], call['id'])
        self.assertEqual(result['content'][0]['text'], '中文\n```python\ncode\n```')
        self.assertEqual(conversation['generation_status'], 'failed')
        self.assertEqual(conversation['result']['result'], 'API Error')
        self.assertEqual(self.source.read_bytes(), original)

    def test_incremental_partial_utf8_line_and_no_duplicates(self):
        line = json.dumps({'type': 'assistant', 'message': {'content': '中文'}}, ensure_ascii=False).encode()
        cut = line.index('中'.encode()) + 1
        self.source.write_bytes(line[:cut])
        reader = TrajectoryReader(self.source)
        reader.refresh()
        self.assertEqual(reader.events, 0)
        with self.source.open('ab') as stream:
            stream.write(line[cut:] + b'\n')
        reader.refresh()
        reader.refresh()
        self.assertEqual(reader.events, 1)
        self.assertEqual(reader.invalid_lines, 0)
        self.assertEqual(reader.messages[0]['content'][0]['text'], '中文')

    def test_message_metadata_survives_merged_blocks(self):
        self.save([
            {'type': 'assistant', 'message': {'id': 'm1', 'model': 'default',
                'content': [{'type': 'thinking', 'thinking': 'plan'}],
                'stop_reason': None, 'usage': {'output_tokens': 0}}},
            {'type': 'assistant', 'message': {'id': 'm1',
                'content': [{'type': 'text', 'text': 'done'}],
                'stop_reason': 'max_tokens', 'usage': {'output_tokens': 4096}}}])
        message = json.loads(render_trajectory(self.source).read_text())['messages'][0]
        self.assertEqual(message['stop_reason'], 'max_tokens')
        self.assertEqual(message['usage']['output_tokens'], 4096)
        self.assertEqual(message['model'], 'default')
        self.assertEqual(len(message['content']), 2)

    def test_bad_lines_unknown_events_and_unterminated_final_line(self):
        self.source.write_text('bad line\n[]\n{"type":"new_event","payload":42}', encoding='utf-8')
        output = render_trajectory(self.source).read_text()
        conversation = json.loads(output)
        self.assertEqual(conversation['unparsed_lines'], 2)
        self.assertEqual(conversation['diagnostics'][0]['text'], 'bad line\n')
        self.assertEqual(conversation['diagnostics'][2]['payload'], 42)
        self.assertEqual(conversation['generation_status'], 'no_final_result')

    def test_cannot_overwrite_raw_log(self):
        self.save([{'type': 'result', 'subtype': 'success'}])
        original = self.source.read_bytes()
        with self.assertRaises(ValueError):
            render_trajectory(self.source, self.source)
        self.assertEqual(self.source.read_bytes(), original)

    def test_live_companion_updates_before_generation_ends(self):
        self.source.touch()
        destination = self.source.with_suffix('.readable.jsonl')
        updated = threading.Event()
        original_write = TrajectoryReader.write

        def write(reader, path):
            original_write(reader, path)
            if reader.events:
                updated.set()

        with patch.object(TrajectoryReader, 'write', write):
            with readable_trajectory(self.source, destination, interval=0.01):
                self.save([{'type': 'assistant', 'message': {'content': 'Live output'}}])
                self.assertTrue(updated.wait(timeout=2))
                conversation = json.loads(destination.read_text())
                self.assertEqual(conversation['messages'][0]['content'][0]['text'], 'Live output')

    def test_merge_message_blocks_and_include_initial_prompt(self):
        self.save([
            {'type': 'system', 'subtype': 'init', 'model': 'model', 'session_id': 'session'},
            {'type': 'assistant', 'message': {'id': 'a', 'content': [
                {'type': 'thinking', 'thinking': 'Plan'}]}},
            {'type': 'assistant', 'message': {'id': 'a', 'content': [
                {'type': 'text', 'text': 'Answer'},
                {'type': 'tool_use', 'id': 't', 'name': 'Read', 'input': {'file_path': 'start.md'}}]}},
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't', 'content': 'Requirements'}]}},
            {'type': 'assistant', 'message': {'id': 'b', 'content': 'Done'}},
            {'type': 'result', 'subtype': 'success', 'stop_reason': 'end_turn', 'result': 'Done'},
        ])
        conversation = json.loads(render_trajectory(self.source, prompt='Implement 中文').read_text())
        messages = conversation['messages']
        self.assertEqual([m['role'] for m in messages], ['user', 'assistant', 'user', 'assistant'])
        self.assertEqual(messages[0]['content'][0]['text'], 'Implement 中文')
        self.assertEqual([b['type'] for b in messages[1]['content']], ['thinking', 'text', 'tool_use'])
        self.assertEqual(messages[2]['content'][0]['name'], 'Read')
        self.assertEqual(conversation['model'], 'model')
        self.assertEqual(conversation['session_id'], 'session')
        self.assertEqual(conversation['generation_status'], 'success')
        self.assertEqual(conversation['result']['stop_reason'], 'end_turn')

    def test_terminal_metadata_and_error_status_are_preserved(self):
        for stop_reason, api_error_status, expected in [
                ('stop_sequence', None, 'success'), ('end_turn', 500, 'failed'),
                ('tool_use', None, 'failed'), (None, None, 'failed')]:
            self.save([{'type': 'result', 'subtype': 'success', 'is_error': False,
                        'stop_reason': stop_reason, 'api_error_status': api_error_status,
                        'terminal_reason': 'completed'}])
            conversation = json.loads(render_trajectory(self.source).read_text())
            self.assertEqual(conversation['generation_status'], expected)
            self.assertEqual(conversation['result']['stop_reason'], stop_reason)
            self.assertEqual(conversation['result']['terminal_reason'], 'completed')
            self.assertEqual(conversation['result']['api_error_status'], api_error_status)

    def test_temporary_stream_poll_does_not_move_writer_offset(self):
        destination = self.source.with_suffix('.readable.jsonl')
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as stream:
            reader = TrajectoryReader(self.source, stream=stream)
            for text in ('中文', 'second'):
                stream.write(json.dumps({'type': 'assistant', 'message': {'content': text}}) + '\n')
                stream.flush()
                position = stream.tell()
                reader.refresh()
                self.assertEqual(stream.tell(), position)
            reader.refresh(final=True)
            reader.write(destination)
        self.assertFalse(self.source.exists())
        self.assertEqual([m['content'][0]['text'] for m in json.loads(destination.read_text())['messages']],
                         ['中文', 'second'])


if __name__ == '__main__':
    unittest.main()
