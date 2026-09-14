"""Validated provider generation settings, separate from routing and credentials."""

from copy import deepcopy
import math


EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'default')
FIELDS = {'temperature', 'top_p', 'max_tokens', 'max_completion_tokens',
          'reasoning_effort', 'seed', 'extra_body'}


def token_limits(options):
    limits = {key: options[key] for key in ('context_length', 'max_output_tokens',
                                           'model_timeout_seconds') if key in options}
    for key, value in limits.items():
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f'{key} must be a positive integer')
    if ('context_length' in limits and 'max_output_tokens' in limits
            and limits['max_output_tokens'] >= limits['context_length']):
        raise ValueError('context_length must exceed max_output_tokens to leave room for input')
    return limits


def number(name, value, minimum, maximum=None, integer=False):
    if (isinstance(value, bool) or not isinstance(value, int if integer else (int, float))
            or not math.isfinite(value) or value < minimum
            or (maximum is not None and value > maximum)):
        raise ValueError(f'Invalid generation parameter: {name}')


def template_kwargs(value):
    if not isinstance(value, dict) or set(value) - {'enable_thinking', 'thinking_budget', 'reasoning_effort'}:
        raise ValueError('chat_template_kwargs supports enable_thinking, thinking_budget, reasoning_effort')
    if 'enable_thinking' in value and not isinstance(value['enable_thinking'], bool):
        raise ValueError('enable_thinking must be boolean')
    if 'thinking_budget' in value:
        number('thinking_budget', value['thinking_budget'], 0, integer=True)
    if 'reasoning_effort' in value and value['reasoning_effort'] not in EFFORTS:
        raise ValueError('Invalid template reasoning_effort')
    return deepcopy(value)


def generation_parameters(source):
    result = deepcopy({key: source[key] for key in FIELDS if key in source})
    for name, bounds in {'temperature': (0, 2), 'top_p': (0, 1)}.items():
        if name in result:
            number(name, result[name], *bounds)
    for name in ('max_tokens', 'max_completion_tokens', 'seed'):
        if name in result:
            number(name, result[name], 0 if name == 'seed' else 1, integer=True)
    if 'reasoning_effort' in result and result['reasoning_effort'] not in EFFORTS:
        raise ValueError('Invalid reasoning_effort')
    if 'extra_body' in result:
        extra = result['extra_body']
        if not isinstance(extra, dict) or set(extra) - {
                'chat_template_kwargs', 'top_k', 'min_p', 'repetition_penalty'}:
            raise ValueError('extra_body supports only chat_template_kwargs, top_k, min_p, repetition_penalty')
        if 'chat_template_kwargs' in extra:
            template_kwargs(extra['chat_template_kwargs'])
        for name, bounds in {'top_k': (-1, None), 'min_p': (0, 1),
                             'repetition_penalty': (0, None)}.items():
            if name in extra:
                number(name, extra[name], *bounds, integer=name == 'top_k')
    return result
