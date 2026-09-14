import json
import ast
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from claude_code.dependency_channel import dependency_channel
from docker_self import docker_service
from grading import post_processor as grader
from grading.grading_prepare import clean_dangling_editables


LOG = logging.getLogger(__name__)


class GradingLifecycleTests(unittest.TestCase):
    def test_conftest_missing_candidate_api_is_not_framework_failure(self):
        for output in ["ImportError while loading conftest '/workspace/tests/conftest.py'.\n"
                       "E ImportError: cannot import name 'StatefulToolEnv' from 'verifiers'",
                       "ImportError while loading conftest '/workspace/tests/conftest.py'.\n"
                       "E TypeError: <class 'verifiers.Protocol'> is not a generic class"]:
            result, _ = self.run_commands(['pytest'], [(4, output)], candidate_modules=['verifiers'])
            self.assertEqual(result['failure_kind'], 'candidate_error')
            self.assertTrue(result['pytest_results']['score_valid'], result)
            self.assertEqual(result['pytest_results']['passed'], 0)
        output = "ImportError while loading conftest '/workspace/tests/conftest.py'.\nE ModuleNotFoundError: No module named 'pytest_missing_plugin'"
        result, _ = self.run_commands(['pytest'], [(4, output)], candidate_modules=['verifiers'])
        self.assertFalse(result['pytest_results']['score_valid'])

    def setUp(self):
        diagnostics = patch.object(grader, 'collect_container_diagnostics', return_value={
            'state': {'Running': True, 'ExitCode': 0, 'OOMKilled': False}, 'logs': ''})
        self.diagnostics = diagnostics.start()
        self.addCleanup(diagnostics.stop)

    def test_container_helpers_parse_on_python_37(self):
        root = Path(__file__).resolve().parents[1]
        for name in ('claude_code/package_relay.py', 'grading/grading_prepare.py'):
            with self.subTest(name=name):
                ast.parse((root / name).read_text(), feature_version=(3, 7))

    def test_stopped_container_is_environment_error_and_diagnostics_precede_cleanup(self):
        evidence = {'state': {'Running': False, 'ExitCode': 1, 'OOMKilled': False},
                    'logs': 'SyntaxError: invalid syntax'}
        self.diagnostics.return_value = evidence
        order = []
        self.diagnostics.side_effect = lambda *args: order.append('diagnostics') or evidence
        with patch.object(grader, 'remove_container', side_effect=lambda *a, **k: order.append('remove')), \
                patch.object(grader, 'create_advanced_container', return_value=SimpleNamespace(id='test')), \
                patch.object(grader, 'execute_command_in_container', return_value=(1, 'container is not running')):
            result = grader.run_test_commands('image', 'container', ['pip install -e .', 'pytest'], 2, None, LOG)
        self.assertEqual(order, ['diagnostics', 'remove'])
        self.assertEqual(result['failure_kind'], 'environment_error')
        self.assertFalse(result['pytest_results']['score_valid'])
        self.assertEqual(result['container_diagnostics'], evidence)
        self.assertEqual(result['command_results'][1]['status'], 'not_run')

    def test_diagnostic_failure_does_not_prevent_cleanup_or_replace_test_result(self):
        self.diagnostics.side_effect = RuntimeError('inspect unavailable')
        result, _ = self.run_commands(['pytest'], [(0, '2 passed in 0.1s')])
        self.assertEqual(result['container_diagnostics']['collection_error'], 'inspect unavailable')
        self.assertTrue(result['pytest_results']['score_valid'])

    def test_creation_failure_retains_diagnostics_on_exception(self):
        self.diagnostics.return_value = {'logs': 'startup failed'}
        with patch.object(grader, 'create_advanced_container', side_effect=RuntimeError('start failed')), \
                patch.object(grader, 'remove_container') as remove, self.assertRaises(RuntimeError) as error:
            grader.run_test_commands('image', 'container', ['pytest'], 2, None, LOG)
        self.assertEqual(error.exception.container_diagnostics['logs'], 'startup failed')
        self.assertEqual(error.exception.container_failure_kind, 'environment_error')
        remove.assert_called_once_with(None, 'container', force=True)

    def test_collector_uses_state_only_and_retains_exit_logs(self):
        state = {'Running': False, 'ExitCode': 137, 'OOMKilled': True,
                 'Error': '', 'StartedAt': 'start', 'FinishedAt': 'end', 'Health': {'secret': 'omit'}}
        with patch.object(docker_service, 'create_docker_client', return_value=SimpleNamespace(
                docker_cmd=['docker', '--host', 'unix:///tmp/test.sock'])), \
                patch.object(docker_service.subprocess, 'run', side_effect=[
                    SimpleNamespace(returncode=0, stdout=json.dumps(state)),
                    SimpleNamespace(returncode=0, stdout='startup trace')]) as run:
            result = docker_service.collect_container_diagnostics(None, 'container')
        self.assertEqual(result['state']['ExitCode'], 137)
        self.assertTrue(result['state']['OOMKilled'])
        self.assertNotIn('Health', result['state'])
        self.assertEqual(result['logs'], 'startup trace')
        self.assertEqual(run.call_args_list[0].args[0][3:6], ['container', 'inspect', '--format'])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ['200', 'container'])

    def test_custom_artifact_install_preserves_official_commands_and_multiple_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            workspace.mkdir()
            (workspace / 'install.py').write_text('# custom installer')
            data = SimpleNamespace(proName='fixture', pyTestFileList=[],
                                   testShell=['python install.py', 'pytest tests'], testCaseCount=2)
            with patch.object(grader, 'build_test_image', return_value='image-id'), \
                    patch.object(grader, 'run_test_commands', side_effect=[
                        {'setup_status': 'failed'}, {'setup_status': 'failed',
                         'failure_kind': 'environment_error',
                         'pytest_results': {'passed': 0, 'score_valid': False}}]) as run:
                result = grader.post_process_task('custom', str(workspace), data, LOG,
                    generation_image='generation:1', base_image='grading:1', allow_base_image_tag=True,
                    install_commands=['python install.py'])
            self.assertEqual(run.call_args_list[0].args[2], ['python install.py'])
            self.assertEqual(run.call_args_list[1].args[2], data.testShell)
            self.assertEqual(result['failures'], [
                {'kind': 'evaluation_error', 'stage': 'artifact_install'},
                {'kind': 'environment_error', 'stage': 'grading_setup'}])
            self.assertTrue((workspace / 'install.py').exists())

    def test_dangling_metadata_cleanup_preserves_live_dependencies_and_pth_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / 'live-source'
            live.mkdir()
            (root / 'live.egg-link').write_text('live-source\n.')
            (root / 'dead.egg-link').write_text('/missing-nl2repo-reference\n.')
            (root / 'relative.egg-link').write_text('missing-relative\n.')
            preserved = '# comment\nlive-source\nimport sys; pass\nunrelated-path\n'
            pth = root / 'easy-install.pth'
            pth.write_text('/missing-nl2repo-reference\nmissing-relative\n' + preserved)
            changes = clean_dangling_editables([directory])
            self.assertEqual(len(changes), 3)
            self.assertFalse((root / 'dead.egg-link').exists())
            self.assertFalse((root / 'relative.egg-link').exists())
            self.assertTrue((root / 'live.egg-link').exists())
            self.assertEqual(pth.read_text(), preserved)
            self.assertEqual(clean_dangling_editables([directory]), [])

    def run_commands(self, commands, effects, **limits):
        with patch.object(grader, 'create_advanced_container', return_value=SimpleNamespace(id='test')), \
                patch.object(grader, 'execute_command_in_container', side_effect=effects) as execute, \
                patch.object(grader, 'remove_container') as remove:
            result = grader.run_test_commands('image', 'container', commands, 2, None, LOG, **limits)
        remove.assert_called_once_with(None, 'container', force=True)
        return result, execute

    def test_install_failure_prevents_testing(self):
        result, execute = self.run_commands(['pip install -e .', 'pytest'], [(1, 'install failed')])
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(result['setup_status'], 'failed')
        self.assertEqual(result['command_results'][1]['status'], 'not_run')
        self.assertFalse(result['pytest_results']['score_valid'])
        self.assertEqual(result['failure_kind'], 'candidate_error')

    def test_framework_setup_and_dependency_transport_failure_are_not_candidate_errors(self):
        for command, output in [('cp /missing-tests /workspace', 'missing benchmark tests'),
                                ('pip install -e .', 'Retrying (Retry(total=0)): Connection refused'),
                                ('pip install -e .', 'No space left on device')]:
            with self.subTest(command=command, output=output):
                result, _ = self.run_commands([command, 'pytest'], [(1, output)])
                self.assertNotEqual(result.get('failure_kind'), 'candidate_error')
        result, _ = self.run_commands(['pytest', 'cp /missing /workspace'],
            [(1, '1 failed, 1 passed in 0.1s'), (1, 'missing benchmark file')])
        self.assertNotEqual(result.get('failure_kind'), 'candidate_error')

    def test_candidate_errors_complete_evaluation_but_infrastructure_errors_do_not(self):
        cases = [
            (['pytest'], [(1, 'collected 18 items / 1 error\n18 passed, 1 error in 1.76s')], True),
            (['pytest'], [(2, 'collected 0 items / 1 error\n1 error in 0.1s')], True),
            (['pip install -e .', 'pytest'], [(1, 'invalid submitted metadata')], True),
            (['pytest'], [(3, 'INTERNALERROR\n1 error in 0.1s')], False),
            (['pytest'], [RuntimeError('Docker exec failed')], False),
            (['pytest'], [subprocess.TimeoutExpired('pytest', 1)], False),
        ]
        for commands, effects, valid in cases:
            with self.subTest(effects=effects), tempfile.TemporaryDirectory() as directory:
                tests, _ = self.run_commands(commands, effects)
                workspace = Path(directory) / 'workspace'
                workspace.mkdir()
                data = SimpleNamespace(proName='fixture', pyTestFileList=[], testShell=commands, testCaseCount=23)
                with patch.object(grader, 'build_test_image', return_value='image'), \
                        patch.object(grader, 'run_test_commands', side_effect=[
                            {'setup_status': 'success'}, tests]):
                    result = grader.post_process_task('fixture', str(workspace), data, LOG,
                        generation_image='generation:1', base_image='grading:1', allow_base_image_tag=True)
                self.assertEqual(result['evaluation_valid'], valid, result)
                self.assertEqual(result['status'], 'success' if valid else 'error')
                self.assertEqual(bool(result['failures']), not valid)
                self.assertEqual(bool(result['candidate_failures']), valid)

    def test_candidate_packaging_failure_does_not_invalidate_completed_grading(self):
        install, _ = self.run_commands(['python custom_install.py'], [(1, 'invalid metadata')], candidate_setup=True)
        tests, _ = self.run_commands(['pytest'], [(0, '2 passed in 0.1s')])
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            workspace.mkdir()
            data = SimpleNamespace(proName='fixture', pyTestFileList=[], testShell=['pytest'], testCaseCount=2)
            with patch.object(grader, 'build_test_image', return_value='image'), \
                    patch.object(grader, 'run_test_commands', side_effect=[install, tests]):
                result = grader.post_process_task('fixture', str(workspace), data, LOG,
                    generation_image='generation:1', base_image='grading:1', allow_base_image_tag=True)
        self.assertEqual(result['status'], 'success')
        self.assertTrue(result['evaluation_valid'])
        self.assertTrue(result['score_valid'])
        self.assertEqual(result['artifact_install']['status'], 'failed')
        self.assertEqual(result['candidate_failures'], [{'kind': 'candidate_error', 'stage': 'artifact_install'}])

    def test_timeout_preserves_output_and_stops_following_commands(self):
        for command in ('pip install -e .', 'pytest'):
            with self.subTest(command=command):
                result, execute = self.run_commands([command, 'pytest later'], [
                    subprocess.TimeoutExpired(command, 1, output=b'partial output\n')])
                self.assertEqual(result['execution_status'], 'timeout')
                self.assertEqual(result['command_results'][0]['output'], 'partial output\n')
                self.assertEqual(result['command_results'][1]['status'], 'not_run')
                self.assertFalse(result['pytest_results']['score_valid'])
                self.assertEqual(execute.call_count, 1)

    def test_stage_deadline_is_shared_across_commands(self):
        with patch.object(grader.time, 'monotonic', side_effect=[10, 12, 18]):
            result, execute = self.run_commands(['pip install -e .', 'pytest'],
                [(0, 'installed'), (0, '2 passed in 0.1s')], timeout_seconds=10, setup_timeout_seconds=3)
        self.assertEqual([call.kwargs['timeout_seconds'] for call in execute.call_args_list], [3, 2])
        self.assertTrue(result['pytest_results']['score_valid'])

    def test_cleanup_runs_when_analysis_raises(self):
        with patch.object(grader, 'analyze_pytest_results', side_effect=RuntimeError('bad output')), \
                patch.object(grader, 'create_advanced_container'), \
                patch.object(grader, 'remove_container') as remove, self.assertRaises(RuntimeError):
            grader.run_test_commands('image', 'container', [], 0, None, LOG)
        remove.assert_called_once_with(None, 'container', force=True)

    def test_host_timeout_keeps_real_exit_code_and_raw_output(self):
        client = SimpleNamespace(docker_cmd=['docker', '--host', 'unix:///tmp/test.sock'])
        with patch.object(docker_service, 'create_docker_client', return_value=client), \
                patch.object(docker_service.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=3, stdout='2 passed\nINTERNALERROR')) as run:
            code, output = docker_service.execute_command_in_container(
                None, 'container', 'python -m pytest', workdir='/workspace', timeout_seconds=2)
        self.assertEqual(code, 3)
        self.assertEqual(output, '2 passed\nINTERNALERROR')
        self.assertEqual(run.call_args.kwargs['timeout'], 2)
        self.assertEqual(run.call_args.args[0][:3], client.docker_cmd)

    def test_staging_preserves_artifact_and_validity_tracks_install(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            workspace.mkdir()
            (workspace / 'pyproject.toml').write_text('submitted packaging')
            (workspace / 'tests').mkdir()
            (workspace / 'tests' / 'test_self.py').write_text('submitted tests')
            data = SimpleNamespace(proName='fixture', pyTestFileList=['tests'],
                                   testShell=['pip install -e .', 'pytest'], testCaseCount=2)
            builds = []

            def build(path, *args, **kwargs):
                root = Path(path).parent
                builds.append((Path(path).read_text(), (root / 'workspace/pyproject.toml').exists()))
                return 'image-id'

            with patch.object(grader, 'build_test_image', side_effect=build), \
                    patch.object(grader, 'run_test_commands', side_effect=[
                        {'setup_status': 'failed'}, {'setup_status': 'success',
                         'pytest_results': {'passed': 2, 'score_valid': True}}]) as run:
                result = grader.post_process_task('fixture', str(workspace), data, LOG,
                    generation_image='generation:1', base_image='grading:1', allow_base_image_tag=True)
            self.assertEqual(result['status'], 'error')
            self.assertEqual(run.call_args_list[0].args[2], ['python -m pip install -e .'])
            self.assertFalse(result['evaluation_valid'])
            self.assertTrue(result['score_valid'])
            self.assertEqual(result['failure_stage'], 'artifact_install')
            self.assertEqual((workspace / 'pyproject.toml').read_text(), 'submitted packaging')
            self.assertTrue((workspace / 'tests/test_self.py').exists())
            self.assertEqual([exists for _, exists in builds], [True, False])
            self.assertIn('ENTRYPOINT []', builds[0][0])
            self.assertIn('COPY --chown=1000:1000', builds[0][0])
            self.assertNotIn('grading_prepare.py', builds[0][0])
            self.assertLess(builds[1][0].index('RUN python -I -S'), builds[1][0].index('COPY workspace'))

    def test_image_build_timeout_is_reported_with_partial_output(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / 'workspace'
            workspace.mkdir()
            (workspace / 'setup.py').write_text('# retained')
            data = SimpleNamespace(proName='fixture')
            with patch.object(grader, 'build_test_image', side_effect=
                    subprocess.TimeoutExpired('docker build', 1, output=b'partial build')):
                result = grader.post_process_task('timeout', str(workspace), data, LOG,
                    generation_image='generation:1', allow_base_image_tag=True)
            self.assertEqual(result['failure_stage'], 'artifact_install')
            self.assertEqual(result['artifact_install']['status'], 'timeout')
            self.assertEqual(result['output'], 'partial build')
            self.assertFalse(result['evaluation_valid'])
            self.assertTrue((workspace / 'setup.py').exists())


@unittest.skipUnless(os.environ.get('NL2REPO_TEST_DOCKER') == '1', 'opt-in Docker integration')
class RealInstallTests(unittest.TestCase):
    def test_python37_relay_and_failed_startup_diagnostics(self):
        image = os.environ.get('NL2REPO_TEST_PY37_IMAGE',
            'ghcr.io/multimodal-art-projection/nl2repobench/cherry:1.0')
        host = grader.DockerHostInfo('localhost')
        policy = {'target_distributions': ['cherry'], 'target_modules': ['cherry']}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = str(Path(__file__).resolve().parents[1] / 'claude_code/package_relay.py')
            name = 'nl2repo-py37-test-' + uuid.uuid4().hex
            with dependency_channel(policy, root / 'dependencies.jsonl') as channel:
                probe = '''import os, sys, time, urllib.request, urllib.error
assert sys.version_info[:2] == (3, 7), sys.version
for attempt in range(20):
    try:
        urllib.request.urlopen(os.environ['PIP_INDEX_URL'] + 'cherry/', timeout=1)
    except urllib.error.HTTPError as exc:
        assert exc.code == 403, exc.code
        break
    except urllib.error.URLError:
        if attempt == 19: raise
        time.sleep(.1)
    else: raise AssertionError('Target dependency was not blocked')
print('PY37_RELAY_OK')
'''
                import shlex
                result = grader.run_test_commands(image, name,
                    ['python -c ' + shlex.quote(probe)], 0, host, LOG, isolated=True,
                    dependency_access=(*channel, relay), timeout_seconds=30)
                self.assertEqual(result['setup_status'], 'success', result)
                self.assertIn('PY37_RELAY_OK', result['command_results'][0]['output'])
                self.assertTrue(result['container_diagnostics']['state']['Running'])
                broken = root / 'broken-relay.py'
                # Reproduce the original Python 3.7 container startup failure.
                broken.write_text('while chunk := b"x":\n    pass\n')
                result = grader.run_test_commands(image, name + '-broken', ['python -V', 'pytest'],
                    1, host, LOG, isolated=True, dependency_access=(*channel, str(broken)),
                    timeout_seconds=30)
                self.assertEqual(result['failure_kind'], 'environment_error', result)
                self.assertFalse(result['container_diagnostics']['state']['Running'])
                self.assertFalse(result['container_diagnostics']['state']['OOMKilled'])
                self.assertIn('SyntaxError', result['container_diagnostics']['logs'])
                for container in (name, name + '-broken'):
                    checked = subprocess.run(['docker', 'container', 'inspect', container],
                                             capture_output=True, timeout=10)
                    self.assertNotEqual(checked.returncode, 0)

    def test_real_install_permissions_relay_and_timeout(self):
        host = grader.DockerHostInfo('localhost')
        for image in ('nl2repo-claude-code:2.1.263', 'nl2repo-claude-code:2.1.263-py3.10'):
            with self.subTest(image=image), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory) / 'workspace'
                workspace.mkdir()
                (workspace / 'pyproject.toml').write_text(
                    '[build-system]\nrequires=[]\nbuild-backend="backend"\nbackend-path=["."]\n')
                # Self-contained PEP 660 backend: no network or preinstalled setuptools.
                (workspace / 'backend.py').write_text('''from pathlib import Path
import zipfile
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    Path("installation-marker").write_text("installed")
    name = "fixture_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(Path(wheel_directory) / name, "w") as wheel:
        wheel.writestr("fixture_probe.pth", str(Path.cwd()) + "\\n")
        wheel.writestr("fixture_probe-1.0.dist-info/METADATA", "Metadata-Version: 2.1\\nName: fixture-probe\\nVersion: 1.0\\n")
        wheel.writestr("fixture_probe-1.0.dist-info/WHEEL", "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        wheel.writestr("fixture_probe-1.0.dist-info/RECORD", "")
    return name
''')
                name = 'nl2repo-install-test-' + uuid.uuid4().hex
                policy = {'target_distributions': ['forbidden-target'], 'target_modules': ['forbidden_target']}
                try:
                    dockerfile = grader.create_dockerfile(str(workspace), 'unused', LOG,
                                                         base_image=image, artifact_install=True)
                    grader.build_test_image(dockerfile, name, host, LOG)
                    with dependency_channel(policy, Path(directory) / 'audit.jsonl') as channel:
                        relay = str(Path(__file__).resolve().parents[1] / 'claude_code/package_relay.py')
                        probe = '''import pathlib, urllib.request, urllib.error, os
assert pathlib.Path("installation-marker").read_text() == "installed"
try: urllib.request.urlopen(os.environ["PIP_INDEX_URL"] + "forbidden-target/")
except urllib.error.HTTPError as exc: assert exc.code == 403
else: raise AssertionError("relay did not enforce package policy")
print("INSTALL_AND_RELAY_OK")
'''
                        import shlex
                        result = grader.run_test_commands(name, name,
                            ['python -m pip install -e .', 'python -c ' + shlex.quote(probe)], 0, host, LOG,
                            isolated=True, dependency_access=(*channel, relay), timeout_seconds=45)
                    self.assertEqual(result['setup_status'], 'success', result)
                    self.assertIn('INSTALL_AND_RELAY_OK', result['command_results'][-1]['output'])
                    timeout_name = name + '-timeout'
                    result = grader.run_test_commands(name, timeout_name,
                        ['python -u -c "import time; print(123, flush=True); time.sleep(120)"'],
                        0, host, LOG, isolated=True, timeout_seconds=2)
                    self.assertEqual(result['execution_status'], 'timeout', result)
                    self.assertIn('123', result['command_results'][0]['output'])
                    checked = subprocess.run(['docker', 'container', 'inspect', timeout_name],
                                             capture_output=True, timeout=10)
                    self.assertNotEqual(checked.returncode, 0)
                finally:
                    subprocess.run(['docker', 'rm', '-f', name, name + '-timeout'], capture_output=True, timeout=15)
                    subprocess.run(['docker', 'image', 'rm', name], capture_output=True, timeout=30)


if __name__ == '__main__':
    unittest.main()
