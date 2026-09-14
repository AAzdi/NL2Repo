"""Launcher contract tests: no real Docker containers or model requests."""

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from claude_code import launch
AWS_EVAL_TOKEN = 'aws-test-token'


class EvalLauncherTests(unittest.TestCase):
    def test_retry_options_default_override_and_validation(self):
        plan, _ = launch.prepare(self.args())
        options = plan['config']['claude_code']
        self.assertEqual((options['generation_retries'], options['evaluation_retries'],
                          options['retry_delay_seconds']), (1, 2, 5))
        plan, _ = launch.prepare(self.args('--generation-retries', '0', '--evaluation-retries', '4',
                                           '--retry-delay-seconds', '0.5'))
        options = plan['config']['claude_code']
        self.assertEqual((options['generation_retries'], options['evaluation_retries'],
                          options['retry_delay_seconds']), (0, 4, 0.5))
        self.template['claude_code']['generation_retries'] = -1
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'generation_retries'):
            launch.prepare(self.args())

    def test_generation_budget_default_override_and_validation(self):
        plan, _ = launch.prepare(self.args('--timeout-seconds', '21600'))
        self.assertEqual(plan['config']['claude_code']['generation_budget_seconds'], 21600)
        self.template['claude_code']['generation_budget_seconds'] = 12000
        self.config.write_text(json.dumps(self.template))
        plan, _ = launch.prepare(self.args())
        self.assertEqual(plan['config']['claude_code']['generation_budget_seconds'], 12000)
        plan, _ = launch.prepare(self.args('--generation-budget-seconds', '18000'))
        saved = self.save_plan(plan)
        self.assertEqual(json.loads((saved / 'config.json').read_text())[
            'claude_code']['generation_budget_seconds'], 18000)
        self.template['claude_code']['generation_budget_seconds'] = 0
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'generation_budget_seconds'):
            launch.prepare(self.args('--name', 'invalid-budget'))

    def test_task_install_commands_are_saved_and_validated(self):
        plan, _ = launch.prepare(self.args('--tasks', 'autojump'))
        entry = plan['config']['claude_code']['offline_environments']['autojump']
        self.assertEqual(entry['artifact_install_commands'], ['python install.py'])
        for invalid in (None, [], '', 'python install.py', [''], ['  '], [1]):
            with self.subTest(invalid=invalid):
                self.template['claude_code']['offline_environments']['six']['artifact_install_commands'] = invalid
                self.config.write_text(json.dumps(self.template))
                with self.assertRaisesRegex(ValueError, 'artifact_install_commands'):
                    launch.prepare(self.args())

    def test_python_version_check_override_is_saved_in_experiment_config(self):
        plan, _ = launch.prepare(self.args())
        self.assertTrue(plan['config']['claude_code'].get('check_python_version', True))
        plan, _ = launch.prepare(self.args('--skip-python-version-check'))
        directory = self.save_plan(plan)
        saved = json.loads((directory / 'config.json').read_text())
        self.assertIs(saved['claude_code']['check_python_version'], False)
        self.template['claude_code']['check_python_version'] = 'false'
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'check_python_version must be a boolean'):
            launch.prepare(self.args('--name', 'invalid-check'))

    def test_install_and_grading_timeouts_override_and_validate(self):
        plan, _ = launch.prepare(self.args('--install-timeout-seconds', '60',
                                           '--grading-timeout-seconds', '300'))
        self.assertEqual(plan['config']['claude_code']['install_timeout_seconds'], 60)
        self.assertEqual(plan['config']['claude_code']['grading_timeout_seconds'], 300)
        self.template['claude_code']['grading_timeout_seconds'] = 0
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'grading_timeout_seconds'):
            launch.prepare(self.args())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = json.loads((launch.ROOT / 'config.claude_code.json').read_text())
        self.template['claude_code']['host_base_url'] = 'http://old-service:4000'
        self.template['startPro'][0]['sk'] = 'old-secret'
        self.config = self.root / 'template.json'
        self.config.write_text(json.dumps(self.template))
        env = patch.dict(os.environ, {'SGLANG_API_KEY': 'upstream-secret'})
        env.start()
        self.addCleanup(env.stop)

    def args(self, *extra):
        return launch.parser().parse_args([
            '--name', 'new-run', '--base-url', 'http://gpu:30000', '--model', 'new-model',
            '--config', str(self.config), '--output-dir', str(self.root), '--tasks', 'six',
            *extra])

    def save_plan(self, plan):
        directory = Path(plan['directory'])
        directory.mkdir()
        launch.write_json(directory / 'config.json', plan['config'])
        return directory

    def test_gateway_route_limits_policy_and_secrets(self):
        plan, env = launch.prepare(self.args('--max-turns', '250', '--concurrency', '3',
                                             '--timeout-seconds', '18000', '--gateway-port', '4010'))
        config = plan['config']
        self.assertEqual(config['startPro'], [{
            'moduleName': 'repo-model', 'baseUrl': 'http://127.0.0.1:4010',
            'apiKeyEnv': 'NL2REPO_EVAL_GATEWAY_KEY', 'proNameList': ['six']}])
        self.assertNotIn('host_base_url', config['claude_code'])
        self.assertEqual(config['claude_code']['max_turns'], 250)
        self.assertEqual(config['claude_code']['timeout_seconds'], 18000)
        self.assertEqual(config['claude_code']['install_timeout_seconds'], 600)
        self.assertEqual(config['claude_code']['grading_timeout_seconds'], 1800)
        self.assertEqual(config['max_pool_size'], 3)
        self.assertEqual(config['claude_code']['offline_environments'],
                         self.template['claude_code']['offline_environments'])
        self.assertEqual(config['claude_code']['check_image_residue'],
                         self.template['claude_code']['check_image_residue'])
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertEqual(params['api_base'], 'http://gpu:30000/v1')
        self.assertEqual(params['model'], 'openai/new-model')
        self.assertEqual(env['NL2REPO_EVAL_UPSTREAM_KEY'], 'upstream-secret')
        for secret in ('upstream-secret', 'old-secret', env['NL2REPO_EVAL_GATEWAY_KEY']):
            self.assertNotIn(secret, json.dumps(plan))
        self.assertEqual(env['AIOHTTP_SO_KEEPALIVE'], 'true')
        self.assertEqual(env['AIOHTTP_TCP_KEEPIDLE'], '30')
        self.assertEqual(env['AIOHTTP_TCP_KEEPINTVL'], '15')
        self.assertEqual(env['AIOHTTP_TCP_KEEPCNT'], '4')

    def test_long_context_limits_and_template_precedence(self):
        args = self.phoenix_args('--context-length', '1000000', '--max-output-tokens', '393216',
                                 '--model-timeout-seconds', '14400')
        private = Path(args.phoenix_config)
        config = json.loads(private.read_text())
        config['model_list'][0]['litellm_params'].update(
            max_tokens=32000, max_completion_tokens=64000, timeout=3600)
        config['model_list'][0]['litellm_params'].setdefault('extra_headers', {})['x-eval-timeout'] = '3600'
        private.write_text(json.dumps(config))
        plan, _ = launch.prepare(args)
        options = plan['config']['claude_code']
        self.assertEqual(options['context_length'], 1000000)
        self.assertEqual(options['max_output_tokens'], 393216)
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertNotIn('max_tokens', params)
        self.assertNotIn('max_completion_tokens', params)
        self.assertEqual(params['timeout'], 14400)
        self.assertEqual(params['extra_headers']['x-eval-timeout'], '14400')
        with self.assertRaisesRegex(ValueError, 'context_length must exceed'):
            launch.prepare(self.args('--context-length', '32000', '--max-output-tokens', '32000'))

    def test_direct_mode_and_versioned_gateway_url(self):
        plan, _ = launch.prepare(self.args('--mode', 'direct'))
        self.assertIsNone(plan['gateway'])
        self.assertEqual(plan['config']['startPro'][0]['baseUrl'], 'http://gpu:30000')
        self.assertEqual(plan['config']['startPro'][0]['moduleName'], 'new-model')
        plan, _ = launch.prepare(self.args('--base-url', 'http://gpu:30000/v1/'))
        self.assertEqual(plan['gateway']['model_list'][0]['litellm_params']['api_base'],
                         'http://gpu:30000/v1')
        with self.assertRaisesRegex(ValueError, 'exclude /v1'):
            launch.prepare(self.args('--mode', 'direct', '--base-url', 'http://gpu:30000/v1'))

    def test_output_budget_can_be_inherited_or_overridden(self):
        plan, _ = launch.prepare(self.args())
        self.assertNotIn('max_output_tokens', plan['config']['claude_code'])
        self.template['claude_code']['max_output_tokens'] = 48000
        self.config.write_text(json.dumps(self.template))
        plan, _ = launch.prepare(self.args())
        self.assertEqual(plan['config']['claude_code']['max_output_tokens'], 48000)
        plan, _ = launch.prepare(self.args('--max-output-tokens', '64000'))
        self.assertEqual(plan['config']['claude_code']['max_output_tokens'], 64000)
        self.template['claude_code']['max_output_tokens'] = -1
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'max_output_tokens'):
            launch.prepare(self.args())

    def test_max_token_aliases_use_existing_output_budget_and_validation(self):
        for flag in ('--max-token', '--max-tokens', '--max-output-tokens'):
            with self.subTest(flag=flag):
                plan, _ = launch.prepare(self.args(flag, '16384'))
                self.assertEqual(plan['config']['claude_code']['max_output_tokens'], 16384)
                with self.assertRaisesRegex(ValueError, 'context_length must exceed'):
                    launch.prepare(self.args(flag, '16384', '--context-length', '16384'))
                with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
                    self.args(flag, '0')

    def test_invalid_inputs_and_existing_experiment(self):
        for extra in [('--tasks', 'six,*'), ('--tasks', 'missing-task'), ('--tasks', ''),
                      ('--name', '../escape'), ('--gateway-port', '65536'),
                      ('--base-url', 'http://user:password@gpu:30000'),
                      ('--base-url', 'http://gpu:bad'),
                      ('--base-url', 'http://gpu:30000/v1/chat/completions'),
                      ('--api-key-env', 'EVAL_TEST_MISSING_KEY')]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                launch.prepare(self.args(*extra))
        (self.root / 'new-run').mkdir()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            launch.prepare(self.args())

    def test_generation_overrides_and_incremental_prompt_are_explicit(self):
        args = self.phoenix_args('--chat-template-kwargs', '{"enable_thinking":false}',
                                 '--reasoning-effort', 'none', '--incremental')
        private = Path(args.phoenix_config)
        config = json.loads(private.read_text())
        config['model_list'][0]['litellm_params']['extra_body'] = {
            'top_k': 20, 'chat_template_kwargs': {'enable_thinking': True}}
        private.write_text(json.dumps(config))
        plan, env = launch.prepare(args)
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertEqual(params['extra_body'], {
            'top_k': 20, 'chat_template_kwargs': {'enable_thinking': False}})
        self.assertEqual(params['reasoning_effort'], 'none')
        self.assertTrue(plan['config']['claude_code']['incremental'])
        self.assertEqual(env['NL2REPO_GATEWAY_DIAGNOSTICS'],
                         str(Path(plan['directory']) / 'gateway.requests.jsonl'))
        with self.assertRaisesRegex(ValueError, 'phoenix/gateway'):
            launch.prepare(self.args('--mode', 'direct', '--chat-template-kwargs', '{}'))
        with self.assertRaisesRegex(ValueError, 'boolean'):
            launch.prepare(self.args('--chat-template-kwargs', '{"enable_thinking":"false"}'))

    def test_missing_task_environment_fails_before_launch(self):
        self.template['claude_code']['offline_environments'].pop('six')
        self.config.write_text(json.dumps(self.template))
        with self.assertRaisesRegex(ValueError, 'Missing offline_environments'):
            launch.prepare(self.args())

    def test_dry_run_has_no_process_network_or_file_side_effects(self):
        with patch.object(launch.subprocess, 'run') as run, \
                patch.object(launch.subprocess, 'Popen') as popen, \
                patch.object(launch.socket, 'socket') as socket, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(launch.launch(self.args('--dry-run')), 0)
        run.assert_not_called()
        popen.assert_not_called()
        socket.assert_not_called()
        self.assertFalse((self.root / 'new-run').exists())
        self.assertNotIn('upstream-secret', output.getvalue())

    def test_gateway_readiness_requires_expected_authenticated_model(self):
        process = Mock()
        process.poll.return_value = None
        opener = Mock()
        response = io.StringIO(json.dumps({'data': [{'id': 'repo-model'}]}))
        opener.open.return_value = response
        with patch.object(launch, 'build_opener', return_value=opener):
            launch.wait_for_gateway(process, 'http://127.0.0.1:4001', 'private-key', 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'http://127.0.0.1:4001/v1/models')
        self.assertEqual(request.get_header('Authorization'), 'Bearer private-key')

    def test_readiness_rejects_wrong_model_until_timeout(self):
        process = Mock()
        process.poll.return_value = None
        opener = Mock()
        opener.open.return_value = io.StringIO(json.dumps({'data': [{'id': 'another-model'}]}))
        with patch.object(launch, 'build_opener', return_value=opener), \
                patch.object(launch.time, 'monotonic', side_effect=[0, 0, 0, 2]), \
                patch.object(launch.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                launch.wait_for_gateway(process, 'http://127.0.0.1:4001', 'private-key', 1)

    def test_background_launch_handoff_preserves_environment_and_snapshots(self):
        def start_worker(command, **kwargs):
            path = Path(command[-1])
            plan = json.loads(path.read_text())
            self.assertEqual(command[-2], '--_worker')
            self.assertEqual(plan['config']['claude_code']['max_turns'], 123)
            self.assertEqual(kwargs['env']['AIOHTTP_TCP_KEEPIDLE'], '45')
            self.assertEqual(kwargs['env']['NL2REPO_EVAL_UPSTREAM_KEY'], 'upstream-secret')
            self.assertTrue(kwargs['start_new_session'])
            launch.write_json(path.parent / 'run-state.json', {'status': 'running'})
            return Mock(pid=5432)

        with patch.object(launch.subprocess, 'run'), \
                patch('claude_code.offline.offline_environment',
                      side_effect=AssertionError('Launcher must not prepare task images')) as offline, \
                patch.object(launch.subprocess, 'Popen', side_effect=start_worker), \
                patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(launch.launch(self.args('--mode', 'direct', '--max-turns', '123',
                                                    '--keepalive-idle', '45')), 0)
        offline.assert_not_called()
        directory = self.root / 'new-run'
        self.assertEqual(json.loads((directory / 'supervisor.pid').read_text()), 5432)
        self.assertFalse((directory / 'gateway.yaml').exists())
        self.assertNotIn('upstream-secret', (directory / 'launch.json').read_text())

    def test_background_failure_is_reported_to_caller(self):
        def failed_worker(command, **kwargs):
            launch.write_json(Path(command[-1]).parent / 'run-state.json', {'status': 'failed'})
            return Mock(pid=5432)

        with patch.object(launch.subprocess, 'run'), \
                patch('claude_code.offline.offline_environment'), \
                patch.object(launch.subprocess, 'Popen', side_effect=failed_worker):
            with self.assertRaisesRegex(RuntimeError, 'Startup failed'):
                launch.launch(self.args('--mode', 'direct'))

    def test_gateway_failure_does_not_start_benchmark(self):
        plan, env = launch.prepare(self.args())
        directory = self.save_plan(plan)
        gateway = Mock(pid=4321)
        gateway.poll.return_value = 1
        with patch.object(launch.subprocess, 'Popen', return_value=gateway) as popen:
            self.assertEqual(launch.supervise(plan, env), 1)
        self.assertEqual(popen.call_count, 1)
        state = json.loads((directory / 'run-state.json').read_text())
        self.assertEqual(state['status'], 'failed')
        self.assertNotIn('benchmark_pid', state)

    def test_success_closes_only_own_gateway_and_records_state(self):
        plan, env = launch.prepare(self.args('--gateway-port', '4002'))
        directory = self.save_plan(plan)
        gateway = Mock(pid=4321)
        gateway.poll.return_value = None
        benchmark = Mock(pid=4322, returncode=0)
        benchmark.poll.return_value = 0
        with patch.object(launch.subprocess, 'Popen', side_effect=[gateway, benchmark]) as popen, \
                patch.object(launch, 'wait_for_gateway') as ready, \
                patch.object(launch.os, 'killpg') as killpg:
            self.assertEqual(launch.supervise(plan, env), 0)
        ready.assert_called_once()
        killpg.assert_called_once_with(gateway.pid, launch.signal.SIGTERM)
        state = json.loads((directory / 'run-state.json').read_text())
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['benchmark_pid'], benchmark.pid)
        gateway_env = popen.call_args_list[0].kwargs['env']
        self.assertEqual(gateway_env['AIOHTTP_SO_KEEPALIVE'], 'true')
        self.assertEqual(popen.call_args_list[1].args[0][-1], str(directory / 'config.json'))

    def test_direct_mode_does_not_start_gateway_and_propagates_failure(self):
        plan, env = launch.prepare(self.args('--mode', 'direct'))
        directory = self.save_plan(plan)
        benchmark = Mock(pid=4322, returncode=1)
        benchmark.poll.return_value = 1
        with patch.object(launch.subprocess, 'Popen', return_value=benchmark) as popen:
            self.assertEqual(launch.supervise(plan, env), 1)
        self.assertEqual(popen.call_count, 1)
        self.assertIn('main.py', popen.call_args.args[0][1])
        self.assertEqual(json.loads((directory / 'run-state.json').read_text())['status'], 'failed')

    def test_busy_port_does_not_create_experiment_or_start_process(self):
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=False)
        fake_socket.bind.side_effect = OSError('Address already in use')
        with patch.object(launch.subprocess, 'run'), \
                patch.object(launch.subprocess, 'Popen') as popen, \
                patch('claude_code.offline.offline_environment'), \
                patch.object(launch.socket, 'socket', return_value=fake_socket):
            with self.assertRaisesRegex(OSError, 'already in use'):
                launch.launch(self.args('--gateway-port', '4000'))
        popen.assert_not_called()
        self.assertFalse((self.root / 'new-run').exists())

    def test_shell_entrypoint_works_outside_repo(self):
        result = subprocess.run(['bash', str(launch.ROOT / 'eval.sh'), '--help'],
                                cwd=self.root, capture_output=True, text=True,
                                env={**os.environ, 'NL2REPO_PYTHON': os.sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--keepalive-idle', result.stdout)

    def phoenix_args(self, *extra):
        private = self.root / 'phoenix.yaml'
        private.write_text(json.dumps({'model_list': [{'model_name': 'repo-model', 'litellm_params': {
            'model': 'openai/default', 'api_key': 'phoenix-key',
            'extra_headers': {'x-eval-token': 'phoenix-token',
                              'x-eval-domain-proxy': 'http://old:30000'}}}]}))
        return launch.parser().parse_args([
            '--name', 'new-run', '--config', str(self.config), '--output-dir', str(self.root),
            '--tasks', 'six', '--concurrency', '1', '--phoenix-config', str(private),
            '--phoenix-proxy', 'proxy_33.59.171.170:23456', *extra])

    def test_phoenix_default_builds_isolated_gateway_and_dry_run_hides_secrets(self):
        args = self.phoenix_args('--dry-run')
        plan, env = launch.prepare(args)
        self.assertEqual(plan['mode'], 'phoenix')
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertEqual(params['api_base'], 'http://phoenix-gw-eval.alibaba.com/eval/v1')
        self.assertEqual(params['model'], 'openai/default')
        self.assertEqual(params['extra_headers']['X-Backend-TrajectoryID'], 'proxy_33.59.171.170:23456')
        self.assertEqual(env['NL2REPO_EVAL_UPSTREAM_KEY'], 'phoenix-key')
        self.assertEqual(plan['config']['startPro'][0]['apiKeyEnv'], 'NL2REPO_EVAL_GATEWAY_KEY')
        with patch.object(launch.subprocess, 'Popen') as popen, \
                patch.object(launch.subprocess, 'run') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(launch.launch(args), 0)
        popen.assert_not_called()
        run.assert_not_called()
        self.assertFalse(Path(plan['directory']).exists())
        for secret in ('phoenix-key', 'phoenix-token', 'upstream-secret'):
            self.assertNotIn(secret, output.getvalue())

    def test_azure_launch_and_default_template_selection(self):
        args = self.phoenix_args('--phoenix-routing', 'azure', '--max-token', '8192',
            '--model-timeout-seconds', '120', '--reasoning-effort', 'low', '--incremental')
        args.phoenix_proxy = None
        private = Path(args.phoenix_config)
        data = json.loads(private.read_text())
        data['model_list'][0]['litellm_params'].update(model='openai/gpt-5.6-luna',
            extra_headers={'x-eval-token': 'cloud-secret', 'tenant': 'cloud-tenant',
                'empId': 'cloud-employee', 'iai-tag': 'test proxy',
                'x-eval-domain-proxy': 'https://iai.example'})
        private.write_text(json.dumps(data))
        plan, env = launch.prepare(args)
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertEqual(plan['phoenix_routing'], 'azure')
        self.assertEqual(params['api_base'], 'http://phoenix-gw-eval.alibaba.com/eval/azure')
        self.assertEqual(params['model'], 'openai/gpt-5.6-luna')
        self.assertEqual(params['reasoning_effort'], 'low')
        self.assertEqual(params['extra_headers']['x-eval-timeout'], '120')
        self.assertTrue(plan['config']['claude_code']['incremental'])
        self.assertEqual(plan['config']['claude_code']['max_output_tokens'], 8192)
        self.assertNotIn('cloud-secret', json.dumps(plan))
        self.check_phoenix_runtime(args, {'tenant': 'cloud-tenant', 'empid': 'cloud-employee',
            'x-eval-token': 'cloud-secret', 'x-eval-domain-proxy': 'https://iai.example'})
        args.phoenix_config = None
        args.name = 'default-template-run'
        with patch.object(launch, 'prepare_phoenix', return_value=(params, env)) as prepare:
            launch.prepare(args)
        self.assertEqual(prepare.call_args.args[0],
                         launch.ROOT / '.nl2repo-local/litellm.phoenix-azure.yaml')

    def test_phoenix_runtime_headers_are_resolved_and_temporary_config_is_removed(self):
        self.check_phoenix_runtime(self.phoenix_args(), {
            'x-eval-domain-proxy': 'http://33.59.171.170:23456'})

    @patch.dict(os.environ, {'PHOENIX_EVAL_TOKEN': AWS_EVAL_TOKEN})
    def test_aws_runtime_resolves_both_ids_and_records_routing(self):
        args = self.phoenix_args('--phoenix-routing', 'aws',
                                 '--phoenix-proxy', 'accio-agentic-rl-multicloud-gateway-aws.alibaba-inc.com',
                                 '--phoenix-backend-host', 'gpu:30000',
                                 '--phoenix-trajectory-id', 'private-b200-route',
                                 '--model-timeout-seconds', '3600')
        self.check_phoenix_runtime(args, {
            'x-eval-domain-proxy': 'accio-agentic-rl-multicloud-gateway-aws.alibaba-inc.com',
            'x-backend-host': 'gpu:30000', 'x-backend-region': 'us_aws',
            'x-smg-routing-key': 'private-b200-route', 'x-backend-trajectoryid': 'private-b200-route',
            'x-eval-timeout': '3600', 'x-backend-timeout': '3600'})

    def check_phoenix_runtime(self, args, expected_headers):
        plan, env = launch.prepare(args)
        directory = self.save_plan(plan)
        gateway = Mock(pid=4321)
        gateway.poll.return_value = None
        benchmark = Mock(pid=4322, returncode=0)
        benchmark.poll.return_value = 0
        runtime_paths = []

        def start(command, **kwargs):
            if command[0].endswith('/litellm'):
                path = Path(command[command.index('--config') + 1])
                runtime_paths.append(path)
                self.assertFalse(path.is_relative_to(directory))
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                runtime = json.loads(path.read_text())
                headers = runtime['model_list'][0]['litellm_params']['extra_headers']
                expected_token = expected_headers.get('x-eval-token',
                    os.environ.get('PHOENIX_EVAL_TOKEN', AWS_EVAL_TOKEN)
                    if plan['phoenix_routing'] == 'aws' else 'phoenix-token')
                self.assertTrue(headers['x-eval-token'] == expected_token)
                for name, value in expected_headers.items():
                    self.assertEqual(headers[name], value)
                self.assertEqual(runtime['general_settings']['master_key'], env['NL2REPO_EVAL_GATEWAY_KEY'])
                return gateway
            return benchmark

        with patch.object(launch.subprocess, 'Popen', side_effect=start), \
                patch.object(launch, 'wait_for_gateway'), patch.object(launch.os, 'killpg'):
            self.assertEqual(launch.supervise(plan, env), 0)
        self.assertEqual(len(runtime_paths), 1)
        self.assertFalse(runtime_paths[0].parent.exists())
        state = json.loads((directory / 'run-state.json').read_text())
        self.assertEqual(state['phoenix_routing'], plan['phoenix_routing'])
        for path in directory.iterdir():
            self.assertNotIn('phoenix-token', path.read_text())
            self.assertNotIn('private-b200-route', path.read_text())
            self.assertTrue(AWS_EVAL_TOKEN not in path.read_text())

    def test_conflicting_modes_fail_before_launch(self):
        for extra in (('--mode', 'phoenix'), ('--phoenix-proxy', 'proxy_host:123'),
                      ('--phoenix-config', 'unused.yaml'),
                      ('--phoenix-routing', 'aws'), ('--phoenix-backend-host', 'host:123'),
                      ('--phoenix-trajectory-id', 'route'), ('--phoenix-backend-region', 'us_aws')):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                launch.prepare(self.args(*extra))
        for mode in ('gateway', 'direct'):
            args = launch.parser().parse_args(['--name', 'new-run', '--mode', mode])
            with self.assertRaisesRegex(ValueError, '--base-url and --model'):
                launch.prepare(args)

    @patch.dict(os.environ, {'PHOENIX_EVAL_TOKEN': AWS_EVAL_TOKEN})
    def test_aws_routes_and_timeout_override_are_in_dry_run_plan(self):
        args = self.phoenix_args('--phoenix-routing', 'aws',
                                 '--phoenix-proxy', 'accio-agentic-rl-multicloud-gateway-aws.alibaba-inc.com',
                                 '--phoenix-backend-host', 'gpu:30000',
                                 '--phoenix-trajectory-id', 'private-b200-route',
                                 '--model-timeout-seconds', '14400', '--max-token', '16384', '--dry-run')
        plan, env = launch.prepare(args)
        self.assertEqual(plan['phoenix_routing'], 'aws')
        params = launch.resolve_environment(plan['gateway'], env)['model_list'][0]['litellm_params']
        headers = params['extra_headers']
        self.assertEqual(headers['x-backend-host'], 'gpu:30000')
        self.assertEqual(headers['x-backend-region'], 'us_aws')
        self.assertEqual(headers['x-smg-routing-key'], 'private-b200-route')
        self.assertEqual(headers['x-backend-trajectoryid'], 'private-b200-route')
        self.assertEqual(headers['x-eval-timeout'], '14400')
        self.assertEqual(headers['x-backend-timeout'], '14400')
        self.assertEqual(params['timeout'], 14400)
        self.assertEqual(plan['config']['claude_code']['model_timeout_seconds'], 14400)
        self.assertEqual(plan['config']['claude_code']['max_output_tokens'], 16384)
        self.assertNotIn('max_tokens', params)
        self.assertNotIn('max_completion_tokens', params)
        with patch.object(launch.subprocess, 'Popen') as popen, \
                patch.object(launch.subprocess, 'run') as run, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(launch.launch(args), 0)
        popen.assert_not_called()
        run.assert_not_called()
        self.assertFalse(Path(plan['directory']).exists())
        for private in ('phoenix-token', 'private-b200-route'):
            self.assertNotIn(private, output.getvalue())
        self.assertTrue(AWS_EVAL_TOKEN not in output.getvalue())

    @patch.dict(os.environ, {'PHOENIX_EVAL_TOKEN': AWS_EVAL_TOKEN})
    def test_aws_default_timeout_is_shared_with_model_channel(self):
        args = self.phoenix_args('--phoenix-routing', 'aws',
                                 '--phoenix-backend-host', 'gpu:30000',
                                 '--phoenix-trajectory-id', 'route')
        plan, _ = launch.prepare(args)
        params = plan['gateway']['model_list'][0]['litellm_params']
        self.assertEqual(params['timeout'], 3600)
        self.assertEqual(plan['config']['claude_code']['model_timeout_seconds'], 3600)
        self.assertEqual(params['extra_headers']['x-backend-timeout'], '3600')

    @patch.dict(os.environ, {'PHOENIX_EVAL_TOKEN': AWS_EVAL_TOKEN})
    def test_aws_timeout_precedence_is_shared_across_all_hops(self):
        args = self.phoenix_args('--phoenix-routing', 'aws',
                                 '--phoenix-backend-host', 'gpu:30000',
                                 '--phoenix-trajectory-id', 'route')
        private = Path(args.phoenix_config)
        data = json.loads(private.read_text())
        data['model_list'][0]['litellm_params']['timeout'] = 1800
        private.write_text(json.dumps(data))
        self.template['claude_code'].pop('model_timeout_seconds', None)
        for base_timeout, cli_timeout, expected in ((None, None, 1800), (600, None, 600), (600, 120, 120)):
            with self.subTest(base=base_timeout, cli=cli_timeout):
                if base_timeout is not None:
                    self.template['claude_code']['model_timeout_seconds'] = base_timeout
                self.config.write_text(json.dumps(self.template))
                args.model_timeout_seconds = cli_timeout
                plan, _ = launch.prepare(args)
                params = plan['gateway']['model_list'][0]['litellm_params']
                self.assertEqual(params['timeout'], expected)
                self.assertEqual(params['extra_headers']['x-eval-timeout'], str(expected))
                self.assertEqual(params['extra_headers']['x-backend-timeout'], str(expected))
                self.assertEqual(plan['config']['claude_code']['model_timeout_seconds'], expected)

    def test_default_templates_follow_resolved_route(self):
        args = self.phoenix_args()
        args.phoenix_config = None
        params = {'model': 'openai/default', 'timeout': 3600, 'extra_headers': {}}
        env = {'NL2REPO_EVAL_UPSTREAM_KEY': 'test-key'}
        for route, filename in (('legacy', 'litellm.phoenix.yaml'),
                                ('aws', 'litellm.phoenix.yaml'),
                                ('azure', 'litellm.phoenix-azure.yaml')):
            with self.subTest(route=route), patch.dict(os.environ, {'PHOENIX_ROUTING': route}), \
                    patch.object(launch, 'prepare_phoenix', return_value=(params.copy(), env)) as prepare:
                plan, _ = launch.prepare(args)
                self.assertEqual(plan['phoenix_routing'], route)
                self.assertEqual(prepare.call_args.args[0], launch.ROOT / '.nl2repo-local' / filename)
                self.assertEqual(prepare.call_args.kwargs['routing'], route)
                args.phoenix_routing = 'legacy'
                plan, _ = launch.prepare(args)
                self.assertEqual(plan['phoenix_routing'], 'legacy')
                self.assertEqual(prepare.call_args.args[0],
                                 launch.ROOT / '.nl2repo-local/litellm.phoenix.yaml')
                args.phoenix_routing = None


if __name__ == '__main__':
    unittest.main()
