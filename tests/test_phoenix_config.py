import json
from pathlib import Path
import tempfile
import unittest

from claude_code.phoenix_config import (prepare_phoenix, normalize_proxy, resolve_environment,
                                       MULTICLOUD_PROXY)


class PhoenixConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'phoenix.yaml'
        self.config = {'model_list': [{'model_name': 'repo-model', 'litellm_params': {
            'api_base': 'http://phoenix-gw-eval.alibaba.com/eval/v1',
            'api_key': 'test-key', 'model': 'openai/default', 'timeout': 3600,
            'extra_headers': {'x-eval-token': 'test-token', 'X-SMG-Routing-Key': 'private-route',
                              'x-eval-domain-proxy': 'http://old:23456',
                              'X-Backend-TrajectoryID': 'proxy_stale:23456'}}}]}
        self.path.write_text(json.dumps(self.config))

    def test_proxy_formats(self):
        for value in ('proxy_33.59.171.170:23456', '33.59.171.170:23456',
                      'http://33.59.171.170:23456/'):
            with self.subTest(value=value):
                self.assertEqual(normalize_proxy(value), 'http://33.59.171.170:23456')
        self.assertEqual(normalize_proxy('https://proxy.example:443'), 'https://proxy.example:443')

    def azure_template(self):
        source = self.config['model_list'][0]['litellm_params']
        source['model'] = 'openai/gpt-5.6-luna'
        source['extra_headers'].update({'tenant': 'test-tenant', 'empId': 'test-employee',
            'iai-tag': 'test proxy', 'x-eval-domain-proxy': 'https://iai.example'})
        self.path.write_text(json.dumps(self.config))

    def test_azure_uses_cloud_endpoint_and_only_cloud_headers(self):
        self.azure_template()
        params, secrets = prepare_phoenix(self.path, routing='azure', environment={
            'PHOENIX_EVAL_TOKEN': 'stale-gpu-token', 'PHOENIX_DOMAIN_PROXY': 'gpu:1234',
            'SGLANG_IP_PORT': 'gpu:5678', 'SANDBOX_TRAJECTORY_ID': 'stale-route'})
        resolved = resolve_environment(params, secrets)
        self.assertEqual(resolved['api_base'], 'http://phoenix-gw-eval.alibaba.com/eval/azure')
        self.assertEqual(resolved['model'], 'openai/gpt-5.6-luna')
        self.assertTrue(params['_skip_responses_api_bridge'])
        self.assertEqual(resolved['api_key'], 'test-tenant')
        self.assertEqual(resolved['extra_headers'], {
            'x-eval-token': 'test-token', 'x-eval-domain-proxy': 'https://iai.example',
            'tenant': 'test-tenant', 'empid': 'test-employee', 'iai-tag': 'test proxy'})
        for secret in ('test-token', 'test-key', 'test-tenant', 'test-employee', 'private-route'):
            self.assertNotIn(secret, json.dumps(params))
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_azure_environment_overrides_and_validation(self):
        self.azure_template()
        params, secrets = prepare_phoenix(self.path, routing='azure', model='other-model',
            environment={'PHOENIX_AZURE_EVAL_TOKEN': 'cloud-token',
                         'PHOENIX_AZURE_DOMAIN_PROXY': 'https://cloud.example'})
        resolved = resolve_environment(params, secrets)
        self.assertEqual(resolved['extra_headers']['x-eval-token'], 'cloud-token')
        self.assertEqual(resolved['extra_headers']['x-eval-domain-proxy'], 'https://cloud.example')
        self.assertEqual(resolved['model'], 'openai/other-model')
        for key in ('x-eval-token', 'tenant', 'empId', 'iai-tag'):
            for value in ('', 'bad\r\ninjected: header'):
                config = json.loads(json.dumps(self.config))
                config['model_list'][0]['litellm_params']['extra_headers'][key] = value
                self.path.write_text(json.dumps(config))
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    prepare_phoenix(self.path, routing='azure', environment={})
        self.azure_template()
        for option in ('backend_host', 'backend_region', 'trajectory_id'):
            with self.subTest(option=option), self.assertRaisesRegex(ValueError, 'require.*aws'):
                prepare_phoenix(self.path, routing='azure', environment={}, **{option: 'unused'})

    def test_generation_settings_survive_without_copying_credentials(self):
        settings = {'temperature': 0.6, 'top_p': 0.95, 'max_tokens': 16384,
                    'reasoning_effort': 'none',
                    'extra_body': {'chat_template_kwargs': {'enable_thinking': False}, 'top_k': 20}}
        self.config['model_list'][0]['litellm_params'].update(settings)
        self.path.write_text(json.dumps(self.config))
        params, _ = prepare_phoenix(self.path, environment={})
        for key, value in settings.items():
            self.assertEqual(params[key], value)
        self.assertNotIn('test-token', json.dumps(params))
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_generation_settings_reject_invalid_values_and_route_overrides(self):
        for settings in ({'temperature': float('nan')}, {'max_tokens': True},
                         {'top_p': 2}, {'reasoning_effort': 'unknown'},
                         {'extra_body': {'api_key': 'secret'}},
                         {'extra_body': {'chat_template_kwargs': {'enable_thinking': 'false'}}},
                         {'extra_body': {'chat_template_kwargs': {'thinking_budget': -1}}}):
            with self.subTest(settings=settings):
                config = json.loads(json.dumps(self.config))
                config['model_list'][0]['litellm_params'].update(settings)
                self.path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    prepare_phoenix(self.path, environment={})

    def test_rejects_invalid_targets(self):
        for value in (None, 123, {}, '', 'http://', 'ftp://host:123', 'host:bad', 'host:65536',
                      'host:0', 'http://user:pass@host:123', 'host:123/eval/v1',
                      'host:123?route=x', 'host:123#fragment', 'bad host:123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_proxy(value)

    def test_malformed_templates_report_configuration_errors(self):
        for config, message in (
                ([], 'model_list'), ({}, 'model_list'),
                ({'model_list': {}}, 'model_list'), ({'model_list': [None]}, 'model_list'),
                ({'model_list': []}, 'exactly one repo-model'),
                ({'model_list': self.config['model_list'] * 2}, 'exactly one repo-model'),
                ({'model_list': [{'model_name': 'repo-model'}]}, 'litellm_params'),
                ({'model_list': [{'model_name': 'repo-model', 'litellm_params': {
                    'extra_headers': []}}]}, 'extra_headers')):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, message):
                self.path.write_text(json.dumps(config))
                prepare_phoenix(self.path, environment={})

    def test_timeout_override_updates_headers_for_every_route(self):
        self.azure_template()
        env = {'SGLANG_IP_PORT': 'backend:123', 'SANDBOX_TRAJECTORY_ID': 'route',
                   'PHOENIX_EVAL_TOKEN': 'aws-test-token'}
        for route in ('legacy', 'aws', 'azure'):
            with self.subTest(route=route):
                params, _ = prepare_phoenix(self.path, routing=route, timeout=120, environment=env)
                self.assertEqual(params['timeout'], 120)
                self.assertEqual(params['extra_headers']['x-eval-timeout'], '120')
                if route == 'aws':
                    self.assertEqual(params['extra_headers']['x-backend-timeout'], '120')
                else:
                    self.assertNotIn('x-backend-timeout', params['extra_headers'])
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_invalid_template_timeouts_fail_before_launch_for_every_route(self):
        self.azure_template()
        env = {'SGLANG_IP_PORT': 'backend:123', 'SANDBOX_TRAJECTORY_ID': 'route',
                   'PHOENIX_EVAL_TOKEN': 'aws-test-token'}
        for route in ('legacy', 'aws', 'azure'):
            for timeout in (None, 0, -1, True, '3600', 0.5):
                with self.subTest(route=route, timeout=timeout):
                    self.config['model_list'][0]['litellm_params']['timeout'] = timeout
                    self.path.write_text(json.dumps(self.config))
                    with self.assertRaisesRegex(ValueError, 'timeout must be a positive integer'):
                        prepare_phoenix(self.path, routing=route, environment=env)
                    # An explicit valid value wins over an invalid template value.
                    params, _ = prepare_phoenix(self.path, routing=route, timeout=60, environment=env)
                    self.assertEqual(params['timeout'], 60)

    def test_empty_proxy_does_not_silently_fall_back_for_any_route(self):
        self.azure_template()
        for route, variable in (('legacy', 'PHOENIX_DOMAIN_PROXY'),
                                ('aws', 'PHOENIX_MULTICLOUD_PROXY'),
                                ('azure', 'PHOENIX_AZURE_DOMAIN_PROXY')):
            env = {variable: '', 'SGLANG_IP_PORT': 'backend:123', 'SANDBOX_TRAJECTORY_ID': 'route',
                   'PHOENIX_EVAL_TOKEN': 'aws-test-token'}
            with self.subTest(route=route):
                with self.assertRaises(ValueError):
                    prepare_phoenix(self.path, routing=route, environment=env)
                params, _ = prepare_phoenix(self.path, routing=route, proxy='https://cli.example',
                                             environment=env)
                expected = 'cli.example' if route == 'aws' else 'https://cli.example'
                self.assertEqual(params['extra_headers']['x-eval-domain-proxy'], expected)
                env[variable] = 'https://env.example'
                with self.assertRaises(ValueError):
                    prepare_phoenix(self.path, routing=route, proxy='', environment=env)

    def test_routes_follow_cli_then_environment_then_template_without_mutation(self):
        for proxy, environment, expected in (
                (None, {}, 'http://old:23456'),
                (None, {'PHOENIX_DOMAIN_PROXY': 'proxy_env:123'}, 'http://env:123'),
                ('https://cli:456', {'PHOENIX_DOMAIN_PROXY': 'proxy_env:123'}, 'https://cli:456')):
            with self.subTest(proxy=proxy):
                params, secrets = prepare_phoenix(self.path, proxy=proxy, environment=environment)
                headers = params['extra_headers']
                self.assertEqual(headers['x-eval-domain-proxy'], expected)
                self.assertEqual(headers['X-Backend-TrajectoryID'], 'proxy_' + expected.split('://')[1])
                self.assertEqual(params['api_base'], 'http://phoenix-gw-eval.alibaba.com/eval/v1')
                self.assertEqual(params['model'], 'openai/default')
                self.assertEqual(params['timeout'], 3600)
                for secret in ('test-key', 'test-token', 'private-route'):
                    self.assertNotIn(secret, json.dumps(params))
                resolved = resolve_environment(params, secrets)
                self.assertEqual(resolved['api_key'], 'test-key')
                self.assertEqual(resolved['extra_headers']['x-eval-token'], 'test-token')
                self.assertEqual(resolved['extra_headers']['x-smg-routing-key'], 'private-route')
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_token_and_model_override_and_missing_token(self):
        params, secrets = prepare_phoenix(self.path, model='new-model',
                                         environment={'PHOENIX_EVAL_TOKEN': 'new-token'})
        self.assertEqual(params['model'], 'openai/new-model')
        self.assertEqual(resolve_environment(params, secrets)['extra_headers']['x-eval-token'], 'new-token')
        with self.assertRaisesRegex(ValueError, 'PHOENIX_EVAL_TOKEN'):
            prepare_phoenix(self.path, environment={'PHOENIX_EVAL_TOKEN': ''})
        with self.assertRaisesRegex(ValueError, 'Set the environment variable'):
            resolve_environment(params, {})

    def test_aws_matches_b200_headers_and_keeps_route_ids_private(self):
        env = {'SGLANG_IP_PORT': '10.0.0.8:30000', 'SANDBOX_TRAJECTORY_ID': 'b200-route-123',
               'PHOENIX_DOMAIN_PROXY': 'proxy_legacy:23456', 'PHOENIX_EVAL_TOKEN': 'b200-test-token'}
        params, secrets = prepare_phoenix(self.path, routing='aws', environment=env)
        resolved = resolve_environment(params, secrets)
        headers = {k.lower(): v for k, v in resolved['extra_headers'].items()}
        self.assertEqual(headers, {
            'x-eval-token': 'b200-test-token', 'x-eval-timeout': '3600',
            'x-eval-domain-proxy': MULTICLOUD_PROXY,
            'x-backend-host': '10.0.0.8:30000', 'x-backend-region': 'us_aws',
            'x-backend-timeout': '3600', 'x-smg-routing-key': 'b200-route-123',
            'x-backend-trajectoryid': 'b200-route-123'})
        self.assertEqual(resolved['api_base'], 'http://phoenix-gw-eval.alibaba.com/eval/v1')
        self.assertEqual(resolved['model'], 'openai/default')
        self.assertEqual(resolved['timeout'], 3600)
        for private in ('b200-test-token', 'b200-route-123', 'private-route'):
            self.assertNotIn(private, json.dumps(params))
        self.assertEqual(json.loads(self.path.read_text()), self.config)

    def test_aws_requires_explicit_token_instead_of_legacy_template_token(self):
        env = {'SGLANG_IP_PORT': 'backend:30000', 'SANDBOX_TRAJECTORY_ID': 'route'}
        for token in (None, ''):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, 'PHOENIX_EVAL_TOKEN'):
                prepare_phoenix(self.path, routing='aws',
                                environment={**env, 'PHOENIX_EVAL_TOKEN': token})
        self.config['model_list'][0]['litellm_params']['extra_headers'].pop('x-eval-token')
        self.path.write_text(json.dumps(self.config))
        with self.assertRaisesRegex(ValueError, 'PHOENIX_EVAL_TOKEN'):
            prepare_phoenix(self.path, routing='aws', environment=env)
        env['PHOENIX_EVAL_TOKEN'] = 'aws-test-token'
        params, secrets = prepare_phoenix(self.path, routing='aws', environment=env)
        self.assertEqual(resolve_environment(params, secrets)['extra_headers']['x-eval-token'],
                         'aws-test-token')
        self.assertNotIn('aws-test-token', json.dumps(params))

    def test_aws_template_environment_and_cli_precedence(self):
        source = self.config['model_list'][0]['litellm_params']
        source['extra_headers'].update({
            'x-eval-domain-proxy': 'template-gateway.example',
            'X-Backend-Host': 'os.environ/TEMPLATE_HOST',
            'X-Backend-Region': 'template-region',
            'X-Backend-TrajectoryID': 'os.environ/TEMPLATE_TRAJECTORY'})
        self.path.write_text(json.dumps(self.config))
        template_env = {'PHOENIX_ROUTING': 'aws', 'TEMPLATE_HOST': 'template:123',
                        'PHOENIX_EVAL_TOKEN': 'aws-test-token',
                        'TEMPLATE_TRAJECTORY': 'template-route'}
        for overrides, env, expected in [
            ({}, template_env, ('template-gateway.example', 'template:123', 'template-region', 'template-route', 3600)),
            ({}, {**template_env, 'PHOENIX_MULTICLOUD_PROXY': 'env-gateway.example',
                  'SGLANG_IP_PORT': 'env:234', 'PHOENIX_BACKEND_REGION': 'env-region',
                  'SANDBOX_TRAJECTORY_ID': 'env-route'},
             ('env-gateway.example', 'env:234', 'env-region', 'env-route', 3600)),
            ({'proxy': 'https://cli-gateway.example', 'backend_host': 'cli:345',
              'backend_region': 'cli-region', 'trajectory_id': 'cli-route', 'timeout': 14400},
             {**template_env, 'SGLANG_IP_PORT': 'env:234', 'SANDBOX_TRAJECTORY_ID': 'env-route'},
             ('cli-gateway.example', 'cli:345', 'cli-region', 'cli-route', 14400)),
        ]:
            with self.subTest(expected=expected):
                params, secrets = prepare_phoenix(self.path, environment=env, **overrides)
                headers = resolve_environment(params, secrets)['extra_headers']
                self.assertEqual(tuple(headers[k] for k in ('x-eval-domain-proxy', 'x-backend-host',
                    'x-backend-region', 'x-backend-trajectoryid')), expected[:4])
                self.assertEqual(headers['x-smg-routing-key'], expected[3])
                self.assertEqual(headers['x-eval-timeout'], str(expected[4]))
                self.assertEqual(headers['x-backend-timeout'], str(expected[4]))
                self.assertEqual(params['timeout'], expected[4])

    def test_aws_rejects_missing_or_invalid_routing_before_launch(self):
        valid = {'SGLANG_IP_PORT': 'backend:30000', 'SANDBOX_TRAJECTORY_ID': 'route'}
        for changes in ({'SGLANG_IP_PORT': ''}, {'SANDBOX_TRAJECTORY_ID': ''},
                        {'SGLANG_IP_PORT': 'http://host:30000'}, {'SGLANG_IP_PORT': 'host'},
                        {'SGLANG_IP_PORT': 'host:0'}, {'SGLANG_IP_PORT': 'host:65536'},
                        {'SGLANG_IP_PORT': 'user@host:30000'}, {'SGLANG_IP_PORT': 'host:30000/path'},
                        {'SANDBOX_TRAJECTORY_ID': 'route\r\nx-header: bad'},
                        {'PHOENIX_BACKEND_REGION': 'us_aws\n'}, {'PHOENIX_ROUTING': 'unknown'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                prepare_phoenix(self.path, environment={'PHOENIX_ROUTING': 'aws', **valid, **changes})
        with self.assertRaisesRegex(ValueError, 'SANDBOX_TRAJECTORY_ID'):
            prepare_phoenix(self.path, routing='aws', environment={'SGLANG_IP_PORT': 'host:123'})
        for timeout in (0, -1, True, '3600', 0.5):
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, 'timeout'):
                prepare_phoenix(self.path, routing='aws', timeout=timeout, environment=valid)
        with self.assertRaisesRegex(ValueError, 'require.*aws'):
            prepare_phoenix(self.path, backend_host='backend:30000', environment={})


if __name__ == '__main__':
    unittest.main()
