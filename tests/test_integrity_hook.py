"""The hook must separate executable fetches from normal implementation work."""
import unittest

from claude_code.integrity_hook import reason


POLICY = {'target_distributions': ['retrying', 'python-box'],
          'target_modules': ['retrying', 'box']}


class HookTests(unittest.TestCase):
    def check_commands(self, commands, denied):
        for command in commands:
            with self.subTest(command=command):
                result = reason({'tool_name': 'Bash', 'tool_input': {'command': command}}, POLICY)
                self.assertEqual(result is not None, denied, result)

    def test_local_install_and_later_tests_are_not_package_arguments(self):
        self.check_commands([
            'python3 -m pip install --force-reinstall --no-deps /workspace 2>&1 | tail -1 '
            '&& echo "=== python retrying.py ===" && python3 retrying.py '
            '&& python3 retrying_test.py 2>&1 | tail -3',
            'pip install -e . && python -c "import retrying"',
            'pip install . || echo retrying',
            'pip install .; python retrying.py',
            'pip install .\npython retrying.py',
            'pip install . | tee retrying',
            'pip install . # retrying is the local module',
            'pip install --log retrying --target retrying requests',
            'pip install --report=retrying requests',
            'pip install . > retrying',
            'pip install . 2>> retrying',
            'pip install . > "retrying log"',
            'pip install -e ./retrying',
            'pip install --editable=/workspace/retrying',
            'pip install /tmp/retrying-1.0.whl',
            'pip install "retrying @ file:///workspace"',
            'pip show retrying',
            'echo "pip install retrying"',
            'printf "%s\\n" "pip install retrying"',
            'echo "&& pip install retrying"',
            'pip install retrying-tools',
            'pip install requests\n# pip install retrying',
            'bash -lc \'pip install -e . && python retrying.py\'',
        ], denied=False)

    def test_direct_target_install_and_download_remain_blocked(self):
        self.check_commands([
            'pip install retrying',
            'pip3 install "retrying>=1.0"',
            '/usr/local/bin/pip3.10 --disable-pip-version-check install retrying',
            'python3.10 -I -m pip install retrying',
            'pip --log output.log install retrying',
            'pip install --target /tmp/site retrying',
            'pip install --log retrying retrying',
            'pip download "RETRYING[extra]==1.3.4"',
            'pip install python_box',
            'pip install Python.Box',
            'pip install python__box',
            'pip install . && pip install retrying',
            'pip install . | pip download retrying',
            'pip install .\npip install retrying',
            'pip install \\\n retrying',
            '(pip install retrying)',
            'env PIP_DISABLE_PIP_VERSION_CHECK=1 python -m pip install retrying',
            'uv pip install retrying',
            'bash -lc "python -m pip install retrying"',
            'pip install "retrying @ https://example.com/package.whl"',
            'pip install https://example.com/retrying-1.3.4.tar.gz',
            'pip install --editable=git+https://github.com/example/retrying.git',
            'pip install -eretrying',
            'pip install --editable=retrying',
            'pip install retrying > install.log',
            'if true; then pip install retrying; fi',
            'true && (pip download retrying)',
            'pip install retrying <<EOF\nhello\nEOF',
            "bash <<'EOF'\npip install retrying\nEOF",
            "cat > install.sh <<'EOF'\npip install retrying\nEOF",
            'git clone https://github.com/example/retrying.git',
            'curl -L https://example.com/retrying/archive/main.zip',
            'wget https://example.com/retrying.py',
        ], denied=True)

    def test_pyperclip_trajectory_local_wheel_install_and_import(self):
        command = ('cd /tmp && python -m pip wheel /workspace --no-deps -w wheeltest '
                   '&& python -m pip install --no-deps --target wheeltest/site '
                   'wheeltest/pyperclip-*.whl -q && PYTHONPATH=wheeltest/site '
                   'python -c "import pyperclip; print(pyperclip.__file__)"')
        self.assertIsNone(reason({'tool_name': 'Bash', 'tool_input': {'command': command}},
                                 {'target_distributions': ['pyperclip']}))

    def test_download_is_not_associated_with_unrelated_url_or_comment(self):
        self.check_commands([
            'echo https://github.com/example/retrying; curl https://example.com/data',
            'curl https://example.com/data -o retrying.json',
            'curl https://example.com/data # https://example.com/retrying',
            'python -c \'url="https://example.com/retrying"; print(url)\'',
        ], denied=False)

    def test_python_fetch_calls_in_inline_code_and_generated_files(self):
        for source in [
            'import subprocess; subprocess.run(["python", "-m", "pip", "install", "retrying"])',
            'subprocess.run("pip install retrying", shell=True)',
            'subprocess.check_call(args=["pip3", "download", "python_box"])',
            'os.system("pip install retrying")',
            'import subprocess as sp; sp.run(["pip", "install", "retrying"])',
            'from subprocess import run; run(["pip", "install", "retrying"])',
            'import sys; subprocess.run([sys.executable, "-m", "pip", "install", "retrying"])',
            'urllib.request.urlopen("https://example.com/retrying/archive/main.zip")',
            'requests.get(url="https://example.com/retrying.py")',
        ]:
            for tool, value in [
                ('Write', {'file_path': '/workspace/setup.py', 'content': source}),
                ('Edit', {'file_path': '/workspace/setup.py', 'new_string': source}),
                ('Bash', {'command': "python -c '" + source + "'"}),
                ('Bash', {'command': "python - <<'PY'\n" + source + '\nPY\n'}),
            ]:
                with self.subTest(tool=tool, source=source):
                    self.assertIsNotNone(reason({'tool_name': tool, 'tool_input': value}, POLICY))

    def test_python_comments_docstrings_and_unrelated_calls_are_allowed(self):
        for source in [
            '# pip install retrying\nprint("ok")',
            '"""Example: pip install retrying"""\nprint("ok")',
            'HELP = "pip install retrying"',
            'print("pip install retrying")',
            'url = "https://example.com/retrying"\nrequests.get("https://example.com/data")',
            'subprocess.run(["python", "-m", "pip", "install", "/workspace"])',
            'subprocess.run("pip install . && python retrying.py", shell=True)',
            'def incomplete(',
        ]:
            with self.subTest(source=source):
                self.assertIsNone(reason({'tool_name': 'Write', 'tool_input': {
                    'file_path': 'setup.py', 'content': source}}, POLICY))
        self.check_commands([
            "python - <<'PY'\n# pip install retrying\nprint('ok')\nPY\n",
            "cat > README.md <<'EOF'\npip install retrying\nEOF",
            "cat > setup.py <<'PY'\nHELP = '''\npip install retrying\n'''\nPY",
        ], denied=False)

    def test_shell_scripts_checked_and_documentation_still_allowed(self):
        for suffix, denied in [('.sh', True), ('.md', False), ('.rst', False), ('.txt', False)]:
            with self.subTest(suffix=suffix):
                result = reason({'tool_name': 'Write', 'tool_input': {
                    'file_path': 'install' + suffix, 'content': 'pip install retrying'}}, POLICY)
                self.assertEqual(result is not None, denied)


if __name__ == '__main__':
    unittest.main()
