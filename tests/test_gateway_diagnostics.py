import json
import asyncio
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from claude_code.phoenix_adapter import PhoenixAdapter


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_azure_cloud_tool_stream_preserves_endpoint_headers_and_budget(self):
        import httpx
        from claude_code.phoenix_config import prepare_phoenix, resolve_environment
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler as Bridge)

        captured = []

        async def send(client, request, **kwargs):
            self.assertEqual(str(request.url),
                             'http://phoenix-gw-eval.alibaba.com/eval/azure/chat/completions')
            self.assertEqual(request.headers['x-eval-token'], 'private-cloud-token')
            self.assertEqual(request.headers['tenant'], 'private-tenant')
            self.assertEqual(request.headers['Authorization'], 'Bearer private-tenant')
            self.assertEqual(request.headers['empId'], 'private-employee')
            self.assertEqual(request.headers['iai-tag'], 'test proxy')
            captured.append(json.loads(request.content))
            chunks = [{'choices': [], 'created': 0, 'id': '', 'model': '', 'object': '',
                       'prompt_filter_results': [{'prompt_index': 0, 'content_filter_results': {}}]},
                      {'id': 'chat-cloud', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'gpt-5.6-luna', 'choices': [{'index': 0, 'delta': {
                           'tool_calls': [{'index': 0, 'id': 'call-cloud', 'type': 'function',
                               'function': {'name': 'Write', 'arguments':
                                   '{"file_path":"/workspace/check.py","content":"print(1)"}'}}]},
                           'finish_reason': 'tool_calls'}]},
                      {'id': 'chat-cloud', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'gpt-5.6-luna', 'choices': [],
                       'usage': {'prompt_tokens': 12, 'completion_tokens': 32, 'total_tokens': 44}}]
            sse = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=sse + 'data: [DONE]\n\n', request=request)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'azure.yaml'
            path.write_text(json.dumps({'model_list': [{'model_name': 'repo-model', 'litellm_params': {
                'model': 'openai/gpt-5.6-luna', 'api_key': 'dummy', 'extra_headers': {
                    'x-eval-token': 'private-cloud-token', 'tenant': 'private-tenant',
                    'empId': 'private-employee', 'iai-tag': 'test proxy',
                    'x-eval-domain-proxy': 'https://iai.example'}}}]}))
            params, env = prepare_phoenix(path, routing='azure', environment={})
            PhoenixAdapter()
            with patch.object(httpx.AsyncClient, 'send', send):
                stream = await asyncio.wait_for(Bridge.async_anthropic_messages_handler(
                    **resolve_environment(params, env), max_tokens=8192, stream=True,
                    reasoning_effort='low', messages=[{'role': 'user', 'content': 'Write a file.'}],
                    tools=[{'name': 'Write', 'description': 'Write a file.', 'input_schema': {
                        'type': 'object', 'properties': {'file_path': {'type': 'string'},
                            'content': {'type': 'string'}}, 'required': ['file_path', 'content']}}],
                    timeout=2, num_retries=0), timeout=5)
                output = b''.join([chunk async for chunk in stream]).decode()
        request = captured[0]
        self.assertEqual(request['model'], 'gpt-5.6-luna')
        self.assertEqual(request['max_completion_tokens'], 8192)
        self.assertNotIn('max_tokens', request)
        self.assertEqual(request['reasoning_effort'], 'low')
        self.assertTrue(request['stream'])
        events = [json.loads(line[6:]) for line in output.splitlines() if line.startswith('data: ')]
        arguments = ''.join(e['delta']['partial_json'] for e in events
            if e['type'] == 'content_block_delta' and e['delta']['type'] == 'input_json_delta')
        self.assertEqual(json.loads(arguments), {'file_path': '/workspace/check.py', 'content': 'print(1)'})
        self.assertTrue(any(e.get('delta', {}).get('stop_reason') == 'tool_use' for e in events))
        self.assertEqual(events[-1]['type'], 'message_stop')

    async def test_installed_litellm_stream_bridge_records_actual_parameters_and_finish(self):
        output_budget = getattr(self, 'output_budget', 16384)
        import httpx
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
            LiteLLMMessagesToCompletionTransformationHandler as Bridge)

        captured = []

        def upstream(request):
            captured.append(json.loads(request.content))
            chunks = [
                {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1,
                 'model': 'default', 'choices': [{'index': 0, 'delta': {
                     'role': 'assistant', 'reasoning_content': 'private-reasoning'},
                     'finish_reason': None}]},
                {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1,
                 'model': 'default', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'length'}]},
                {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1,
                 'model': 'default', 'choices': [],
                 'usage': {'prompt_tokens': 12, 'completion_tokens': 32, 'total_tokens': 44}}]
            sse = ''.join('data: ' + json.dumps(chunk) + '\n\n' for chunk in chunks)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=sse + 'data: [DONE]\n\n', request=request)

        async def send(client, request, **kwargs):
            self.assertEqual(request.url.host, 'mock.test')
            return upstream(request)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'requests.jsonl'
            adapter = PhoenixAdapter()
            with patch.object(httpx.AsyncClient, 'send', send):
                with patch.dict(os.environ, {'NL2REPO_GATEWAY_DIAGNOSTICS': str(path)}), \
                        patch.object(litellm, 'input_callback', [adapter]), \
                        patch.object(litellm, '_async_success_callback', [adapter]):
                    stream = await asyncio.wait_for(Bridge.async_anthropic_messages_handler(
                        model='openai/default', max_tokens=output_budget, stream=True,
                        messages=[{'role': 'user', 'content': 'private-prompt'}],
                        api_key='private-key', api_base='http://mock.test/v1', timeout=2, num_retries=0,
                        extra_body={'chat_template_kwargs': {'enable_thinking': False}}), timeout=5)
                    output = b''.join([chunk async for chunk in stream])
                    # LiteLLM dispatches completed-response logging in a background task.
                    for _ in range(100):
                        if path.exists() and 'upstream_response' in path.read_text():
                            break
                        await asyncio.sleep(.01)
            self.assertEqual(captured[0]['chat_template_kwargs'], {'enable_thinking': False})
            self.assertEqual(captured[0]['max_tokens'], output_budget)
            self.assertIn(b'max_tokens', output)
            records = [json.loads(line) for line in path.read_text().splitlines()]
            requests = [r for r in records if r['event'] == 'upstream_request']
            responses = [r for r in records if r['event'] == 'upstream_response']
            self.assertTrue(requests[0]['request_parameters_available'])
            self.assertEqual(responses[0]['choices'][0]['finish_reason'], 'length')
            self.assertEqual(responses[0]['usage']['completion_tokens'], 32)
            self.assertNotIn('private-', path.read_text())

    async def test_large_output_budget_survives_protocol_conversion(self):
        self.output_budget = 393216
        await self.test_installed_litellm_stream_bridge_records_actual_parameters_and_finish()

    async def test_request_and_response_metadata_without_content_or_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'requests.jsonl'
            with patch.dict(os.environ, {'NL2REPO_GATEWAY_DIAGNOSTICS': str(path)}):
                adapter = PhoenixAdapter()
                kwargs = {'litellm_call_id': 'call-1', 'additional_args': {
                    'headers': {'Authorization': 'private-key'}, 'complete_input_dict': {
                        'messages': [{'role': 'user', 'content': 'private-prompt'}],
                        'max_tokens': 16384, 'stream': True,
                        'extra_body': {'chat_template_kwargs': {'enable_thinking': False,
                                                               'secret': 'private-template'}},
                        'api_key': 'private-key'}}}
                adapter.log_pre_api_call('default', [], kwargs)
                start = datetime.now()
                await adapter.async_log_success_event(kwargs, {
                    'choices': [{'finish_reason': 'length', 'message': {
                        'content': None, 'reasoning_content': 'private-reasoning'}}],
                    'usage': {'completion_tokens': 16384,
                              'completion_tokens_details': {'reasoning_tokens': 16384},
                              'secret': 'private-usage'}}, start, start + timedelta(seconds=3))
            text = path.read_text()
            self.assertNotIn('private-', text)
            request, response = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(request['parameters']['extra_body']['chat_template_kwargs'],
                             {'enable_thinking': False})
            self.assertEqual(response['call_id'], request['call_id'])
            self.assertEqual(response['choices'][0]['finish_reason'], 'length')
            self.assertEqual(response['choices'][0]['tool_calls'], 0)
            self.assertEqual(response['usage']['completion_tokens'], 16384)
            self.assertEqual(response['duration_ms'], 3000)

    async def test_missing_request_and_failure_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'requests.jsonl'
            with patch.dict(os.environ, {'NL2REPO_GATEWAY_DIAGNOSTICS': str(path)}):
                adapter = PhoenixAdapter()
                adapter.log_pre_api_call('default', [], {})
                now = datetime.now()
                await adapter.async_log_failure_event(
                    {'exception': ConnectionResetError('private-url')}, None, now, now)
            data = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertFalse(data[0]['request_parameters_available'])
            self.assertEqual(data[1]['error_type'], 'ConnectionResetError')
            self.assertNotIn('private-url', path.read_text())
