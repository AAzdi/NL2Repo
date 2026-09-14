"""Small, allowlisted request records; never save headers or prompt bodies."""

from datetime import datetime, timezone
import json
import logging
import os
import re
import threading


_lock = threading.Lock()
_NUMBERS = {'max_tokens', 'max_completion_tokens', 'temperature', 'top_p', 'top_k',
            'min_p', 'repetition_penalty', 'seed', 'thinking_budget', 'budget_tokens'}
_EFFORTS = {'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'default'}


def fields(value):
    if isinstance(value, dict):
        return value
    if hasattr(value, 'model_dump'):
        return value.model_dump()
    return {}


def safe_parameters(body):
    result = {}
    for key, value in fields(body).items():
        if key in _NUMBERS and isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = value
        elif key in ('stream', 'enable_thinking', 'include_usage') and isinstance(value, bool):
            result[key] = value
        elif key == 'reasoning_effort' and isinstance(value, str) and value in _EFFORTS:
            result[key] = value
        elif key == 'type' and isinstance(value, str) and value in ('enabled', 'disabled', 'adaptive'):
            result[key] = value
        elif key in ('extra_body', 'chat_template_kwargs', 'thinking', 'stream_options'):
            result[key] = safe_parameters(value)
    return result


def safe_usage(value):
    # Providers can include arbitrary metadata alongside their counters.
    allowed = {'prompt_tokens', 'completion_tokens', 'total_tokens', 'input_tokens',
               'output_tokens', 'reasoning_tokens', 'cached_tokens',
               'cache_creation_input_tokens', 'cache_read_input_tokens',
               'completion_tokens_details', 'prompt_tokens_details', 'output_tokens_details'}
    return {key: safe_usage(item) if isinstance(item, dict) else item
            for key, item in fields(value).items() if key in allowed
            and (isinstance(item, dict) or isinstance(item, (int, float)))}


def record(event, kwargs, **details):
    destination = os.environ.get('NL2REPO_GATEWAY_DIAGNOSTICS')
    if not destination:
        return
    entry = {'event': event, 'timestamp': datetime.now(timezone.utc).isoformat(),
             'call_id': kwargs.get('litellm_call_id'), **details}
    params = fields(kwargs.get('litellm_params'))
    for container in (kwargs, params):
        headers = fields(fields(container.get('proxy_server_request')).get('headers'))
        metadata = {**fields(container.get('metadata')), **fields(container.get('litellm_metadata'))}
        for key in ('task_id', 'request_id'):
            value = metadata.get('nl2repo_' + key) or headers.get('x-nl2repo-' + key.replace('_', '-'))
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', value):
                entry[key] = value
    try:
        line = json.dumps(entry, ensure_ascii=False, allow_nan=False) + '\n'
        with _lock, open(destination, 'a', encoding='utf-8') as output:
            output.write(line)
    except (OSError, ValueError, TypeError):
        logging.getLogger(__name__).warning('Could not write gateway request diagnostics')


def request_record(model, kwargs):
    body = fields(kwargs.get('additional_args')).get('complete_input_dict')
    if not isinstance(body, dict):
        # Do not label an earlier configuration snapshot as the actual request.
        record('upstream_request', kwargs, model=model, request_parameters_available=False)
        return
    record('upstream_request', kwargs, model=model, request_parameters_available=True,
           parameters=safe_parameters(body), message_count=len(body.get('messages', [])))


def response_record(kwargs, response, start_time, end_time):
    response = fields(response)
    choices = []
    for choice in response.get('choices', []):
        choice = fields(choice)
        message = fields(choice.get('message'))
        choices.append({'finish_reason': choice.get('finish_reason'),
                        'text_chars': len(message.get('content') or ''),
                        'reasoning_chars': len(message.get('reasoning_content') or ''),
                        'tool_calls': len(message.get('tool_calls') or [])})
    record('upstream_response', kwargs, choices=choices,
           response_id=response.get('id'),
           usage=safe_usage(response.get('usage')),
           usage_source='litellm_aggregate_unverified',
           duration_ms=round((end_time - start_time).total_seconds() * 1000))
