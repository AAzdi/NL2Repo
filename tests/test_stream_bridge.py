import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('LITELLM_LOCAL_MODEL_COST_MAP', 'True')

import httpx
import litellm
from claude_code.phoenix_adapter import PhoenixAdapter
from claude_code.stream_bridge import ProviderStream, IncompleteStreamError
from litellm.llms.anthropic.experimental_pass_through.adapters.handler import (
    LiteLLMMessagesToCompletionTransformationHandler as Bridge)
from litellm.llms.openai.responses.count_tokens.token_counter import OpenAITokenCounter
from litellm.types.utils import ModelResponseStream


def chunk(delta, finish=None):
    return {'id': 'chat-test', 'object': 'chat.completion.chunk', 'created': 1,
            'model': 'default', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}


def tool(index, args, name=None):
    fn = {'arguments': args}
    result = {'index': index, 'function': fn}
    if name:
        fn['name'] = name
        result.update(id=f'call_{index}', type='function')
    return result


class StreamBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def bridge(self, chunks, *, done=True):
        payload = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks)
        if done:
            payload += 'data: [DONE]\n\n'
        captured = []

        async def send(client, request, **kwargs):
            self.assertEqual(request.url.host, 'mock.test')
            captured.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content=payload, request=request)

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'audit.jsonl'
            adapter = PhoenixAdapter()
            with patch.object(httpx.AsyncClient, 'send', send), \
                    patch.dict(os.environ, {'NL2REPO_GATEWAY_DIAGNOSTICS': str(path)}), \
                    patch.object(litellm, 'input_callback', [adapter]), \
                    patch.object(litellm, '_async_success_callback', [adapter]):
                stream = await Bridge.async_anthropic_messages_handler(
                    model='openai/default', max_tokens=65536, stream=True,
                    messages=[{'role': 'user', 'content': 'test'}],
                    api_key='dummy', api_base='http://mock.test/v1', timeout=2, num_retries=0)
                # Include our outer hook: a protocol failure must reach the
                # Anthropic client as an error event, not a final message_stop.
                from types import SimpleNamespace
                guarded = adapter.async_post_call_streaming_iterator_hook(None, stream,
                    {'litellm_logging_obj': SimpleNamespace(call_type='anthropic_messages')})
                output = []
                error = None
                try:
                    async for part in guarded:
                        output.append(part)
                except Exception as exc:
                    error = exc
                await asyncio.sleep(.1)
            records = [json.loads(s) for s in path.read_text().splitlines()] if path.exists() else []
        events = [json.loads(s[6:]) for s in b''.join(output).decode().splitlines() if s.startswith('data: {')]
        return events, error, records

    async def test_clean_eof_and_done_without_provider_finish_are_errors(self):
        for done in (False, True):
            for arguments in ('', '{"file_path": "retrying.py"'):
                with self.subTest(done=done, arguments=arguments):
                    events, error, records = await self.bridge([
                        chunk({'reasoning_content': 'Plan'}),
                        chunk({'tool_calls': [tool(0, arguments, 'Write')]})], done=done)
                    self.assertIsNotNone(error)
                    self.assertTrue(any(e['type'] == 'error' for e in events))
                    self.assertFalse(any(e['type'] == 'message_stop' for e in events))
                    self.assertFalse(any(r['event'] == 'upstream_response' for r in records))
                    record = next(r for r in records if r['event'] == 'provider_stream_end')
                    self.assertEqual(record['status'], 'incomplete_stream')
                    self.assertFalse(record['provider_usage_seen'])

    async def test_interleaved_parallel_tools_and_combined_finish_remain_separate(self):
        first = json.dumps({'file_path': 'a.py', 'content': 'print("中文")\n' * 8000})
        second = json.dumps({'command': 'echo done'})
        chunks = [chunk({'reasoning_content': 'plan'}),
                  chunk({'tool_calls': [tool(0, first[:12], 'Write'), tool(1, second[:8], 'Bash')]}),
                  chunk({'tool_calls': [tool(1, second[8:])]}),
                  chunk({'tool_calls': [tool(0, first[12:])]}, 'tool_calls'),
                  {'id': 'chat-test', 'choices': [], 'usage': {
                      'prompt_tokens': 10, 'completion_tokens': 655, 'total_tokens': 665}}]
        events, error, records = await self.bridge(chunks)
        self.assertIsNone(error)
        blocks = {}
        for event in events:
            if event['type'] == 'content_block_start' and event['content_block']['type'] == 'tool_use':
                blocks[event['index']] = {'name': event['content_block']['name'], 'arguments': ''}
            if event['type'] == 'content_block_delta' and event['delta']['type'] == 'input_json_delta':
                blocks[event['index']]['arguments'] += event['delta']['partial_json']
        self.assertEqual([b['name'] for b in blocks.values()], ['Write', 'Bash'])
        self.assertEqual([b['arguments'] for b in blocks.values()], [first, second])
        terminal = next(e for e in events if e['type'] == 'message_delta')
        self.assertEqual(terminal['delta']['stop_reason'], 'tool_use')
        self.assertEqual(terminal['usage']['output_tokens'], 655)
        record = next(r for r in records if r['event'] == 'provider_stream_end')
        self.assertEqual(record['provider_finish_reason'], 'tool_calls')
        self.assertTrue(record['provider_usage_seen'])
        self.assertEqual(record['usage']['completion_tokens'], 655)
        self.assertNotIn('print', json.dumps(records))

    async def test_token_counter_forwards_headers_and_bounds_its_timeout(self):
        observed = []

        class Client:
            async def post(self, url, **kwargs):
                observed.append(kwargs)
                return httpx.Response(200, json={'input_tokens': 21}, request=httpx.Request('POST', url))

        with patch('litellm.llms.custom_httpx.http_handler.get_async_httpx_client', return_value=Client()):
            result = await OpenAITokenCounter().count_tokens('default', [{'role': 'user', 'content': 'hi'}],
                None, {'litellm_params': {'api_base': 'http://mock.test/v1', 'api_key': 'dummy',
                    'timeout': 3600, 'extra_headers': {'x-eval-token': 'test-token', 'x-backend-host': 'test:1'}}})
        self.assertEqual(result.total_tokens, 21)
        self.assertEqual(observed[0]['headers']['x-eval-token'], 'test-token')
        self.assertEqual(observed[0]['headers']['x-backend-host'], 'test:1')
        self.assertEqual(observed[0]['timeout'], 30)

    def test_sync_eof_is_rejected_and_provider_usage_can_be_unknown(self):
        with self.assertRaises(IncompleteStreamError):
            list(ProviderStream(iter([ModelResponseStream(**chunk({'content': 'partial'}))])))
        stream = ProviderStream(iter([ModelResponseStream(**chunk({'content': 'done'}, 'stop'))]))
        result = list(stream)
        self.assertEqual(result[-1].choices[0].finish_reason, 'stop')
        self.assertIsNone(stream.usage)

    async def test_malformed_arguments_with_finish_do_not_reach_tools(self):
        events, error, _ = await self.bridge([chunk({'tool_calls': [tool(0, '{', 'Write')]}, 'stop')])
        self.assertIsNotNone(error)
        self.assertFalse(any(e.get('content_block', {}).get('type') == 'tool_use' for e in events))
