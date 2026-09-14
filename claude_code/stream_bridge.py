"""Gateway-local compatibility fixes, before LiteLLM synthesizes terminal events.

Installed by our gateway callback; no changes to site-packages are required.
The adapter is exercised against the installed LiteLLM by protocol regression tests.
"""

from collections import deque
import json

from claude_code.diagnostics import fields, record, safe_usage


class IncompleteStreamError(RuntimeError):
    status_code = 502


class ProviderStream:
    """Require a provider finish, and serialize tools by index (including interleaving).

    Text/reasoning stays live. Tool arguments are held until the provider finishes,
    so an EOF cannot execute a partially received tool. Only structural diagnostics
    are persisted; this does not save an additional raw response body.
    """

    def __init__(self, source, logging_obj=None):
        self.source = source
        self.logging_obj = logging_obj
        self.pending = deque()
        self.tools = {}
        self.finish = None
        self.usage = None
        self.recorded = False
        self.sync = None
        self.asynchronous = None

    def report(self, status):
        if self.recorded:
            return
        self.recorded = True
        record('provider_stream_end', getattr(self.logging_obj, 'model_call_details', {}),
               status=status, provider_finish_reason=self.finish,
               provider_usage_seen=self.usage is not None,
               usage=safe_usage(self.usage), usage_source='provider' if self.usage is not None else 'unknown',
               tools=[{'index': index, 'name': tool['name'],
                       'argument_chars': sum(map(len, tool['arguments']))}
                      for index, tool in self.tools.items()])

    def accept(self, chunk):
        from litellm.types.utils import ModelResponseStream
        data = fields(chunk)
        # The base iterator represents [DONE]/SSE comments as GenericStreamingChunk.
        # Those synthetic stop values must never count as a provider finish_reason.
        if 'choices' not in data:
            return
        if data.get('usage') is not None:
            self.usage = data['usage']
        choices = data.get('choices') or []
        if not choices:
            self.pending.append(chunk)
            return
        if len(choices) != 1 or choices[0].get('index', 0) != 0:
            raise IncompleteStreamError('Unsupported multiple response choices')
        choice = choices[0]
        delta = choice.get('delta') or {}
        finish = choice.get('finish_reason')
        if self.finish is not None:
            if any(delta.get(key) for key in ('content', 'reasoning_content', 'tool_calls')):
                raise IncompleteStreamError('Content received after provider finish')
            if data.get('usage') is not None:
                self.pending.append(ModelResponseStream(**{**data, 'choices': []}))
            return
        for call in delta.get('tool_calls') or []:
            index = call.get('index', 0)
            tool = self.tools.setdefault(index, {'id': None, 'name': None, 'arguments': []})
            fn = call.get('function') or {}
            for key, value in (('id', call.get('id')), ('name', fn.get('name'))):
                if value:
                    if tool[key] not in (None, value):
                        raise IncompleteStreamError('Tool identity changed within a stream')
                    tool[key] = value
            if fn.get('arguments'):
                tool['arguments'].append(fn['arguments'])
        # Separate content from terminal and tool chunks. This also avoids the
        # upstream chunk builder losing finish_reason on a combined last chunk.
        content = {k: v for k, v in delta.items() if k != 'tool_calls' and v is not None}
        if any(content.get(k) for k in ('content', 'reasoning_content', 'thinking_blocks', 'role')):
            value = {**data, 'choices': [{**choice, 'delta': content, 'finish_reason': None}]}
            value.pop('usage', None)
            self.pending.append(ModelResponseStream(**value))
        if finish is not None:
            self.finish = finish
            for index, tool in self.tools.items():
                arguments = ''.join(tool['arguments'])
                try:
                    valid = isinstance(json.loads(arguments), dict)
                except (ValueError, TypeError):
                    valid = False
                if not valid or not tool['id'] or not tool['name']:
                    raise IncompleteStreamError('Provider finished with incomplete tool arguments')
                value = {**data, 'choices': [{**choice, 'finish_reason': None, 'delta': {'tool_calls': [{
                    'index': index, 'id': tool['id'], 'type': 'function',
                    'function': {'name': tool['name'], 'arguments': arguments}}]}}]}
                value.pop('usage', None)
                self.pending.append(ModelResponseStream(**value))
            self.pending.append(ModelResponseStream(**{
                **data, 'choices': [{**choice, 'delta': {}, 'finish_reason': finish}]}))
        elif data.get('usage') is not None:
            self.pending.append(ModelResponseStream(**{**data, 'choices': []}))

    def end(self):
        if self.finish is None:
            self.report('incomplete_stream')
            raise IncompleteStreamError('Provider stream ended without a finish_reason')
        self.report('completed')

    def __iter__(self):
        if self.sync is None:
            self.sync = iter(self.source)
        return self

    def __next__(self):
        if self.sync is None:
            iter(self)
        while not self.pending:
            try:
                chunk = next(self.sync)
            except StopIteration:
                self.end()
                raise
            except BaseException:
                self.report('interrupted')
                raise
            try:
                self.accept(chunk)
            except Exception:
                self.report('invalid_stream')
                raise
        return self.pending.popleft()

    def __aiter__(self):
        if self.asynchronous is None:
            self.asynchronous = self.source.__aiter__()
        return self

    async def __anext__(self):
        if self.asynchronous is None:
            self.__aiter__()
        while not self.pending:
            try:
                chunk = await self.asynchronous.__anext__()
            except StopAsyncIteration:
                self.end()
                raise
            except BaseException:
                self.report('interrupted')
                raise
            try:
                self.accept(chunk)
            except Exception:
                self.report('invalid_stream')
                raise
        return self.pending.popleft()

    async def aclose(self):
        self.report('cancelled')
        close = getattr(self.source, 'aclose', None)
        if close:
            await close()

    def close(self):
        self.report('cancelled')
        close = getattr(self.source, 'close', None)
        if close:
            close()


def install():
    """Install once in this gateway process, for its OpenAI-compatible provider."""
    import litellm
    from litellm.llms.openai.responses.count_tokens.token_counter import OpenAITokenCounter
    if getattr(litellm.CustomStreamWrapper, '_nl2repo_guard', False):
        return
    original = litellm.CustomStreamWrapper.__init__

    def initialize(self, completion_stream, model, logging_obj, custom_llm_provider=None, **kwargs):
        if custom_llm_provider == 'openai' and not isinstance(completion_stream, ProviderStream):
            completion_stream = ProviderStream(completion_stream, logging_obj)
        original(self, completion_stream=completion_stream, model=model, logging_obj=logging_obj,
                 custom_llm_provider=custom_llm_provider, **kwargs)

    litellm.CustomStreamWrapper.__init__ = initialize
    litellm.CustomStreamWrapper._nl2repo_guard = True
    original_count = OpenAITokenCounter.count_tokens

    async def count(self, model_to_use, messages, contents, deployment=None,
                    request_model='', tools=None, system=None):
        params = (deployment or {}).get('litellm_params', {})
        if not params.get('extra_headers'):
            return await original_count(self, model_to_use, messages, contents, deployment,
                                        request_model, tools, system)
        from litellm.llms.openai.responses.count_tokens.transformation import OpenAICountTokensConfig
        from litellm.llms.custom_httpx.http_handler import get_async_httpx_client
        from litellm.types.utils import TokenCountResponse
        if not messages:
            return None
        config = OpenAICountTokensConfig()
        items, instructions = config.messages_to_responses_input(messages)
        if not items:
            return None
        body = config.transform_request_to_count_tokens(
            model_to_use, items, tools, instructions if instructions is not None else system)
        headers = {**config.get_required_headers(params.get('api_key', 'dummy')),
                   **params['extra_headers']}
        timeout = min(30, float(params.get('timeout', 30)))
        try:
            client = get_async_httpx_client(llm_provider=litellm.LlmProviders.OPENAI)
            response = await client.post(config.get_openai_count_tokens_endpoint(params.get('api_base')),
                                         headers=headers, json=body, timeout=timeout)
            response.raise_for_status()
            total = response.json()['input_tokens']
            if not isinstance(total, int) or isinstance(total, bool) or total < 0:
                raise ValueError('Invalid token count')
            return TokenCountResponse(total_tokens=total, request_model=request_model,
                                      model_used=model_to_use, tokenizer_type='openai_api')
        except Exception:
            # The proxy falls back to local counting on error=True. Do not echo
            # upstream bodies, which may contain routing headers or credentials.
            return TokenCountResponse(total_tokens=0, request_model=request_model,
                                      model_used=model_to_use, tokenizer_type='openai_api',
                                      error=True, error_message='Provider token count unavailable; using local estimate',
                                      status_code=502)

    OpenAITokenCounter.count_tokens = count
