import json
from pathlib import Path
import subprocess
import threading
import unittest
from unittest.mock import Mock, patch

import test_claude_code as fixtures
from claude_code import runner
from claude_code.retries import (retry_options, generation_retry_reason,
    evaluation_retry_reason, transient_exception, wait_before_retry)


SUCCESS = dict(type='result', subtype='success', is_error=False, stop_reason='end_turn')
API_ERROR = dict(type='result', subtype='success', is_error=True,
                 terminal_reason='api_error', api_error_status=503)
GRADED = {'status': 'success', 'score_valid': True, 'evaluation_valid': True,
          'artifact_install': {'status': 'success'}, 'pytest_results': {'passed': 3}}
TRANSIENT = {'status': 'error', 'score_valid': False, 'evaluation_valid': False,
             'failure_kind': 'environment_error', 'failure_stage': 'grading_setup',
             'container_diagnostics': {'state': {'Running': False}}}


class RetryPolicyTests(unittest.TestCase):
    def test_validation(self):
        for key in ('generation_retries', 'evaluation_retries'):
            for value in (-1, 1.5, '2', True, None):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    retry_options({key: value})
        for value in (-1, 61, float('nan'), float('inf'), True, '5', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                retry_options({'retry_delay_seconds': value})
        self.assertEqual(retry_options({})['generation_retries'], 0)
        self.assertEqual(retry_options({'timeout_seconds': 21600})['generation_budget_seconds'], 21600)
        for value in (0, -1, 1.5, True, None, '60', float('inf')):
            with self.subTest(budget=value), self.assertRaises(ValueError):
                retry_options({'generation_budget_seconds': value})

    def test_api_errors_limits_and_cancellation(self):
        for status in (None, 408, 429, 500, 503, '504'):
            self.assertIsNone(generation_retry_reason({'failure_kind': 'upstream_api_error',
                'agent_result': {'api_error_status': status}}))
        for status in (400, 401, 403, 404, 413, 422):
            self.assertIsNone(generation_retry_reason({'failure_kind': 'upstream_api_error',
                'agent_result': {'api_error_status': status}}))
        for event in ({'subtype': 'error_max_turns'}, {'stop_reason': 'max_tokens'}):
            self.assertIsNone(generation_retry_reason({'failure_kind': 'generation_error',
                                                       'agent_result': event}))
        self.assertIsNone(generation_retry_reason({'status': 'interrupted',
                                                   'failure_kind': 'generation_timeout'}))
        self.assertIsNone(generation_retry_reason({'failure_kind': 'generation_timeout'}))
        self.assertIsNone(generation_retry_reason({'failure_kind': 'upstream_api_error',
            'agent_result': {'result': 'Authentication failed: invalid API key'}}))

    def test_grading_uses_operational_evidence(self):
        for details in ({'execution_status': 'timeout'}, {'last_exit_code': 137},
                        {'command_results': [{'status': 'completed', 'exit_code': 1,
                            'command': 'pip install requests', 'output': 'HTTP error 503'}]}):
            self.assertIsNotNone(evaluation_retry_reason({'status': 'error', 'test_results': details}))
        for details in ({'execution_status': 'success', 'last_exit_code': 1},
                        {'command_results': [{'status': 'completed', 'exit_code': 1,
                            'command': 'pytest tests', 'output': 'AssertionError: HTTP error 503'}]},
                        {'command_results': [{'status': 'completed', 'exit_code': 1,
                            'command': 'pip install unavailable', 'output': 'No matching distribution found'}]}):
            self.assertIsNone(evaluation_retry_reason({'status': 'error', 'test_results': details}))
        self.assertIsNone(evaluation_retry_reason({**GRADED, 'pytest_results': {'passed': 0}}))
        self.assertIsNone(evaluation_retry_reason({'status': 'success', 'evaluation_valid': True,
            'score_valid': False, 'candidate_failures': [{'kind': 'candidate_error'}]}))
        self.assertFalse(transient_exception(ValueError('Offline image validation failed')))
        self.assertTrue(transient_exception(subprocess.CalledProcessError(1, 'docker',
                                                    stderr='TLS handshake timeout')))

    def test_preparation_retry_requires_no_generation_started(self):
        failure = {'stage': 'preparing', 'failure_kind': 'environment_error',
                   'transient_execution_error': True}
        self.assertIsNotNone(generation_retry_reason(failure))
        for change in ({'stage': 'generation'}, {'generation_started_at': '2026-09-13T00:00:00Z'},
                       {'status': 'interrupted'}, {'transient_execution_error': False}):
            with self.subTest(change=change):
                self.assertIsNone(generation_retry_reason({**failure, **change}))

    def test_backoff_is_bounded_and_interruptible(self):
        cancel = Mock()
        cancel.wait.return_value = False
        for attempt in range(1, 6):
            self.assertTrue(wait_before_retry(cancel, 5, attempt))
        self.assertEqual([call.args[0] for call in cancel.wait.call_args_list], [5, 10, 20, 40, 60])
        cancel.wait.return_value = True
        self.assertFalse(wait_before_retry(cancel, 5, 1))


class TaskRetryTests(unittest.TestCase):
    setUp = fixtures.ClaudeCodeTests.setUp
    fake_docker = fixtures.ClaudeCodeTests.fake_docker

    def test_preparation_retry_archives_diagnostics_before_first_generation(self):
        environment = runner.offline_environment.return_value
        preparations = []
        def prepare(*args):
            root = next(Path('workspaces').iterdir())
            preparations.append(root)
            self.assertFalse((root / 'workspace').exists())
            if len(preparations) == 1:
                (root / 'preparation.log').write_text('temporary image failure')
                raise ConnectionError('connection reset')
            self.assertFalse((root / 'preparation.log').exists())
            return environment
        with patch.object(runner, 'offline_environment', side_effect=prepare), \
                patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', return_value=GRADED) as evaluate:
            result = runner.run_task(self.pro, self.data,
                {'generation_retries': 2, 'retry_delay_seconds': 0})
        self.assertEqual(len(preparations), 2)
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 1)
        evaluate.assert_called_once()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['task_attempt_count'], 2)
        archived = preparations[0].resolve() / 'generation-attempts/1'
        self.assertEqual((archived / 'preparation.log').read_text(), 'temporary image failure')
        state = json.loads((archived / 'task_state.json').read_text())
        self.assertEqual(Path(state['trajectory_path']), archived / 'trajectory.json')
        self.assertNotIn('generation_started_at', state)
        self.assertEqual(len(list(Path('result').glob('*.json'))), 1)
        self.assertNotIn('failure_kind', result)

    def test_api_and_incomplete_stream_failures_preserve_code_without_regeneration(self):
        for event in (API_ERROR, {**API_ERROR, 'api_error_status': 429},
                      {**API_ERROR, 'api_error_status': None}, None):
            with self.subTest(event=event):
                def docker(command, **kwargs):
                    if command[1] == 'run':
                        workspace = Path(command[command.index('--volume') + 1].split(':')[0])
                        (workspace / 'partial.py').write_text('keep existing progress')
                    if event is None and command[1] == 'run':
                        return subprocess.CompletedProcess(command, 0)
                    return self.fake_docker(event)(command, **kwargs)
                with patch.object(runner.subprocess, 'run', side_effect=docker) as run, \
                        patch.object(runner, 'evaluate', return_value=GRADED) as evaluate, \
                        patch.object(runner, 'wait_before_retry') as wait:
                    result = runner.run_task(self.pro, self.data,
                        {'generation_retries': 5, 'retry_delay_seconds': 0})
                self.assertEqual(sum(c.args[0][1] == 'run' for c in run.call_args_list), 1)
                self.assertEqual(result['task_attempt_count'], 1)
                self.assertEqual(result['generation_retry_stop_reason'], 'generation_already_started')
                self.assertEqual(result['status'], 'failed')
                self.assertFalse(result['score_valid'])
                self.assertEqual(result['failure_kind'], 'upstream_api_error' if event else 'generation_error')
                root = Path(result['workspace_path']).parent
                self.assertEqual((root / 'workspace/partial.py').read_text(), 'keep existing progress')
                self.assertFalse((root / 'generation-attempts').exists())
                self.assertTrue(Path('result', result['task_uuid'] + '.json').exists())
                evaluate.assert_called_once()
                wait.assert_not_called()

    def test_generation_timeout_auth_and_limits_do_not_retry(self):
        for event in ({**API_ERROR, 'api_error_status': 401},
                      {**SUCCESS, 'subtype': 'error_max_turns'},
                      {**SUCCESS, 'stop_reason': 'max_tokens'}):
            with self.subTest(event=event), patch.object(runner.subprocess, 'run',
                    side_effect=self.fake_docker(event)) as docker, \
                    patch.object(runner, 'evaluate', return_value=GRADED):
                result = runner.run_task(self.pro, self.data, {'generation_retries': 2,
                                                            'retry_delay_seconds': 0})
                self.assertEqual(result['task_attempt_count'], 1)
        calls = []
        def docker(command, **kwargs):
            if command[1] == 'run':
                calls.append(command)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(command, 1)
            return self.fake_docker(SUCCESS)(command, **kwargs)
        with patch.object(runner.subprocess, 'run', side_effect=docker), \
                patch.object(runner, 'evaluate', return_value=GRADED):
            result = runner.run_task(self.pro, self.data, {'generation_retries': 1,
                                                        'generation_budget_seconds': 14400,
                                                        'retry_delay_seconds': 0})
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['generation_retry_stop_reason'], 'generation_timeout')
        self.assertEqual(result['task_attempt_count'], 1)
        self.assertEqual(len(calls), 1)

    def test_generation_process_failure_preserves_workspace_without_retry(self):
        def docker(command, **kwargs):
            if command[1] == 'run':
                workspace = Path(command[command.index('--volume') + 1].split(':')[0])
                (workspace / 'partial.py').write_text('keep progress')
                raise ConnectionError('connection reset')
            return self.fake_docker(SUCCESS)(command, **kwargs)
        with patch.object(runner.subprocess, 'run', side_effect=docker) as run, \
                patch.object(runner, 'evaluate') as evaluate:
            result = runner.run_task(self.pro, self.data,
                {'generation_retries': 5, 'retry_delay_seconds': 0})
        self.assertEqual(sum(c.args[0][1] == 'run' for c in run.call_args_list), 1)
        self.assertEqual(result['task_attempt_count'], 1)
        self.assertEqual(result['generation_retry_stop_reason'], 'generation_already_started')
        self.assertEqual(result['status'], 'error')
        self.assertEqual(Path(result['workspace_path'], 'partial.py').read_text(), 'keep progress')
        evaluate.assert_not_called()

    def test_preparation_retries_share_budget_with_generation_and_backoff(self):
        clock = [0]
        environment = runner.offline_environment.return_value
        preparations = []
        def prepare(*args):
            preparations.append(True)
            clock[0] += 50 if len(preparations) == 1 else 10
            if len(preparations) == 1:
                raise ConnectionError('connection reset')
            return environment
        def wait(*args):
            clock[0] += 5
            return True
        options = {'timeout_seconds': 100, 'generation_retries': 2}
        with patch.object(runner, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner, 'offline_environment', side_effect=prepare), \
                patch.object(runner, 'wait_before_retry', side_effect=wait), \
                patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', return_value=GRADED) as evaluate:
            result = runner.run_task(self.pro, self.data, options)
        timeouts = [c.kwargs['timeout'] for c in docker.call_args_list if c.args[0][1] == 'run']
        self.assertEqual(timeouts, [35])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['retry_policy']['generation_budget_seconds'], 100)
        self.assertEqual(options, {'timeout_seconds': 100, 'generation_retries': 2})
        evaluate.assert_called_once()

    def test_preparation_budget_exhaustion_does_not_retry_or_generate(self):
        for elapsed in (96, 100):
            clock = [0]
            def prepare(*args):
                clock[0] = elapsed
                raise ConnectionError('connection reset')
            with self.subTest(elapsed=elapsed), \
                    patch.object(runner, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(runner, 'offline_environment', side_effect=prepare), \
                    patch.object(runner.subprocess, 'run') as docker, \
                    patch.object(runner, 'wait_before_retry') as wait:
                result = runner.run_task(self.pro, self.data,
                    {'timeout_seconds': 100, 'generation_retries': 2})
            self.assertEqual(result['task_attempt_count'], 1)
            self.assertEqual(result['generation_retry_stop_reason'], 'generation_budget_exhausted')
            self.assertTrue(Path(result['trajectory_path']).exists())
            docker.assert_not_called()
            wait.assert_not_called()

    def test_generation_after_preparation_retry_cannot_reset_budget(self):
        clock = [0]
        timeouts = []
        environment = runner.offline_environment.return_value
        def prepare(*args):
            if clock[0] == 0:
                clock[0] = 40
                raise ConnectionError('connection reset')
            return environment
        def docker(command, **kwargs):
            if command[1] == 'run':
                timeouts.append(kwargs['timeout'])
                clock[0] += kwargs['timeout']
                raise subprocess.TimeoutExpired(command, kwargs['timeout'])
            return self.fake_docker(SUCCESS)(command, **kwargs)
        with patch.object(runner, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner, 'offline_environment', side_effect=prepare), \
                patch.object(runner.subprocess, 'run', side_effect=docker), \
                patch.object(runner, 'evaluate', return_value=GRADED) as evaluate:
            result = runner.run_task(self.pro, self.data,
                {'timeout_seconds': 100, 'generation_retries': 2, 'retry_delay_seconds': 0})
        self.assertEqual(timeouts, [60])
        self.assertEqual(clock[0], 100)
        self.assertEqual(result['task_attempt_count'], 2)
        self.assertEqual(result['generation_retry_stop_reason'], 'generation_timeout')
        evaluate.assert_not_called()

    def test_budget_expiring_during_preparation_wait_preserves_diagnostics(self):
        clock = [0]
        def wait(*args):
            clock[0] = 101
            return True
        with patch.object(runner, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner, 'wait_before_retry', side_effect=wait), \
                patch.object(runner, 'offline_environment', side_effect=ConnectionError('connection reset')), \
                patch.object(runner.subprocess, 'run') as docker:
            result = runner.run_task(self.pro, self.data,
                {'timeout_seconds': 100, 'generation_retries': 2})
        docker.assert_not_called()
        self.assertEqual(result['generation_retry_stop_reason'], 'generation_budget_exhausted')
        self.assertTrue(Path(result['trajectory_path']).exists())
        self.assertFalse((Path(result['workspace_path']).parent / 'generation-attempts').exists())

    def test_expired_preparation_budget_does_not_launch_generation(self):
        clock = [0]
        environment = runner.offline_environment.return_value
        def prepare(*args):
            clock[0] = 101
            return environment
        with patch.object(runner, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(runner, 'offline_environment', side_effect=prepare), \
                patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', return_value=GRADED) as evaluate:
            result = runner.run_task(self.pro, self.data,
                                     {'timeout_seconds': 100, 'generation_retries': 2})
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 0)
        self.assertEqual(result['generation_status'], 'timeout')
        self.assertEqual(result['task_attempt_count'], 1)
        evaluate.assert_not_called()

    def test_evaluation_retry_preserves_code_logs_and_single_result(self):
        calls = []
        def evaluate(task_id, workspace, data, **kwargs):
            calls.append(workspace)
            self.assertFalse((workspace.parent / 'log.log').exists())
            (workspace.parent / 'log.log').write_text(f'grading {len(calls)}')
            (workspace.parent / 'dependencies.grading.jsonl').write_text('{}\n')
            self.assertEqual(list(Path('result').glob('*.json')), [])
            return TRANSIENT if len(calls) == 1 else GRADED
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', side_effect=evaluate):
            result = runner.run_task(self.pro, self.data, {'evaluation_retries': 2,
                                                        'retry_delay_seconds': 0})
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 1)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(result['score'], 3)
        self.assertEqual(len(result['evaluation_attempts']), 2)
        root = calls[0].parent
        self.assertEqual((root / 'evaluation-attempts/1/log.log').read_text(), 'grading 1')
        self.assertEqual((root / 'log.log').read_text(), 'grading 2')
        self.assertEqual(len(list(Path('result').glob('*.json'))), 1)

    def test_evaluation_exception_retry_and_exhaustion(self):
        for outcomes, status in ([ConnectionError('connection reset'), GRADED], 'completed'), \
                                ([ConnectionError('connection reset')] * 3, 'error'):
            with self.subTest(status=status), patch.object(runner.subprocess, 'run',
                    side_effect=self.fake_docker(SUCCESS)), patch.object(runner, 'evaluate',
                    side_effect=outcomes) as evaluate:
                result = runner.run_task(self.pro, self.data, {'evaluation_retries': 2,
                                                            'retry_delay_seconds': 0})
                self.assertEqual(result['status'], status)
                self.assertEqual(evaluate.call_count, len(outcomes))
                self.assertEqual(len(result['evaluation_attempts']), len(outcomes))

    def test_deterministic_grading_failure_not_retried_or_regenerated(self):
        post = {'status': 'error', 'evaluation_valid': False, 'failure_kind': 'evaluation_error',
                'artifact_install': {'status': 'success'}, 'test_results': {
                    'execution_status': 'success', 'last_exit_code': 1}}
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', return_value=post) as evaluate:
            result = runner.run_task(self.pro, self.data, {'evaluation_retries': 2,
                'generation_retries': 2, 'retry_delay_seconds': 0})
        evaluate.assert_called_once()
        self.assertEqual(result['task_attempt_count'], 1)
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 1)

    def test_grading_exception_does_not_restart_max_turns_generation(self):
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(
                {**SUCCESS, 'subtype': 'error_max_turns'})) as docker, \
                patch.object(runner, 'evaluate', side_effect=ConnectionError('connection reset')):
            result = runner.run_task(self.pro, self.data, {'generation_retries': 2,
                'evaluation_retries': 1, 'retry_delay_seconds': 0})
        self.assertEqual(result['task_attempt_count'], 1)
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 1)
        self.assertEqual(len(result['evaluation_attempts']), 2)

    def test_low_score_completes_without_retry(self):
        with patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)) as docker, \
                patch.object(runner, 'evaluate', return_value={**GRADED,
                    'pytest_results': {'passed': 0, 'failed': 100}}) as evaluate:
            result = runner.run_task(self.pro, self.data, {'generation_retries': 2,
                'evaluation_retries': 2, 'retry_delay_seconds': 0})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['score'], 0)
        evaluate.assert_called_once()
        self.assertEqual(sum(c.args[0][1] == 'run' for c in docker.call_args_list), 1)

    def test_preparation_retry_excludes_integrity_validation(self):
        environment = runner.offline_environment.return_value
        for error, expected in ((ConnectionError('connection reset'), 2),
                                (ValueError('Reference implementation present'), 1)):
            with self.subTest(error=error), patch.object(runner, 'offline_environment',
                    side_effect=[error, environment]) as offline, \
                    patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)), \
                    patch.object(runner, 'evaluate', return_value=GRADED):
                result = runner.run_task(self.pro, self.data, {'generation_retries': 2,
                                                            'retry_delay_seconds': 0})
                self.assertEqual(offline.call_count, expected)
                self.assertEqual(result['task_attempt_count'], expected)

    def test_cancellation_during_retry_wait_preserves_failure(self):
        for phase in ('generation', 'evaluation'):
            cancel = threading.Event()
            def interrupted(*args):
                cancel.set()
                return False
            with self.subTest(phase=phase), patch.object(runner, 'offline_environment',
                    side_effect=ConnectionError('connection reset') if phase == 'generation' else None,
                    return_value=runner.offline_environment.return_value), \
                    patch.object(runner.subprocess, 'run', side_effect=self.fake_docker(SUCCESS)), \
                    patch.object(runner, 'evaluate', return_value=TRANSIENT) as evaluate, \
                    patch.object(runner, 'wait_before_retry', side_effect=interrupted):
                result = runner.run_task(self.pro, self.data, {'generation_retries': 2,
                    'evaluation_retries': 2}, cancel=cancel)
                self.assertEqual(result['status'], 'interrupted')
                self.assertEqual(result['task_attempt_count'], 1)
                self.assertFalse(result['evaluation_valid'])
                self.assertTrue(Path(result['trajectory_path']).exists())
                self.assertEqual(evaluate.call_count, 0 if phase == 'generation' else 1)
