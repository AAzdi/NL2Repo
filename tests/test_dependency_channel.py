import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from claude_code.dependency_channel import WheelBroker, dependency_channel
from claude_code.integrity_hook import reason


POLICY = {'target_distributions': ['aiofiles', 'python-box'], 'target_modules': ['aiofiles', 'box']}


def wheel(name='bench_safe_dependency', requirements=(), extra=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        metadata = f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n'
        metadata += ''.join(f'Requires-Dist: {r}\n' for r in requirements)
        z.writestr(f'{name}-1.0.dist-info/METADATA', metadata)
        z.writestr(f'{name}-1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: benchmark-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        z.writestr(f'{name}-1.0.dist-info/RECORD', '')
        z.writestr(f'{name}.py', 'VALUE = "DEPENDENCY_OK"\n')
        if extra:
            z.writestr(extra, '# fixture\n')
    return buffer.getvalue()


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.broker = WheelBroker(POLICY, Path(self.temp.name) / 'audit.jsonl')

    def register(self, body):
        digest = hashlib.sha256(body).hexdigest()
        self.broker.artifacts[digest] = ('bench-safe-dependency', 'safe.whl', 'https://files.pythonhosted.org/safe.whl')
        return digest

    def test_target_aliases_blocked_before_any_fetch(self):
        with patch.object(self.broker, 'fetch') as fetch:
            for name in ('aiofiles', 'python_box', 'python.box'):
                with self.assertRaises(ValueError):
                    self.broker.index(name)
            fetch.assert_not_called()

    def test_valid_wheel_and_tampered_or_embedded_target_rejected(self):
        valid = wheel()
        digest = self.register(valid)
        with patch.object(self.broker, 'fetch', side_effect=lambda *a, **k: k['destination'].write(valid)):
            self.assertEqual(self.broker.wheel(digest, 'safe.whl'), valid)
        for body in (valid + b'tampered', wheel(extra='vendor/aiofiles/__init__.py'),
                     wheel(extra='../escape'), wheel(requirements=['x @ https://bad/source.whl'])):
            expected = digest if body.endswith(b'tampered') else self.register(body)
            with patch.object(self.broker, 'fetch', side_effect=lambda *a, **k: k['destination'].write(body)), self.assertRaises(ValueError):
                self.broker.wheel(expected, 'safe.whl')

    def test_destination_and_unknown_artifacts_rejected(self):
        for url in ('http://files.pythonhosted.org/x', 'https://bad/x',
                    'https://files.pythonhosted.org:443/x', 'file:///etc/passwd'):
            with self.assertRaises(ValueError):
                self.broker.fetch(url, 100)
        with self.assertRaises(ValueError):
            self.broker.wheel('a' * 64, 'anything.whl')

    def test_index_cache_deduplicates_repeated_metadata_fetches(self):
        body = json.dumps({'name': 'numpy', 'files': []}).encode()
        with patch.object(self.broker, 'fetch', return_value=body) as fetch:
            self.assertEqual(self.broker.index('numpy'), self.broker.index('numpy'))
            self.assertEqual(fetch.call_count, 1)

    def test_streaming_fetch_bounds_memory_and_size(self):
        class Response(io.BytesIO):
            def read(self, count=-1):
                self.assert_count(count)
                return super().read(count)
            def assert_count(self, count):
                if not 0 < count <= 1024 * 1024:
                    raise AssertionError('unbounded read')
        with patch.object(self.broker.opener, 'open', return_value=Response(b'abcdef')):
            output = io.BytesIO()
            self.assertEqual(self.broker.fetch('https://files.pythonhosted.org/a', 6,
                                              destination=output), 6)
            self.assertEqual(output.getvalue(), b'abcdef')
        with patch.object(self.broker.opener, 'open', return_value=Response(b'abcdef')):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.broker.fetch('https://files.pythonhosted.org/a', 5, destination=io.BytesIO())

    def test_large_wheel_limit_is_used_without_buffering_http_response(self):
        body = wheel()
        digest = self.register(body)
        def download(url, limit, *, destination):
            self.assertEqual(limit, 2 * 1024**3)
            return destination.write(body)
        with patch.object(self.broker, 'fetch', side_effect=download):
            with self.broker.open_wheel(digest, 'safe.whl') as (stream, size):
                self.assertEqual(size, len(body))
                self.assertEqual(stream.read(), body)

    def test_hook_allows_unrelated_dependencies_and_local_project(self):
        for command in ('pip install requests pytest', 'python -m pip install -e .',
                        'pip install -e ./aiofiles',
                        'python -c "import aiofiles"', 'pytest tests'):
            self.assertIsNone(reason({'tool_name': 'Bash', 'tool_input': {'command': command}}, POLICY))
        self.assertIsNone(reason({'tool_name': 'Write', 'tool_input': {
            'file_path': 'client.py', 'content': '# aiofiles helper\nrequests.get("https://example.com/data")'}}, POLICY))
        for tool, value in [('Bash', {'command': 'pip install aiofiles==24.1.0'}),
                            ('Bash', {'command': 'git clone https://github.com/Tinche/aiofiles'}),
                            ('Write', {'content': "import urllib.request; urllib.request.urlopen('https://github.com/Tinche/aiofiles/archive/main.zip')"}),
                            ('Edit', {'new_string': 'subprocess.run("pip install python_box", shell=True)'})]:
            self.assertIsNotNone(reason({'tool_name': tool, 'tool_input': value}, POLICY))


@unittest.skipUnless(os.environ.get('NL2REPO_TEST_DOCKER') == '1', 'opt-in Docker integration')
class DependencyDockerTests(unittest.TestCase):
    def test_pip_install_allowed_and_direct_transitive_target_denied(self):
        bodies = {'bench-safe-dependency': wheel(),
                  'bench-parent': wheel('bench_parent', ['aiofiles>=1'])}
        requested = []

        def fetch(broker, url, limit, *, metadata=False, destination=None):
            if metadata:
                name = url.rstrip('/').split('/')[-1]
                requested.append(name)
                body = bodies[name]
                filename = name.replace('-', '_') + '-1.0-py3-none-any.whl'
                return json.dumps({'name': name, 'files': [{'filename': filename,
                    'url': 'https://files.pythonhosted.org/' + name,
                    'hashes': {'sha256': hashlib.sha256(body).hexdigest()}}]}).encode()
            return destination.write(bodies[url.rsplit('/', 1)[-1]])

        with tempfile.TemporaryDirectory() as directory, patch.object(WheelBroker, 'fetch', fetch):
            with dependency_channel(POLICY, Path(directory) / 'audit.jsonl') as (socket_dir, env):
                relay = Path(__file__).resolve().parents[1] / 'claude_code/package_relay.py'
                script = '''import subprocess, socket
assert [n for _,n in socket.if_nameindex()] == ["lo"]
def install(name):
 return subprocess.run(["python", "-m", "pip", "install", "--user", "--no-cache-dir", "--retries", "0", name], capture_output=True, text=True)
p=install("bench-safe-dependency==1.0")
assert p.returncode == 0, p.stdout+p.stderr
subprocess.run(["python", "-c", "import bench_safe_dependency; assert bench_safe_dependency.VALUE == 'DEPENDENCY_OK'"], check=True)
for name in ["aiofiles", "python_box", "bench-parent"]:
 p=install(name)
 assert p.returncode != 0, name
print("PASS: unrelated wheel installs; direct, alias, transitive targets blocked")
'''
                command = ['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                           '--security-opt', 'no-new-privileges', '--user', '1000:1000',
                           '--volume', f'{socket_dir}:/run/nl2repo-packages:ro',
                           '--volume', f'{relay}:/opt/relay.py:ro', '--entrypoint', 'python']
                for name in env:
                    command.extend(['--env', name])
                command += ['nl2repo-claude-code:2.1.263', '-I', '/opt/relay.py', 'python', '-c', script]
                result = subprocess.run(command, env={**os.environ, **env}, capture_output=True,
                                        text=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('aiofiles', requested)
        self.assertNotIn('python-box', requested)


if __name__ == '__main__':
    unittest.main()
