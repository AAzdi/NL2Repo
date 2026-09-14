"""A per-task Unix socket exposing only the fixed Anthropic model API.

The container has no network interface except loopback. Its local relay reaches
this socket; the gateway credential and destination never enter the container.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
import http.client
import json
import logging
import os
from pathlib import Path
import secrets
import socketserver
import tempfile
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from claude_code.generation import token_limits


TOOLS = {'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep'}
FIELDS = {'model', 'messages', 'max_tokens', 'system', 'tools', 'tool_choice',
          'stream', 'temperature', 'top_p', 'top_k', 'stop_sequences',
          'metadata', 'thinking', 'output_config'}
MAX_BODY = 32 * 1024 * 1024


def validate_request(path, body, model):
    if path not in ('/v1/messages', '/v1/messages?beta=true',
                    '/v1/messages/count_tokens', '/v1/messages/count_tokens?beta=true'):
        raise ValueError('Only the model messages API is available')
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('Unsupported model API fields')
    if set(value) - FIELDS:
        raise ValueError('Unsupported model API fields: ' + repr(sorted(set(value) - FIELDS))[:200])
    if value.get('model') != model:
        raise ValueError('Model routing is fixed by the benchmark')
    output = value.get('output_config', {})
    if not isinstance(output, dict) or set(output) - {'effort'}:
        raise ValueError('Only output effort configuration is supported')
    if 'effort' in output and output['effort'] not in ('low', 'medium', 'high', 'max'):
        raise ValueError('Unsupported output effort')
    metadata = value.get('metadata', {})
    if not isinstance(metadata, dict) or set(metadata) - {'user_id'} or (
            'user_id' in metadata and not isinstance(metadata['user_id'], str)):
        raise ValueError('Only user_id metadata is supported')

    def schema_refs(item):
        if isinstance(item, dict):
            for key, child in item.items():
                if key in ('$ref', '$dynamicRef', '$recursiveRef') and (
                        not isinstance(child, str) or not child.startswith('#')):
                    raise ValueError('Remote schema references are unavailable')
                schema_refs(child)
        elif isinstance(item, list):
            for child in item:
                schema_refs(child)

    for tool in value.get('tools', []):
        if not isinstance(tool, dict) or tool.get('name') not in TOOLS or set(tool) - {
                'name', 'description', 'input_schema', 'cache_control', 'strict', 'defer_loading'}:
            raise ValueError('Only local benchmark tools are allowed')
        schema_refs(tool.get('input_schema', {}))

    def content(blocks):
        if isinstance(blocks, str):
            return
        if not isinstance(blocks, list):
            raise ValueError('Invalid content')
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError('Invalid content block')
            kind = block.get('type')
            if kind == 'tool_result':
                content(block.get('content', ''))
            elif kind == 'image':
                if block.get('source', {}).get('type') != 'base64':
                    raise ValueError('Remote content is unavailable')
            elif kind not in ('text', 'tool_use', 'thinking', 'redacted_thinking'):
                raise ValueError('Unsupported content block')

    content(value.get('system', ''))
    for message in value.get('messages', []):
        content(message.get('content', ''))
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


@contextmanager
def model_channel(environment, *, host_base_url=None, max_output_tokens=None,
                  timeout_seconds=3600, task_id=None):
    """Yield (socket directory, container environment); never forward client auth."""
    token_limits({'model_timeout_seconds': timeout_seconds, **(
        {'max_output_tokens': max_output_tokens} if max_output_tokens is not None else {})})
    base = urlsplit(host_base_url or environment['ANTHROPIC_BASE_URL'])
    if base.hostname == 'host.docker.internal':
        base = base._replace(netloc='127.0.0.1' + (f':{base.port}' if base.port else ''))
    if base.scheme not in ('http', 'https') or not base.hostname or base.username or base.query or base.fragment:
        raise ValueError('Invalid host model gateway URL')
    destination = urlunsplit(base).rstrip('/')
    token = secrets.token_hex(32)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log prompts, credentials or provider error bodies.

        def do_POST(self):
            self.connection.settimeout(60)
            if self.headers.get('Authorization') != 'Bearer ' + token:
                self.send_error(403)
                return
            try:
                if self.headers.get('Transfer-Encoding'):
                    raise ValueError('Chunked request bodies are unsupported')
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > MAX_BODY:
                    raise ValueError('Invalid request size')
                body = self.rfile.read(size)
                value = validate_request(self.path, body, environment['ANTHROPIC_MODEL'])
                if max_output_tokens is not None and self.path.split('?')[0] == '/v1/messages':
                    # The trusted experiment limit wins over Claude Code's
                    # built-in cap for unknown model IDs. Count requests stay intact.
                    value['max_tokens'] = max_output_tokens
                    body = json.dumps(value).encode('utf-8')
            except (ValueError, TypeError, AttributeError) as exc:
                logging.getLogger(__name__).warning('Model channel rejected request: %s', exc)
                self.send_error(400, 'Request rejected by benchmark model channel')
                return
            request = urllib.request.Request(destination + self.path, data=body, headers={
                'Content-Type': 'application/json', 'anthropic-version': '2023-06-01',
                'Authorization': 'Bearer ' + environment['ANTHROPIC_AUTH_TOKEN'],
                **({'x-nl2repo-task-id': task_id,
                    'x-nl2repo-request-id': secrets.token_hex(16)} if task_id else {}),
            }, method='POST')
            started = False
            try:
                with opener.open(request, timeout=timeout_seconds) as response:
                    self.send_response(response.status)
                    self.send_header('Content-Type', response.headers.get('Content-Type', 'application/json'))
                    self.end_headers()
                    started = True
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except urllib.error.HTTPError as exc:
                # Provider errors can echo internal headers or URLs; keep them on the host.
                self.send_error(exc.code, 'Model gateway rejected the request')
                exc.close()
            except (OSError, urllib.error.URLError, http.client.HTTPException):
                if not started:
                    self.send_error(502, 'Model gateway unavailable')
                else:
                    try:
                        self.wfile.write(b'event: error\ndata: {"type":"error","error":'
                                         b'{"type":"api_error","message":"Model channel interrupted"}}\n\n')
                        self.wfile.flush()
                    except OSError:
                        pass
            finally:
                self.close_connection = True

    with tempfile.TemporaryDirectory(prefix='nl2repo-model-') as directory:
        os.chmod(directory, 0o755)
        socket_path = Path(directory) / 'api.sock'
        with Server(str(socket_path), Handler) as server:
            os.chmod(socket_path, 0o666)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
            thread.start()
            try:
                yield directory, {**environment, 'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18080',
                                  'ANTHROPIC_AUTH_TOKEN': token}
            finally:
                server.shutdown()
                thread.join()
