import http.client
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

from claude_code import offline
from claude_code.model_channel import model_channel, validate_request
from claude_code.offline import (offline_environment, inspect_offline_image,
                                 grading_runtime_info, _grading_runtime_info)
from grading.post_processor import (run_test_commands, create_dockerfile, build_test_image,
                                     DockerHostInfo, pinned_base_image)


class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self.host)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        runtime = patch('claude_code.offline.runtime_info',
                        side_effect=lambda image: {'image': image, 'python': '3.12.14'})
        runtime.start()
        self.addCleanup(runtime.stop)
        grading = patch('claude_code.offline.grading_runtime_info',
                        side_effect=lambda image: offline.runtime_info(image))
        grading.start()
        self.addCleanup(grading.stop)

    def test_explicit_residue_check_skip_retains_package_policy(self):
        entry = {'generation_image': 'nl2repo-claude-code:2.1.263',
                 'grading_image': 'ghcr.io/multimodal-art-projection/nl2repobench/aiofiles:1.0',
                 'target_distributions': ['aiofiles'], 'target_modules': ['aiofiles']}
        options = {'check_image_residue': False, 'offline_environments': {'aiofiles': entry}}
        with patch('claude_code.offline.inspect_offline_image') as inspect:
            result = offline_environment(options, 'aiofiles')
            inspect.assert_not_called()
        self.assertEqual(result['image_residue_check'], 'skipped')
        self.assertEqual(result['target_distributions'], ['aiofiles'])
        with pinned_base_image(entry['grading_image'], None, allow_tag=True) as reference:
            self.assertEqual(reference, entry['grading_image'])
        with self.assertRaises(ValueError):
            with pinned_base_image(entry['grading_image'], None):
                pass

    def test_reject_routing_remote_fetch_and_server_tools(self):
        base = {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}]}
        for body in [base | {'api_base': 'http://attacker'}, base | {'model': 'other'},
                     base | {'tools': [{'type': 'web_search_20250305', 'name': 'web_search'}]},
                     base | {'tools': [{'name': 'WebFetch'}]},
                     base | {'tools': [{'name': 'Bash', 'input_schema': {'$ref': 'https://bad/schema'}}]},
                     base | {'metadata': {'api_base': 'https://bad'}},
                     base | {'messages': [{'role': 'user', 'content': [{'type': 'image',
                        'source': {'type': 'url', 'url': 'http://attacker'}}]}]},
                     base | {'messages': [{'role': 'user', 'content': [{'type': 'tool_result',
                        'content': [{'type': 'document', 'source': {'type': 'url', 'url': 'http://attacker'}}]}]}]}]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_request('/v1/messages', json.dumps(body), 'test')
        for path in ('/v1/chat/completions', '/v1/messages/../../fetch',
                     '/v1/messages?api_base=http://attacker', 'http://attacker/v1/messages'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_request(path, json.dumps(base), 'test')
        valid = base | {'tools': [{'name': 'Bash', 'input_schema': {'type': 'object'}}]}
        self.assertEqual(validate_request('/v1/messages?beta=true', json.dumps(valid), 'test'), valid)

    def test_offline_environment_fails_before_unprepared_task(self):
        with patch('claude_code.offline.inspect_offline_image') as inspect:
            with self.assertRaisesRegex(ValueError, 'not prepared'):
                offline_environment({}, 'boto')
            entry = dict(reviewed=True, generation_image='latest', grading_image='latest',
                         target_distributions=['boto3'], target_modules=['boto3'])
            with self.assertRaisesRegex(ValueError, 'immutable'):
                offline_environment({'offline_environments': {'boto': entry}}, 'boto')
            inspect.assert_not_called()
            entry.update(generation_image='sha256:' + 'a' * 64, grading_image='sha256:' + 'b' * 64)
            result = offline_environment({'offline_environments': {'boto': entry}}, 'boto')
            self.assertEqual({key: result[key] for key in entry}, entry)
            self.assertEqual(inspect.call_args_list[0].args[1:3], (('boto3',), ('boto3',)))

    def test_version_mismatch_is_rejected_even_with_residue_check_disabled(self):
        entry = dict(generation_image='generation:1', grading_image='grading:1',
                     target_distributions=['six'], target_modules=['six'])
        for generation, grading, expected in [('3.12.14', '3.10.11', None),
                                              ('3.12.14', '3.12.4', '3.10')]:
            policy = {**entry, **({'python_version': expected} if expected else {})}
            with patch('claude_code.offline.runtime_info', side_effect=[
                    {'image': 'sha256:' + 'a' * 64, 'python': generation},
                    {'image': 'sha256:' + 'b' * 64, 'python': grading}]), \
                    self.assertRaisesRegex(ValueError, 'Python version mismatch'):
                offline_environment({'check_image_residue': False,
                                     'offline_environments': {'six': policy}}, 'six')

    def test_matching_minor_versions_are_recorded_and_images_pinned(self):
        entry = dict(generation_image='generation:1', grading_image='grading:1', python_version='3.10',
                     target_distributions=['six'], target_modules=['six'])
        with patch('claude_code.offline.runtime_info', side_effect=[
                {'image': 'sha256:' + 'a' * 64, 'python': '3.10.20'},
                {'image': 'sha256:' + 'b' * 64, 'python': '3.10.11'}]):
            result = offline_environment({'check_image_residue': False,
                                          'offline_environments': {'six': entry}}, 'six')
        self.assertEqual(result['generation_image'], 'sha256:' + 'a' * 64)
        self.assertEqual(result['runtime']['grading']['python'], '3.10.11')
        self.assertEqual(result['image_references']['grading'], 'grading:1')

    def test_python_version_check_can_be_skipped_without_skipping_image_checks(self):
        entry = dict(reviewed=True, generation_image='sha256:' + 'a' * 64,
                     grading_image='sha256:' + 'b' * 64, python_version='3.10',
                     target_distributions=['six'], target_modules=['six'])
        with patch('claude_code.offline.runtime_info', side_effect=[
                {'image': entry['generation_image'], 'python': '3.12.14'},
                {'image': entry['grading_image'], 'python': '3.11.7'}]), \
                patch('claude_code.offline.inspect_offline_image') as inspect:
            result = offline_environment({'check_python_version': False,
                                          'offline_environments': {'six': entry}}, 'six')
        self.assertEqual(result['python_version_check'], 'skipped')
        self.assertEqual(result['runtime']['generation']['python'], '3.12.14')
        self.assertEqual(result['runtime']['grading']['python'], '3.11.7')
        self.assertEqual(inspect.call_count, 2)

    def test_python_version_check_requires_boolean(self):
        with self.assertRaisesRegex(ValueError, 'check_python_version must be a boolean'):
            offline_environment({'check_python_version': 'false'}, 'six')

    def test_image_with_onbuild_or_volumes_is_rejected(self):
        for config in ({'OnBuild': ['RUN curl https://example.com']}, {'Volumes': {'/workspace': {}}}):
            with patch('claude_code.offline.subprocess.run', return_value=subprocess.CompletedProcess(
                    [], 0, stdout=json.dumps([{'Config': config}]))) as run:
                with self.assertRaisesRegex(ValueError, 'ONBUILD'):
                    inspect_offline_image('sha256:' + 'c' * 64, ('target',), ('target',), 'generation')
                self.assertEqual(run.call_count, 1)

    def test_grading_is_offline_without_model_channel(self):
        with patch('grading.post_processor.create_advanced_container') as create, \
             patch('grading.post_processor.execute_command_in_container', return_value=(0, '1 passed in 0.1s')), \
             patch('grading.post_processor.collect_container_diagnostics', return_value={}), \
             patch('grading.post_processor.remove_container'):
            run_test_commands('image', 'name', ['pytest'], 1, None,
                              logging.getLogger(__name__), isolated=True)
        args = create.call_args.kwargs
        self.assertEqual(args['network_mode'], 'none')
        self.assertEqual(args['cap_drop'], ['ALL'])
        self.assertEqual(args['security_options'], ['no-new-privileges'])
        self.assertNotIn('volumes', args)


class GradingImageSourceTests(unittest.TestCase):
    def setUp(self):
        _grading_runtime_info.cache_clear()
        self.addCleanup(_grading_runtime_info.cache_clear)

    def test_different_images_can_pull_concurrently(self):
        barrier = threading.Barrier(2)

        def pull(*args, **kwargs):
            barrier.wait(timeout=5)

        with patch('claude_code.offline.subprocess.run', side_effect=pull) as run, \
             patch('claude_code.offline.runtime_info', side_effect=lambda image: {'image': image}), \
             ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(grading_runtime_info, ['ghcr.io/org/a:1', 'ghcr.io/org/b:1']))
        self.assertEqual(run.call_count, 2)
        self.assertEqual([r['image'] for r in results], ['ghcr.io/org/a:1', 'ghcr.io/org/b:1'])

    def test_concurrent_users_of_same_image_pull_only_once(self):
        barrier = threading.Barrier(4)

        def prepare(_):
            barrier.wait(timeout=5)
            return grading_runtime_info('ghcr.io/org/shared:1')

        with patch('claude_code.offline.subprocess.run') as run, \
             patch('claude_code.offline.runtime_info', return_value={'image': 'sha256:' + 'a' * 64}), \
             ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(prepare, range(4)))
        run.assert_called_once()
        self.assertTrue(all(result == results[0] for result in results))

    def test_registry_pull_precedes_resolution_and_is_cached(self):
        calls = []
        reference = 'ghcr.io/multimodal-art-projection/nl2repobench/retrying:1.0'
        with patch('claude_code.offline.subprocess.run', side_effect=lambda *a, **kw: calls.append('pull')) as run, \
                patch('claude_code.offline.runtime_info', side_effect=lambda image:
                      calls.append('resolve') or {'image': 'sha256:' + 'a' * 64, 'python': '3.10.11'}):
            result = grading_runtime_info(reference)
            self.assertEqual(grading_runtime_info(reference), result)
        self.assertEqual(calls, ['pull', 'resolve'])
        self.assertEqual(run.call_args.args[0], ['docker', 'pull', '--platform', 'linux/amd64', reference])
        self.assertEqual(result['source'], 'registry_pull')

    def test_pull_failure_does_not_use_local_tag(self):
        with patch('claude_code.offline.subprocess.run', side_effect=subprocess.CalledProcessError(1, 'pull')), \
                patch('claude_code.offline.runtime_info') as resolve:
            with self.assertRaises(subprocess.CalledProcessError):
                grading_runtime_info('ghcr.io/org/image:1')
            resolve.assert_not_called()

    def test_reviewed_local_id_does_not_pull(self):
        image = 'sha256:' + 'b' * 64
        with patch('claude_code.offline.subprocess.run') as run, \
                patch('claude_code.offline.runtime_info', return_value={'image': image, 'python': '3.12.4'}):
            self.assertEqual(grading_runtime_info(image)['source'], 'local_image')
            run.assert_not_called()


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        requests = self.requests

        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                requests.append((self.path, dict(self.headers), json.loads(body)))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b'data: {"ok":true}\n\ndata: [DONE]\n\n')

        self.gateway = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
        self.thread = threading.Thread(target=self.gateway.serve_forever, kwargs={'poll_interval': .05})
        self.thread.start()
        self.addCleanup(self.gateway.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.gateway.shutdown)
        self.env = {'ANTHROPIC_MODEL': 'test', 'ANTHROPIC_AUTH_TOKEN': 'host-secret',
                    'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{self.gateway.server_port}'}

    def test_output_limit_overrides_client_cap_but_not_count_request(self):
        with model_channel(self.env, max_output_tokens=393216, timeout_seconds=14400) as (directory, env):
            for path in ('/v1/messages?beta=true', '/v1/messages/count_tokens?beta=true'):
                body = {'model': 'test', 'messages': [{'role': 'user', 'content': 'hello'}],
                        'max_tokens': 128000}
                connection = UnixConnection(str(Path(directory) / 'api.sock'))
                connection.request('POST', path, json.dumps(body), headers={
                    'Authorization': 'Bearer ' + env['ANTHROPIC_AUTH_TOKEN']})
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                response.read()
                connection.close()
        self.assertEqual(self.requests[0][2]['max_tokens'], 393216)
        self.assertEqual(self.requests[1][2]['max_tokens'], 128000)
        self.assertEqual(self.requests[0][2]['messages'], body['messages'])

    def test_only_fixed_authenticated_model_endpoint_is_forwarded(self):
        with model_channel(self.env) as (directory, env):
            self.assertNotEqual(env['ANTHROPIC_AUTH_TOKEN'], 'host-secret')
            for path, token, body, expected in [
                ('/v1/messages', 'wrong', {'model': 'test'}, 403),
                ('/fetch', env['ANTHROPIC_AUTH_TOKEN'], {'model': 'test'}, 400),
                ('/v1/messages', env['ANTHROPIC_AUTH_TOKEN'], {'model': 'test', 'api_base': 'http://bad'}, 400),
                ('/v1/messages', env['ANTHROPIC_AUTH_TOKEN'], {'model': 'test', 'messages': []}, 200),
            ]:
                connection = UnixConnection(str(Path(directory) / 'api.sock'))
                connection.request('POST', path, json.dumps(body), headers={
                    'Authorization': 'Bearer ' + token, 'x-api-key': 'attacker-key',
                    'x-eval-domain-proxy': 'http://bad'})
                response = connection.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
                connection.close()
        self.assertFalse(Path(directory).exists())
        self.assertEqual(len(self.requests), 1)
        headers = {k.lower(): v for k, v in self.requests[0][1].items()}
        self.assertEqual(headers['authorization'], 'Bearer host-secret')
        self.assertNotIn('x-api-key', headers)
        self.assertNotIn('x-eval-domain-proxy', headers)


@unittest.skipUnless(os.environ.get('NL2REPO_TEST_DOCKER') == '1', 'opt-in Docker integration')
class DockerIsolationTests(unittest.TestCase):
    image = 'nl2repo-claude-code:2.1.263'

    def test_grader_build_and_generated_download_under_real_evaluator(self):
        name = 'nl2repo-integrity-' + uuid.uuid4().hex
        base_tag, test_tag = name + '-base', name + '-test'
        host = DockerHostInfo(hostname='localhost')
        log = logging.getLogger(__name__)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_id = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', self.image], text=True).strip()
            try:
                with pinned_base_image(image_id, host) as reference:
                    (root / 'Dockerfile').write_text(f'FROM {reference}\nENTRYPOINT []\n')
                    build = subprocess.run(['docker', 'build', '--network', 'none', '-t', base_tag, directory],
                                           capture_output=True, text=True, timeout=45)
                    self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
                base_id = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', base_tag], text=True).strip()
                workspace = root / 'workspace'
                workspace.mkdir()
                (workspace / 'probe.py').write_text('''import pathlib, socket, urllib.request
assert [name for _, name in socket.if_nameindex()] == ["lo"]
assert not pathlib.Path("/run/nl2repo-model").exists()
status = pathlib.Path("/proc/self/status").read_text()
assert "CapEff:\\t0000000000000000" in status
assert "NoNewPrivs:\\t1" in status
try: urllib.request.urlopen("http://1.1.1.1/original.py", timeout=2)
except OSError: print("GRADING_DOWNLOAD_BLOCKED")
else: raise AssertionError("grading downloaded original source")
''')
                with pinned_base_image(base_id, host) as reference:
                    dockerfile = create_dockerfile(str(workspace), 'unused', log, base_image=reference)
                    build_test_image(dockerfile, test_tag, host, log)
                result = run_test_commands(test_tag, name, ['python /workspace/probe.py'], 1,
                                           host, log, isolated=True)
                self.assertEqual(result['last_exit_code'], 0, result)
                self.assertIn('GRADING_DOWNLOAD_BLOCKED', result['command_results'][0]['output'])
            finally:
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=15)
                subprocess.run(['docker', 'image', 'rm', test_tag, base_tag], capture_output=True, timeout=30)

    def test_direct_and_scripted_downloads_have_no_route(self):
        script = '''import errno, socket, subprocess, urllib.request
assert [name for _, name in socket.if_nameindex()] == ["lo"]
for family, address in [(socket.AF_INET, ("1.1.1.1", 443)),
                        (socket.AF_INET6, ("2606:4700:4700::1111", 443))]:
    s = socket.socket(family); s.settimeout(2)
    try: s.connect(address)
    except OSError: pass
    else: raise AssertionError("external route available")
    finally: s.close()
# Executing this string models a download deferred into a generated module.
exec("try:\\n urllib.request.urlopen('http://1.1.1.1/original.py', timeout=2)\\nexcept OSError:\\n pass\\nelse:\\n raise AssertionError('script downloaded source')")
for cmd in [["git", "clone", "http://1.1.1.1/original", "/tmp/original"],
            ["python", "-m", "pip", "download", "boltons", "--isolated", "--no-cache-dir",
             "--index-url", "http://1.1.1.1/simple", "--trusted-host", "1.1.1.1", "--retries", "0", "--timeout", "2"]]:
    p = subprocess.run(cmd, capture_output=True, timeout=15)
    assert p.returncode != 0, cmd
print("PASS: IPv4, IPv6, generated Python, git clone, pip download blocked")
'''
        result = subprocess.run(['docker', 'run', '--rm', '--network', 'none',
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--entrypoint', 'python', self.image, '-c', script],
            capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_real_claude_cli_can_use_model_channel_without_network(self):
        calls = []
        with_hook = getattr(self, 'with_hook', False)
        long_context = getattr(self, 'long_context', False)
        request_failure = getattr(self, 'request_failure', None)

        class FakeModel(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(value)
                if request_failure == 'http' and len(calls) == 2:
                    self.send_response(503)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"type":"error","error":{"type":"overloaded_error","message":"temporary"}}')
                    return
                if request_failure and not value.get('stream'):
                    # Claude Code recovers an interrupted stream by retrying
                    # the same messages as a non-streaming API request.
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        'id': f'msg_test_{len(calls)}', 'type': 'message', 'role': 'assistant',
                        'model': 'test', 'content': [{'type': 'text', 'text': 'CHANNEL_OK'}],
                        'stop_reason': 'end_turn', 'stop_sequence': None,
                        'usage': {'input_tokens': 1, 'output_tokens': 1},
                    }).encode())
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                events = [
                    ('message_start', {'message': {'id': f'msg_test_{len(calls)}', 'type': 'message',
                     'role': 'assistant', 'model': 'test', 'content': [], 'stop_reason': None,
                     'stop_sequence': None, 'usage': {'input_tokens': 1, 'output_tokens': 0}}}),
                    ('content_block_start', {'index': 0, 'content_block': {'type': 'text', 'text': ''}}),
                    ('content_block_delta', {'index': 0, 'delta': {'type': 'text_delta', 'text': 'CHANNEL_OK'}}),
                    ('content_block_stop', {'index': 0}),
                    ('message_delta', {'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
                                       'usage': {'output_tokens': 1}}),
                    ('message_stop', {}),
                ]
                if len(calls) == 1 or (with_hook and len(calls) == 2):
                    events[1] = ('content_block_start', {'index': 0, 'content_block': {
                        'type': 'tool_use', 'id': f'tool_probe_{len(calls)}', 'name': 'Bash', 'input': {}}})
                    events[2] = ('content_block_delta', {'index': 0, 'delta': {
                        'type': 'input_json_delta', 'partial_json': json.dumps({
                            'command': ('pip install aiofiles' if with_hook and len(calls) == 1
                                        else 'printf INTEGRITY_TOOL_OK; printenv CLAUDE_CODE_MAX_CONTEXT_TOKENS CLAUDE_CODE_AUTO_COMPACT_WINDOW CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'
                                        if long_context else 'printf INTEGRITY_TOOL_OK'),
                            'description': 'Integrity hook round-trip'})}})
                    events[4][1]['delta']['stop_reason'] = 'tool_use'
                for kind, data in events:
                    if request_failure in ('stream', 'eof') and len(calls) == 2 and kind == 'content_block_stop':
                        if request_failure == 'stream':
                            self.wfile.write(b'event: error\ndata: {"type":"error","error":'
                                             b'{"type":"api_error","message":"temporary stream failure"}}\n\n')
                            self.wfile.flush()
                        return
                    self.wfile.write(f'event: {kind}\ndata: {json.dumps(dict(type=kind, **data))}\n\n'.encode())
                    self.wfile.flush()

        server = ThreadingHTTPServer(('127.0.0.1', 0), FakeModel)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05})
        thread.start()
        hook_temp = tempfile.TemporaryDirectory()
        try:
            env = dict(ANTHROPIC_MODEL='test', ANTHROPIC_AUTH_TOKEN='host-secret',
                       ANTHROPIC_BASE_URL=f'http://127.0.0.1:{server.server_port}',
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1', DISABLE_AUTOUPDATER='1',
                       MAX_THINKING_TOKENS='0', DISABLE_PROMPT_CACHING='1',
                       CLAUDE_CODE_MAX_RETRIES='1', API_TIMEOUT_MS='15000')
            if long_context:
                from claude_code.runner import model_environment
                env.update(model_environment({'moduleName': 'test', 'baseUrl': env['ANTHROPIC_BASE_URL'],
                                              'sk': 'host-secret'},
                                             {'context_length': 1000000, 'max_output_tokens': 65536}))
            with model_channel(env, max_output_tokens=65536 if long_context else None) as (directory, child_env):
                relay = Path(__file__).resolve().parents[1] / 'claude_code/container_relay.cjs'
                command = ['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                           '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                           '--volume', f'{directory}:/run/nl2repo-model:ro',
                           '--volume', f'{relay}:/opt/nl2repo-relay.cjs:ro', '--entrypoint', 'node']
                if with_hook:
                    hook_root = Path(hook_temp.name)
                    hook_root.chmod(0o755)
                    hook_root.joinpath('hook.py').write_text(relay.with_name('integrity_hook.py').read_text())
                    hook_root.joinpath('policy.json').write_text(json.dumps({
                        'target_distributions': ['aiofiles'], 'target_modules': ['aiofiles']}))
                    hook_root.joinpath('managed-settings.json').write_text(json.dumps({
                        'allowManagedHooksOnly': True, 'disableAllHooks': False,
                        'hooks': {'PreToolUse': [{'matcher': 'Bash|Write|Edit', 'hooks': [{
                            'type': 'command', 'command': '/usr/local/bin/python -I /opt/nl2repo-integrity/hook.py'}]}]}}))
                    command.extend(['--volume', f'{hook_root}:/opt/nl2repo-integrity:ro',
                                    '--volume', f'{hook_root}/managed-settings.json:/etc/claude-code/managed-settings.json:ro'])
                for name in child_env:
                    command.extend(['--env', name])
                command.extend([self.image, '/opt/nl2repo-relay.cjs', '-p', 'Say CHANNEL_OK.',
                                '--model', 'test', '--tools', 'Bash,Read,Write,Edit,Glob,Grep',
                                '--dangerously-skip-permissions', '--max-turns', '4',
                                '--output-format', 'stream-json', '--verbose'])
                result = subprocess.run(command, env={**os.environ, **child_env},
                                        capture_output=True, text=True, timeout=50)
            self.assertEqual(result.returncode, 0, result.stdout[-4000:] + result.stderr[-1000:])
            self.assertIn('CHANNEL_OK', result.stdout)
            self.assertGreaterEqual(len(calls), 2)
            self.assertIn('INTEGRITY_TOOL_OK', json.dumps(calls[-1]['messages']))
            if request_failure:
                self.assertEqual(len(calls), 3, 'Retry only the failed request, not the earlier tool turn')
                self.assertEqual(calls[1]['messages'], calls[2]['messages'])
                results = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
                final = [event for event in results if event.get('type') == 'result'][-1]
                self.assertFalse(final.get('is_error'), final)
                self.assertEqual(len({event['session_id'] for event in results if event.get('session_id')}), 1)
            if long_context:
                self.assertTrue(all(call['max_tokens'] == 65536 for call in calls))
                tool_output = '\n'.join(
                    block['content']
                    for message in calls[-1]['messages']
                    for block in message['content']
                    if isinstance(block, dict) and block.get('type') == 'tool_result'
                    and isinstance(block.get('content'), str))
                self.assertIn('1000000\n1000000\n90', tool_output)
            if with_hook:
                self.assertIn('Do not retrieve the target project', json.dumps(calls[-1]['messages']))
                self.assertGreaterEqual(len(calls), 3)
        finally:
            hook_temp.cleanup()
            server.shutdown()
            thread.join()
            server.server_close()

    def test_real_claude_managed_hook_denies_target_but_allows_local_tools(self):
        self.with_hook = True
        self.test_real_claude_cli_can_use_model_channel_without_network()

    def test_real_claude_long_context_and_output_budget(self):
        self.long_context = True
        self.test_real_claude_cli_can_use_model_channel_without_network()

    def test_real_claude_retries_http_error_in_same_conversation(self):
        self.request_failure = 'http'
        self.test_real_claude_cli_can_use_model_channel_without_network()

    def test_real_claude_retries_stream_error_in_same_conversation(self):
        self.request_failure = 'stream'
        self.test_real_claude_cli_can_use_model_channel_without_network()

    def test_real_claude_retries_incomplete_stream_in_same_conversation(self):
        self.request_failure = 'eof'
        self.test_real_claude_cli_can_use_model_channel_without_network()


if __name__ == '__main__':
    unittest.main()
