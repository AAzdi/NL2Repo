import json
from contextlib import contextmanager
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_data_service

from claude_code import runner
from grading import post_processor as grader


class ClaudeCodeTests(unittest.TestCase):
    def test_incremental_prompt_preserves_requirements_and_integrity_rules(self):
        self.assertEqual(runner.task_prompt({}), runner.PROMPT)
        prompt = runner.task_prompt({'incremental': True})
        self.assertTrue(prompt.startswith(runner.PROMPT))
        self.assertIn('using Write or Edit', prompt)
        self.assertIn('Complete all requirements', prompt)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        previous = os.getcwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, previous)
        Path('start.md').write_text('Implement the task.')
        self.data = SimpleNamespace(proName='six', md=str(Path('start.md').resolve()))
        self.pro = dict(moduleName='repo-model', baseUrl='http://gpu:30000', sk='secret')
        # Unit tests model a regular non-root host; container ownership is an
        # integration concern and cannot be changed in some CI filesystems.
        uid = patch.object(runner.os, 'getuid', return_value=1000)
        uid.start()
        self.addCleanup(uid.stop)
        offline = patch.object(runner, 'offline_environment', return_value={
            'generation_image': 'sha256:' + 'a' * 64,
            'grading_image': 'sha256:' + 'b' * 64})
        offline.start()
        self.addCleanup(offline.stop)

        @contextmanager
        def channel(environment, **kwargs):
            yield '/tmp/test-model-channel', {**environment,
                'ANTHROPIC_BASE_URL': 'http://127.0.0.1:18080',
                'ANTHROPIC_AUTH_TOKEN': 'task-channel-token'}
        relay = patch.object(runner, 'model_channel', side_effect=channel)
        relay.start()
        self.addCleanup(relay.stop)
        @contextmanager
        def packages(*args):
            yield '/tmp/test-packages', {'PIP_NO_INDEX': '0', 'PIP_INDEX_URL': 'http://127.0.0.1:18081/test/simple/'}
        dependencies = patch.object(runner, 'dependency_channel', side_effect=packages)
        dependencies.start()
        self.addCleanup(dependencies.stop)
        # Container execution is mocked here; real pipe/timeout/cancellation
        # behavior is covered with local child processes in test_lifecycle.py.
        def generation(command, **kwargs):
            kwargs.pop('cancel', None)
            return runner.subprocess.run(command, **kwargs)
        generation_patch = patch.object(runner, 'run_generation', side_effect=generation)
        generation_patch.start()
        self.addCleanup(generation_patch.stop)

    def fake_docker(self, event, returncode=0):
        def run(command, **kwargs):
            if command[1] == 'run':
                kwargs['stdout'].write(json.dumps(event) + '\n')
                self.assertNotIn('secret', ' '.join(command))
                self.assertEqual(kwargs['env']['ANTHROPIC_AUTH_TOKEN'], 'task-channel-token')
                self.assertEqual(command[command.index('--network') + 1], 'none')
                self.assertEqual(command[command.index('--cap-drop') + 1], 'ALL')
                self.assertIn('no-new-privileges', command)
                self.assertNotIn('--add-host', command)
                self.assertNotIn('/var/run/docker.sock', ' '.join(command))
                self.assertEqual(Path(command[command.index('--volume') + 1].split(':')[0],
                                      'start.md').read_text(), 'Implement the task.')
            return SimpleNamespace(returncode=returncode)
        return run

    def test_success_and_distinct_workspaces(self):
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(
                dict(type='result', subtype='success', is_error=False, stop_reason='end_turn'))), \
             patch.object(runner, 'evaluate', return_value={
                 'status': 'success', 'score_valid': True, 'artifact_install': {'status': 'success'},
                 'pytest_results': {'passed': 3}}):
            first = runner.run_task(self.pro, self.data, {})
            second = runner.run_task(self.pro, self.data, {})
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['score'], 3)
        self.assertNotEqual(first['workspace_path'], second['workspace_path'])
        saved = Path('result', first['task_uuid'] + '.json').read_text()
        self.assertNotIn('secret', saved)
        self.assertEqual(json.loads(saved)['exit_code'], 0)
        readable = Path(first['trajectory_readable_path']).read_text()
        self.assertTrue(first['trajectory_readable_path'].endswith('/trajectory.json'))
        self.assertEqual(json.loads(readable)['generation_status'], 'success')
        self.assertGreater(len(readable.splitlines()), 1)
        self.assertEqual(first['trajectory_path'], first['trajectory_readable_path'])
        self.assertEqual(json.loads(readable)['messages'][0]['content'][0]['text'], runner.PROMPT)
        self.assertEqual(list(Path(first['workspace_path']).parent.glob('*.jsonl')), [])
        self.assertNotIn('trajectory_raw_path', first)
        self.assertTrue(first['evaluation_valid'])

    def test_incremental_prompt_reaches_container_and_saved_trajectory(self):
        event = dict(type='result', subtype='success', is_error=False, stop_reason='end_turn')
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event)) as run, \
                patch.object(runner, 'evaluate', return_value={'status': 'success', 'score_valid': True,
                    'artifact_install': {'status': 'success'}}):
            result = runner.run_task(self.pro, self.data, {'incremental': True})
        command = next(call.args[0] for call in run.call_args_list if call.args[0][1] == 'run')
        sent = command[command.index('-p') + 1]
        saved = json.loads(Path(result['trajectory_path']).read_text())['messages'][0]['content'][0]['text']
        self.assertEqual(sent, saved)
        self.assertIn('EXECUTION GUIDANCE', saved)
        self.assertIn('BENCHMARK INTEGRITY RULES', saved)

    def test_results_are_published_only_after_evaluation(self):
        task_id = 'publication-test'
        state = runner.initial_result(self.pro, self.data, {}, task_id)
        result_path = Path('result', task_id + '.json')
        state_path = Path(state['workspace_path']).parent / 'task_state.json'
        self.assertFalse(result_path.exists())
        self.assertEqual(json.loads(state_path.read_text())['status'], 'queued')
        observed = []

        def prepare(*args):
            self.assertFalse(result_path.exists())
            observed.append(json.loads(state_path.read_text())['stage'])
            return {'generation_image': 'generation', 'grading_image': 'grading'}

        fake = self.fake_docker(dict(type='result', subtype='success', is_error=False, stop_reason='end_turn'))

        def generate(command, **kwargs):
            self.assertFalse(result_path.exists())
            if command[1] == 'run':
                observed.append(json.loads(state_path.read_text())['stage'])
            return fake(command, **kwargs)

        def evaluate(*args, **kwargs):
            self.assertFalse(result_path.exists())
            observed.append(json.loads(state_path.read_text())['stage'])
            return {'status': 'success', 'evaluation_valid': True, 'score_valid': True,
                    'artifact_install': {'status': 'success'}, 'pytest_results': {'passed': 18, 'errors': 1}}

        with patch.object(runner, 'offline_environment', side_effect=prepare), \
                patch.object(runner.subprocess, 'run', side_effect=generate), \
                patch.object(runner, 'evaluate', side_effect=evaluate):
            result = runner.run_task(self.pro, self.data, {}, task_id=task_id)
        self.assertEqual(observed, ['preparing', 'generation', 'evaluation'])
        self.assertEqual(json.loads(result_path.read_text()), result)
        self.assertEqual(json.loads(state_path.read_text()), result)
        self.assertEqual(result['status'], 'completed')
        self.assertIn('evaluation_finished_at', result)

    def test_candidate_install_failure_completes_without_a_valid_test_score(self):
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(
                dict(type='result', subtype='success', is_error=False, stop_reason='end_turn'))), \
                patch.object(runner, 'evaluate', return_value={
                    'status': 'success', 'evaluation_valid': True, 'score_valid': False,
                    'artifact_install': {'status': 'failed'}, 'pytest_results': {'passed': 0},
                    'candidate_failures': [{'kind': 'candidate_error', 'stage': 'artifact_install'}]}):
            result = runner.run_task(self.pro, self.data, {})
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(result['evaluation_valid'])
        self.assertFalse(result['score_valid'])
        self.assertNotIn('evaluation_failures', result)
        self.assertNotIn('failure_kind', result)
        self.assertEqual(result['candidate_failures'][0]['kind'], 'candidate_error')

    def test_failed_or_missing_result_is_not_success(self):
        for event, code in [(dict(type='result', subtype='error_max_turns', is_error=True), 0),
                            (dict(type='assistant'), 0),
                            (dict(type='result', subtype='success'), 1)]:
            with self.subTest(event=event, code=code), \
                 patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event, code)), \
                 patch.object(runner, 'evaluate', return_value={'status': 'success', 'score_valid': True,
                    'artifact_install': {'status': 'success'}}) as evaluate:
                result = runner.run_task(self.pro, self.data, {})
                self.assertEqual(result['status'], 'failed')
                if code:
                    self.assertEqual(json.loads(Path(result['trajectory_path']).read_text())['generation_status'],
                                     'failed')
                evaluate.assert_called_once()

    def test_truncated_success_is_failed_in_result_and_trajectory(self):
        for stop_reason in (None, 'tool_use', 'max_tokens'):
            event = dict(type='result', subtype='success', is_error=False,
                         stop_reason=stop_reason, result='Now I will write the core module.')
            with self.subTest(stop_reason=stop_reason), \
                 patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event)), \
                 patch.object(runner, 'evaluate', return_value={
                     'status': 'success', 'pytest_results': {'passed': 1}}):
                result = runner.run_task(self.pro, self.data, {})
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['generation_status'], 'failed')
            self.assertEqual(result['score'], 1)
            self.assertIn('stop_reason', result['generation_error'])
            saved = json.loads(Path(result['trajectory_path']).read_text())
            self.assertEqual(saved['generation_status'], 'failed')
            self.assertEqual(saved['result']['stop_reason'], stop_reason)

    def test_timeout_stops_container_without_scoring(self):
        def run(command, **kwargs):
            if command[1] == 'run':
                kwargs['stdout'].write(json.dumps({'type': 'assistant', 'message': {
                    'content': [{'type': 'text', 'text': 'Partial work before timeout'}]}}) + '\n')
                raise subprocess.TimeoutExpired(command, 1)
            return SimpleNamespace(returncode=0)
        with patch.object(runner.subprocess, 'run', side_effect=run) as docker, \
             patch.object(runner, 'evaluate') as evaluate:
            result = runner.run_task(self.pro, self.data, {'timeout_seconds': 1})
        self.assertEqual(result['generation_status'], 'timeout')
        self.assertEqual(result['failure_kind'], 'generation_timeout')
        self.assertEqual(result['failure_stage'], 'generation')
        self.assertEqual(docker.call_args.args[0][:3], ['docker', 'rm', '-f'])
        evaluate.assert_not_called()
        self.assertFalse(Path('result', result['task_uuid'] + '.json').exists())
        self.assertTrue(Path(result['workspace_path']).parent.joinpath('task_state.json').exists())
        readable = Path(result['trajectory_readable_path']).read_text()
        self.assertIn('Partial work before timeout', readable)
        self.assertEqual(json.loads(readable)['generation_status'], 'timeout')
        self.assertNotIn('trajectory_raw_path', result)
        self.assertFalse(Path(result['workspace_path']).parent.joinpath('trajectory.jsonl').exists())
        self.assertFalse(result['evaluation_valid'])

    def test_api_error_without_http_status_retains_evaluation_failures(self):
        event = dict(type='result', subtype='success', is_error=True,
                     terminal_reason='api_error', api_error_status=None,
                     result='API Error: Server error mid-response.')
        failures = [{'kind': 'evaluation_error', 'stage': 'artifact_install'},
                    {'kind': 'environment_error', 'stage': 'grading_setup'}]
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event, returncode=1)), \
                patch.object(runner, 'evaluate', return_value={'status': 'error',
                    'score_valid': False, 'artifact_install': {'status': 'failed'},
                    'failure_stage': 'artifact_install', 'failures': failures}):
            result = runner.run_task(self.pro, self.data, {})
        self.assertEqual(result['failure_kind'], 'upstream_api_error')
        self.assertEqual(result['failure_stage'], 'generation')
        self.assertEqual(result['evaluation_failures'], failures)
        self.assertEqual(result['agent_result']['terminal_reason'], 'api_error')

    def test_generation_failure_survives_evaluation_exception(self):
        event = dict(type='result', subtype='success', is_error=True,
                     terminal_reason='api_error', api_error_status=None)
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event, returncode=1)), \
                patch.object(runner, 'evaluate', side_effect=RuntimeError('grading failed')):
            result = runner.run_task(self.pro, self.data, {})
        self.assertEqual(result['failure_kind'], 'upstream_api_error')
        self.assertEqual(result['failure_stage'], 'generation')
        self.assertEqual(result['evaluation_failures'][0]['error'], 'grading failed')
        saved = json.loads(Path('result', result['task_uuid'] + '.json').read_text())
        self.assertIn('evaluation_finished_at', saved)
        self.assertEqual(saved['status'], 'error')

    def test_grading_environment_failure_reaches_task_classification(self):
        event = dict(type='result', subtype='success', is_error=False, stop_reason='end_turn')
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(event)), \
                patch.object(runner, 'evaluate', return_value={'status': 'error',
                    'score_valid': False, 'artifact_install': {'status': 'success'},
                    'failure_kind': 'environment_error', 'failure_stage': 'grading_setup'}):
            result = runner.run_task(self.pro, self.data, {})
        self.assertEqual(result['failure_kind'], 'environment_error')
        self.assertEqual(result['failure_stage'], 'grading_setup')
        self.assertFalse(result['evaluation_valid'])

    def test_task_install_commands_are_passed_to_post_processor(self):
        policy = {'generation_image': 'generation', 'artifact_install_commands': ['python install.py']}
        with patch.object(grader, 'post_process_task', return_value={}) as post:
            runner.evaluate('fixture', Path(self.temp.name), self.data,
                            grading_image='grading', dependency_policy=policy)
        self.assertEqual(post.call_args.kwargs['install_commands'], ['python install.py'])

    def test_invalid_evaluation_never_completes(self):
        for score_valid, install, stage in [(True, 'failed', 'artifact_install'),
                                           (False, 'success', 'grading_tests')]:
            with self.subTest(stage=stage), patch.object(runner.subprocess, 'run',
                    side_effect=self.fake_docker(dict(type='result', subtype='success',
                                                     is_error=False, stop_reason='end_turn'))), \
                    patch.object(runner, 'evaluate', return_value={
                        'status': 'success', 'score_valid': score_valid,
                        'artifact_install': {'status': install}, 'pytest_results': {'passed': 2}}):
                result = runner.run_task(self.pro, self.data, {})
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['evaluation_valid'])
                self.assertEqual(result['failure_stage'], stage)
                self.assertEqual(result['score'], 2)

    def test_entrypoint_returns_failure_for_invalid_evaluation(self):
        Path('config.claude_code.json').write_text(json.dumps({'harness': 'claude_code'}))
        with patch.object(sys, 'argv', ['main.py']), \
                patch.object(test_data_service, 'read_all_test_data'), \
                patch.object(runner, 'start_claude_code', return_value=[
                    {'status': 'completed', 'evaluation_valid': False}]), \
                self.assertRaises(SystemExit) as error:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'), run_name='__main__')
        self.assertEqual(error.exception.code, 1)

    def test_entrypoint_rejects_unsupported_harness_before_loading_tasks(self):
        Path('legacy.json').write_text(json.dumps({'harness': 'legacy'}))
        with patch.object(sys, 'argv', ['main.py', '--config', 'legacy.json']), \
                patch.object(test_data_service, 'read_all_test_data') as read_data, \
                patch.object(runner, 'start_claude_code') as start, \
                self.assertRaises(SystemExit) as error:
            runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'), run_name='__main__')
        self.assertEqual(error.exception.code, 2)
        read_data.assert_not_called()
        start.assert_not_called()

    def test_environment_and_endpoint_validation(self):
        pro = {**self.pro, 'apiKeyEnv': 'TEST_NL2REPO_KEY'}
        with patch.dict(os.environ, {'TEST_NL2REPO_KEY': 'env-key'}):
            env = runner.model_environment(pro)
        self.assertEqual(env['ANTHROPIC_AUTH_TOKEN'], 'env-key')
        self.assertEqual(env['ANTHROPIC_DEFAULT_HAIKU_MODEL'], 'repo-model')
        for url in ('', 'gpu:30000', 'http://gpu:30000/v1', 'http://gpu/v1/messages'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                runner.model_environment({**self.pro, 'baseUrl': url})

    def test_output_token_budget_is_explicitly_forwarded_into_container(self):
        fake_run = self.fake_docker(dict(type='result', subtype='success', is_error=False,
                                        stop_reason='end_turn'))
        observed = []

        def run(command, **kwargs):
            if command[1] == 'run':
                observed.append(command)
                self.assertIn('CLAUDE_CODE_MAX_OUTPUT_TOKENS', command)
                position = command.index('CLAUDE_CODE_MAX_OUTPUT_TOKENS')
                self.assertEqual(command[position - 1], '--env')
                self.assertEqual(kwargs['env']['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '64000')
            return fake_run(command, **kwargs)

        with patch.object(runner.subprocess, 'run', side_effect=run), \
                patch.object(runner, 'evaluate', return_value={'status': 'success', 'score_valid': True,
                    'artifact_install': {'status': 'success'}}):
            result = runner.run_task(self.pro, self.data, {'max_output_tokens': 64000})
        self.assertEqual(len(observed), 1)
        self.assertEqual(result['max_output_tokens'], 64000)
        self.assertEqual(result['status'], 'completed')
        self.assertNotIn('CLAUDE_CODE_MAX_OUTPUT_TOKENS', runner.model_environment(self.pro))

    def test_cli_request_settings_reach_container_and_saved_result(self):
        defaults = {
            'API_TIMEOUT_MS': '1800000', 'API_FORCE_IDLE_TIMEOUT': '0',
            'CLAUDE_ENABLE_STREAM_WATCHDOG': '1',
            'CLAUDE_STREAM_IDLE_TIMEOUT_MS': '1800000',
            'CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS': '1800000',
            'CLAUDE_CODE_MAX_RETRIES': '1',
        }
        for overrides in ({}, {'API_TIMEOUT_MS': '600000', 'CLAUDE_CODE_MAX_RETRIES': '0'}):
            with self.subTest(overrides=overrides), patch.dict(os.environ, overrides, clear=True), \
                    patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(
                        dict(type='result', subtype='success', is_error=False,
                             stop_reason='end_turn'))) as run, \
                    patch.object(runner, 'evaluate', return_value={'status': 'success',
                        'score_valid': True, 'artifact_install': {'status': 'success'}}):
                result = runner.run_task(self.pro, self.data, {})
                self.assertEqual(result['status'], 'completed')
                call = next(call for call in run.call_args_list if call.args[0][1] == 'run')
                command = call.args[0]
                forwarded = [command[i + 1] for i, arg in enumerate(command) if arg == '--env']
                expected = {**defaults, **overrides}
                for name, value in expected.items():
                    self.assertIn(name, forwarded)
                    self.assertEqual(call.kwargs['env'][name], value)
                saved = json.loads(Path('result', result['task_uuid'] + '.json').read_text())
                self.assertEqual(saved['claude_code_environment'], expected)

    def test_invalid_output_budget_rejected_before_any_task(self):
        with patch.object(test_data_service, 'test_data_list', [self.data]), \
                patch.object(runner, 'run_task') as run:
            for value in (0, -1, True, '64000', None):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'max_output_tokens'):
                    runner.start_claude_code({
                        'startPro': [{**self.pro, 'proNameList': ['six']}],
                        'claude_code': {'max_output_tokens': value}})
        run.assert_not_called()

    def test_long_context_compacts_at_90_percent_of_full_window(self):
        options = {'context_length': 1000000, 'max_output_tokens': 65536,
                   'model_timeout_seconds': 14400}
        env = runner.model_environment(self.pro, options)
        self.assertEqual(env['CLAUDE_CODE_MAX_CONTEXT_TOKENS'], '1000000')
        self.assertEqual(env['CLAUDE_CODE_AUTO_COMPACT_WINDOW'], '1000000')
        self.assertEqual(env['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'], '90')
        self.assertEqual(env['CLAUDE_CODE_MAX_OUTPUT_TOKENS'], '65536')
        for key in options:
            for value in (0, -1, True, '1000000', None):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                    runner.model_environment(self.pro, {key: value})

    def test_full_experiment_routes_all_task_artifacts(self):
        data = [self.data, SimpleNamespace(proName='other', md=self.data.md)]
        config = {'startPro': [{**self.pro, 'proNameList': ['*']}],
                  'experiment_name': 'full-test', 'output_dir': 'custom outputs',
                  'max_pool_size': 2, 'claude_code': {}}
        with patch.object(test_data_service, 'test_data_list', data), \
             patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(
                 dict(type='result', subtype='success', is_error=False, stop_reason='end_turn'))), \
             patch.object(runner, 'evaluate', return_value={'status': 'success', 'score_valid': True,
                    'artifact_install': {'status': 'success'}}):
            results = runner.start_claude_code(config)
        root = Path('custom outputs/full-test').resolve()
        self.assertEqual({r['pro_name'] for r in results}, {'six', 'other'})
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['experiment_name'], 'full-test')
            self.assertEqual(result['experiment_path'], str(root))
            self.assertTrue(Path(result['workspace_path']).is_relative_to(root / 'workspaces'))
            self.assertTrue(Path(result['trajectory_path']).is_file())
            self.assertTrue(Path(result['stderr_path']).is_file())
            self.assertTrue((root / 'result' / (result['task_uuid'] + '.json')).is_file())
        self.assertFalse(Path('workspaces').exists())
        self.assertFalse(Path('result').exists())
        self.assertEqual(config['claude_code'], {})
        self.assertEqual(config['startPro'][0]['proNameList'], ['*'])

    def test_explicit_task_selection_and_legacy_paths(self):
        with patch.object(test_data_service, 'test_data_list', [self.data]), \
             patch.object(runner, 'run_task', return_value={}) as run:
            runner.start_claude_code({'startPro': [{**self.pro, 'proNameList': ['six', 'six']}]})
        run.assert_called_once()
        self.assertEqual(run.call_args.args, (self.pro | {'proNameList': ['six', 'six']}, self.data, {}))
        self.assertIn('task_id', run.call_args.kwargs)
        self.assertEqual(runner.experiment_directory({}), Path.cwd())
        self.assertEqual(runner.experiment_directory({'experiment_name': 'trial'}), Path.cwd() / 'trial')
        absolute = str(Path('external').resolve())
        self.assertEqual(runner.experiment_directory({'output_dir': absolute}), Path(absolute))

    def test_invalid_experiment_or_selection_rejected_before_launch(self):
        invalid = [
            {'experiment_name': value} for value in ('', '..', '../escape', '/tmp/escape', 'a/b', None, 1)
        ] + [{'output_dir': value} for value in ('', ' ', None, 1)]
        with patch.object(test_data_service, 'test_data_list', [self.data]), \
             patch.object(runner, 'run_task') as run:
            for options in invalid:
                with self.subTest(options=options), self.assertRaises(ValueError):
                    runner.start_claude_code({'startPro': [{**self.pro, 'proNameList': ['six']}], **options})
            for names in ([], '*', ['*', 'six'], ['missing'], [1]):
                with self.subTest(names=names), self.assertRaises(ValueError):
                    runner.start_claude_code({'startPro': [{**self.pro, 'proNameList': names}]})
        run.assert_not_called()

    def test_empty_dataset_does_not_launch_full_run(self):
        with patch.object(test_data_service, 'test_data_list', []), \
             patch.object(runner, 'run_task') as run, self.assertRaisesRegex(ValueError, 'No tasks'):
            runner.start_claude_code({'startPro': [{**self.pro, 'proNameList': ['*']}]})
        run.assert_not_called()

    def test_images_are_prepared_per_worker_and_failure_does_not_block_other_tasks(self):
        other = SimpleNamespace(proName='other', md=self.data.md)
        bad = SimpleNamespace(proName='bad', md=self.data.md)
        events = []

        def prepare(options, name):
            events.append('prepare:' + name)
            if name == 'bad':
                raise ValueError('image pull failed')
            return {'generation_image': 'sha256:' + 'a' * 64,
                    'grading_image': 'sha256:' + 'b' * 64}

        fake_docker = self.fake_docker(dict(
            type='result', subtype='success', is_error=False, stop_reason='end_turn'))

        def docker(command, **kwargs):
            if command[1] == 'run':
                events.append('generation')
            return fake_docker(command, **kwargs)

        def evaluate(*args, **kwargs):
            events.append('evaluation')
            return {'status': 'success', 'score_valid': True,
                    'artifact_install': {'status': 'success'}}

        with patch.object(test_data_service, 'test_data_list', [self.data, bad, other]), \
             patch.object(runner, 'offline_environment', side_effect=prepare), \
             patch.object(runner.subprocess, 'run', side_effect=docker), \
             patch.object(runner, 'evaluate', side_effect=evaluate):
            results = runner.start_claude_code({
                'max_pool_size': 1,
                'startPro': [{**self.pro, 'proNameList': ['six', 'bad', 'other']}]})
        self.assertEqual(events, ['prepare:six', 'generation', 'evaluation',
                                  'prepare:bad', 'prepare:other', 'generation', 'evaluation'])
        by_name = {result['pro_name']: result for result in results}
        self.assertEqual(by_name['six']['status'], 'completed')
        self.assertEqual(by_name['other']['status'], 'completed')
        self.assertEqual(by_name['bad']['status'], 'error')
        self.assertEqual(by_name['bad']['failure_stage'], 'preparing')
        self.assertFalse(by_name['bad']['evaluation_valid'])
        self.assertFalse(Path('result', by_name['bad']['task_uuid'] + '.json').exists())
        saved = json.loads(Path(by_name['bad']['workspace_path']).parent.joinpath('task_state.json').read_text())
        self.assertEqual(saved['error'], 'image pull failed')

    def test_generation_failure_saved_inside_experiment(self):
        options = {'output_dir': 'runs', 'experiment_name': 'failed-run'}
        with patch.object(runner.subprocess, 'run') as docker:
            result = runner.run_task({**self.pro, 'sk': ''}, self.data, options)
        docker.assert_not_called()
        self.assertEqual(result['status'], 'error')
        self.assertFalse(Path('runs/failed-run/result', result['task_uuid'] + '.json').is_file())
        self.assertTrue(Path(result['workspace_path']).parent.joinpath('task_state.json').is_file())
        self.assertFalse(Path('result').exists())

    def test_cli_experiment_arguments_override_config(self):
        config = {'harness': 'claude_code', 'experiment_name': 'from-config', 'output_dir': 'config-output'}
        Path('config.json').write_text(json.dumps(config))
        entrypoint = Path(__file__).resolve().parents[1] / 'main.py'
        with patch.object(sys, 'argv', ['main.py', '--config', 'config.json',
                                       '--experiment-name', 'from-cli', '--output-dir', 'cli-output']), \
             patch.object(test_data_service, 'read_all_test_data'), \
             patch.object(runner, 'start_claude_code', return_value=[]) as start, \
             self.assertRaises(SystemExit) as exit_status:
            runpy.run_path(str(entrypoint), run_name='__main__')
        self.assertEqual(exit_status.exception.code, 0)
        self.assertEqual(start.call_args.args[0]['experiment_name'], 'from-cli')
        self.assertEqual(start.call_args.args[0]['output_dir'], 'cli-output')
        self.assertEqual(json.loads(Path('config.json').read_text()), config)


if __name__ == '__main__':
    unittest.main()
