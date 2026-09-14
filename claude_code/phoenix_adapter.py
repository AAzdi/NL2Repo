"""Adapt SGLang prompts and streamed failures for Anthropic clients."""

import json
import re

from litellm.integrations.custom_logger import CustomLogger

from claude_code.diagnostics import record, request_record, response_record


def normalize_system_messages(data):
    messages = data.get('messages')
    if not isinstance(messages, list):
        return data
    extra = [message for message in messages if message.get('role') == 'system']
    if not extra:
        return data
    system = data.get('system', [])
    blocks = [{'type': 'text', 'text': system}] if isinstance(system, str) else list(system or [])
    for message in extra:
        content = message.get('content', '')
        if isinstance(content, str):
            blocks.append({'type': 'text', 'text': content})
        else:
            blocks.extend(content or [])
    return {
        **data,
        'system': blocks,
        'messages': [message for message in messages if message.get('role') != 'system'],
    }


class PhoenixAdapter(CustomLogger):
    def __init__(self):
        super().__init__()
        from claude_code.stream_bridge import install
        install()

    def log_pre_api_call(self, model, messages, kwargs):
        request_record(model, kwargs)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        response_record(kwargs, response_obj, start_time, end_time)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        error = kwargs.get('exception', response_obj)
        record('upstream_error', kwargs, error_type=type(error).__name__,
               status_code=getattr(error, 'status_code', None),
               duration_ms=round((end_time - start_time).total_seconds() * 1000))

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        headers = (data.get('proxy_server_request') or {}).get('headers') or {}
        for key in ('task_id', 'request_id'):
            value = headers.get('x-nl2repo-' + key.replace('_', '-'))
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
                data.setdefault('litellm_metadata', {})['nl2repo_' + key] = value
        return normalize_system_messages(data)

    async def async_post_call_streaming_iterator_hook(
            self, user_api_key_dict, response, request_data):
        # LiteLLM 1.100.0's outer SSE error serializer uses an untyped
        # data-only event. Claude Code ignores it and can accept truncated
        # output as success. Emit the Anthropic error event first, then
        # re-raise so LiteLLM retains its failure accounting and cleanup.
        call_type = getattr(request_data.get('litellm_logging_obj'), 'call_type', None)
        anthropic = call_type == 'anthropic_messages'
        try:
            async for chunk in response:
                yield chunk
        except Exception as exc:
            if anthropic:
                status = getattr(exc, 'status_code', 500)
                error_type = {400: 'invalid_request_error', 401: 'authentication_error',
                              403: 'permission_error', 404: 'not_found_error',
                              429: 'rate_limit_error', 503: 'overloaded_error',
                              504: 'timeout_error'}.get(status, 'api_error')
                # Upstream exception strings can include private URLs/headers.
                payload = {'type': 'error', 'error': {
                    'type': error_type,
                    'message': 'Upstream stream failed; response may be incomplete '
                               f'({type(exc).__name__}).',
                }}
                yield ('event: error\ndata: ' + json.dumps(payload) + '\n\n').encode()
            raise


callback = PhoenixAdapter()
