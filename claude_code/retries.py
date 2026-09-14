"""Bounded retries for interrupted execution, never for a better test score."""

import math
import re
import subprocess
import threading


def retry_options(options):
    # Old direct-run configurations remain single-attempt. The shipped template
    # and launcher enable retries explicitly and persist the effective settings.
    result = {name: options.get(name, default) for name, default in (
        ('generation_retries', 0), ('evaluation_retries', 0), ('retry_delay_seconds', 5))}
    result['generation_budget_seconds'] = options.get(
        'generation_budget_seconds', options.get('timeout_seconds', 7200))
    budget = result['generation_budget_seconds']
    if not isinstance(budget, int) or isinstance(budget, bool) or budget <= 0:
        raise ValueError('generation_budget_seconds must be a positive integer')
    for name in ('generation_retries', 'evaluation_retries'):
        value = result[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f'{name} must be a non-negative integer')
    delay = result['retry_delay_seconds']
    if (not isinstance(delay, (int, float)) or isinstance(delay, bool)
            or not math.isfinite(delay) or not 0 <= delay <= 60):
        raise ValueError('retry_delay_seconds must be between 0 and 60')
    return result


_TRANSIENT = re.compile(
    r'connection (?:reset|refused|aborted)|RemoteDisconnected|'
    r'temporary failure in name resolution|name or service not known|'
    r'network is unreachable|read timed out|readtimeout|connecttimeout|'
    r'timeout awaiting response|TLS handshake timeout|i/o timeout|'
    r'unexpected EOF|broken pipe|dependency upstream unavailable|'
    r'cannot connect to the docker daemon|container .* is not running|'
    r'HTTP (?:error )?(?:408|429|5\d\d)|too many requests|'
    r'service unavailable|bad gateway|gateway timeout', re.I)


def transient_exception(exc):
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError, ConnectionError)):
        return True
    return bool(_TRANSIENT.search(' '.join(str(v) for v in (
        exc, getattr(exc, 'stderr', ''), getattr(exc, 'output', '')))))


def generation_retry_reason(result):
    """Only retry preparation, before a generation process could modify code.

    API/stream recovery belongs to the client's request loop. Once that client
    exits, preserve its workspace and report the failure, never regenerate it.
    """
    if result.get('status') == 'interrupted' or result.get('generation_status') == 'success':
        return None
    if result.get('stage') != 'preparing' or result.get('generation_started_at'):
        return None
    if result.get('failure_kind') == 'environment_error' and result.get('transient_execution_error'):
        return 'transient_execution_error'
    return None


def evaluation_retry_reason(post):
    if post.get('status') == 'success' and post.get('evaluation_valid', post.get('score_valid', False)):
        return None
    if post.get('failure_kind') == 'candidate_error':
        return None
    # Only inspect operationally failed commands. Ordinary pytest exit 1/2 and
    # collection/import errors must not trigger retries, even if their tracebacks
    # happen to contain network-error strings.
    for details in (post, post.get('artifact_install', {}), post.get('test_results', {})):
        if details.get('failure_kind') == 'candidate_error':
            continue
        if details.get('container_diagnostics', {}).get('state', {}).get('Running') is False:
            return 'container_stopped'
        if details.get('execution_status') == 'timeout':
            return 'evaluation_timeout'
        if details.get('last_exit_code') in (137, 143):
            return 'container_process_terminated'
        if _TRANSIENT.search(str(details.get('error', ''))):
            return 'transient_evaluation_error'
        for command in details.get('command_results', []):
            if command.get('status') in ('timeout', 'error'):
                if command['status'] == 'timeout' or _TRANSIENT.search(command.get('output', '')):
                    return 'transient_command_error'
            if (command.get('exit_code') not in (None, 0)
                    and not re.search(r'\bpytest\b', command.get('command', ''))
                    and _TRANSIENT.search(command.get('output', ''))):
                return 'dependency_transport_error'
    return None


def retry_delay(delay, retry_number):
    return min(60, delay * 2 ** min(retry_number - 1, 6))


def wait_before_retry(cancel, delay, retry_number):
    """Exponential backoff, capped at 60 seconds and interruptible on shutdown."""
    return not (cancel or threading.Event()).wait(retry_delay(delay, retry_number))
