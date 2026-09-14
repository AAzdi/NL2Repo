import unittest
import asyncio
import json
from types import SimpleNamespace

from claude_code.phoenix_adapter import PhoenixAdapter, normalize_system_messages


class PhoenixAdapterTests(unittest.TestCase):
    def test_preserves_prompt_and_tool_round_trip(self):
        user = {'role': 'user', 'content': 'Build the project'}
        assistant = {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call1', 'name': 'Read', 'input': {}}]}
        tool = {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call1', 'content': 'Task text'}]}
        data = {'system': 'Original prompt', 'messages': [user, {'role': 'system', 'content': 'Budget reminder'}, assistant, tool]}
        result = normalize_system_messages(data)
        self.assertEqual(result['messages'], [user, assistant, tool])
        self.assertEqual(result['system'], [{'type': 'text', 'text': 'Original prompt'}, {'type': 'text', 'text': 'Budget reminder'}])
        self.assertEqual(len(data['messages']), 4)

    def test_preserves_system_blocks(self):
        block = {'type': 'text', 'text': 'Prompt', 'cache_control': {'type': 'ephemeral'}}
        result = normalize_system_messages({'system': [block], 'messages': [{'role': 'system', 'content': [{'type': 'text', 'text': 'Reminder'}]}]})
        self.assertEqual(result['system'][0], block)
        self.assertEqual(result['system'][1]['text'], 'Reminder')

    def test_ordinary_messages_unchanged(self):
        data = {'messages': [{'role': 'user', 'content': 'pong'}]}
        self.assertIs(normalize_system_messages(data), data)


class StreamFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_stream_emits_anthropic_error_and_preserves_failure(self):
        first = b'event: content_block_delta\ndata: {"type":"content_block_delta"}\n\n'
        failure = ConnectionResetError('private upstream URL and credentials')

        async def upstream():
            yield first
            raise failure

        stream = PhoenixAdapter().async_post_call_streaming_iterator_hook(
            None, upstream(), {'litellm_logging_obj': SimpleNamespace(call_type='anthropic_messages')})
        self.assertEqual(await anext(stream), first)
        error = await anext(stream)
        self.assertTrue(error.startswith(b'event: error\ndata: '))
        payload = json.loads(error.split(b'data: ', 1)[1])
        self.assertEqual(payload['type'], 'error')
        self.assertEqual(payload['error']['type'], 'api_error')
        self.assertNotIn(b'credentials', error)
        with self.assertRaises(ConnectionResetError) as raised:
            await anext(stream)
        self.assertIs(raised.exception, failure)

    async def test_normal_stream_is_unchanged(self):
        chunks = [b'event: message_start\n\n', b'event: message_stop\n\n']

        async def upstream():
            for chunk in chunks:
                yield chunk

        result = [c async for c in PhoenixAdapter().async_post_call_streaming_iterator_hook(
            None, upstream(), {'litellm_logging_obj': SimpleNamespace(call_type='anthropic_messages')})]
        self.assertEqual(result, chunks)

    async def test_other_protocols_and_cancellation_are_not_converted(self):
        for call_type, exc in [('acompletion', RuntimeError('failed')),
                               ('anthropic_messages', asyncio.CancelledError())]:
            async def upstream():
                raise exc
                yield

            stream = PhoenixAdapter().async_post_call_streaming_iterator_hook(
                None, upstream(), {'litellm_logging_obj': SimpleNamespace(call_type=call_type)})
            with self.assertRaises(type(exc)):
                await anext(stream)


if __name__ == '__main__':
    unittest.main()
