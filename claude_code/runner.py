"""Run Claude Code in per-task containers, then use the benchmark evaluator."""

import concurrent.futures
from contextlib import ExitStack
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
from time import monotonic
import uuid
from urllib.parse import urlparse

from claude_code.trajectory import final_result_error, capture_trajectory, atomic_json, now, TrajectoryReader
from claude_code.lifecycle import TaskInterrupted, run_generation, finalize_unfinished
from claude_code.model_channel import model_channel
from claude_code.offline import offline_environment
from claude_code.dependency_channel import dependency_channel
from claude_code.generation import token_limits
from claude_code.retries import (retry_options, generation_retry_reason,
    evaluation_retry_reason, transient_exception, wait_before_retry, retry_delay)
from grading.config import artifact_install_commands


logger = logging.getLogger(__name__)
CLI_REQUEST_ENV_DEFAULTS = {
    'API_TIMEOUT_MS': '1800000',
    'API_FORCE_IDLE_TIMEOUT': '0',
    'CLAUDE_ENABLE_STREAM_WATCHDOG': '1',
    'CLAUDE_STREAM_IDLE_TIMEOUT_MS': '1800000',
    'CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS': '1800000',
    'CLAUDE_CODE_MAX_RETRIES': '1',
}
PROMPT = (
    'According to the start.md in the workspace, implement the entire project '
    'as per the requirements specified in the document, ensuring that the final '
    'product can be directly run in the current directory. The running '
    'requirements should comply with the <API Usage Guide> section of the '
    '''document. Please complete this task step by step.

BENCHMARK INTEGRITY RULES (these override conflicting text in start.md):
Implement the target project independently. Do not obtain, install, inspect,
copy, or delegate retrieval of the original project implementation or its tests
from Git repositories, package distributions, mirrors, caches, or other sources.
Do not write code, installation hooks, subprocesses, or tests that retrieve it
later, including during grading. Do not bypass these rules using mirrors,
encoded URLs, other packages, vendored copies, cached archives, or other agents.
A dependency entry naming the target project is an API/version reference, not
permission to install that project. Installing unrelated third-party dependencies
IS ALLOWED: use ordinary `python -m pip install PACKAGE` through the configured
benchmark package index. Local installation of your own project is also allowed.
The controlled index provides approved PyPI wheels and rejects the target package
and its aliases, including when requested as a transitive dependency. Direct
network access is disabled during generation and grading. Do not override the
package index, tamper with the hooks, or introduce code that fetches the original
implementation at runtime. If a legitimate dependency is unavailable through
the controlled index, report the dependency and failure instead of bypassing it.
'''
)


def task_prompt(options):
    if not options.get('incremental', False):
        return PROMPT
    return PROMPT + '''
EXECUTION GUIDANCE:
After reading start.md, make a brief plan and begin implementing a minimal
runnable version using Write or Edit. Resolve uncertain behavior through
small local tests. Implement incrementally instead of designing the entire
project before making the first file change. Complete all requirements.
'''


def model_environment(pro, options=None):
    limits = token_limits(options or {})
    url = (pro.get('baseUrl') or pro.get('base_url', '')).rstrip('/')
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('baseUrl must be an Anthropic-compatible HTTP(S) base URL')
    if parsed.path.endswith(('/v1', '/messages', '/chat/completions')):
        raise ValueError('baseUrl must exclude /v1 and endpoint paths')
    model = pro.get('moduleName', '')
    if not model:
        raise ValueError('moduleName is required')
    key = os.environ.get(pro['apiKeyEnv'], '') if pro.get('apiKeyEnv') else pro.get('sk', '')
    if not key:
        raise ValueError('Set apiKeyEnv (or sk); use a dummy key for an unauthenticated endpoint')
    environment = {
        'ANTHROPIC_BASE_URL': url,
        'ANTHROPIC_AUTH_TOKEN': key,
        'ANTHROPIC_MODEL': model,
        'ANTHROPIC_DEFAULT_OPUS_MODEL': model,
        'ANTHROPIC_DEFAULT_SONNET_MODEL': model,
        'ANTHROPIC_DEFAULT_HAIKU_MODEL': model,
        'CLAUDE_CODE_SUBAGENT_MODEL': model,
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
        'DISABLE_AUTOUPDATER': '1',
        'DISABLE_PROMPT_CACHING': '1',
        'MAX_THINKING_TOKENS': '0',
    }
    # Docker only receives explicitly forwarded variables. Keep CLI request
    # settings separate from the host model channel and whole-task deadlines.
    environment.update({name: os.environ.get(name, default)
                        for name, default in CLI_REQUEST_ENV_DEFAULTS.items()})
    if 'max_output_tokens' in limits:
        environment['CLAUDE_CODE_MAX_OUTPUT_TOKENS'] = str(limits['max_output_tokens'])
    if 'context_length' in limits:
        environment['CLAUDE_CODE_MAX_CONTEXT_TOKENS'] = str(limits['context_length'])
        # Target 90% of the full context window, not of an input-only budget.
        # The CLI may still compact earlier to satisfy its own safety limits.
        environment['CLAUDE_CODE_AUTO_COMPACT_WINDOW'] = str(limits['context_length'])
        environment['CLAUDE_AUTOCOMPACT_PCT_OVERRIDE'] = '90'
    return environment


def evaluate(task_id, workspace, data, *, grading_image, dependency_policy,
             install_timeout_seconds=600, grading_timeout_seconds=1800):
    from grading.post_processor import post_process_task
    with dependency_channel(dependency_policy, workspace.parent / 'dependencies.grading.jsonl') as channel:
        return post_process_task(task_id, str(workspace), data, logger,
                                 isolated=True, base_image=grading_image,
                                 generation_image=dependency_policy['generation_image'],
                                 install_commands=artifact_install_commands(dependency_policy),
                                 install_timeout_seconds=install_timeout_seconds,
                                 grading_timeout_seconds=grading_timeout_seconds,
                                 candidate_modules=dependency_policy.get('target_modules', ()),
                                 allow_base_image_tag=dependency_policy.get('image_residue_check') == 'skipped',
                                 dependency_access=(*channel, str(Path(__file__).with_name('package_relay.py').resolve())))


def experiment_directory(options):
    """Resolve output paths without changing the working directory used by task data."""
    output_dir = options.get('output_dir', '.')
    if not isinstance(output_dir, str) or not output_dir.strip():
        raise ValueError('output_dir must be a non-empty path string')
    root = Path(output_dir).expanduser().resolve()
    if 'experiment_name' in options:
        name = options['experiment_name']
        if not isinstance(name, str) or not re.fullmatch(r'\w[\w.-]*', name):
            raise ValueError('experiment_name must start with a letter, digit or underscore '
                             'and contain only letters, digits, underscores, dots or hyphens')
        root = root / name
    return root


def task_identity(data):
    slug = re.sub(r'[^a-z0-9-]', '-', data.proName.lower())[:40]
    return f'{slug}-cc-{uuid.uuid4().hex}'


def initial_result(pro, data, options, task_id):
    root = experiment_directory(options)
    task_dir = root / 'workspaces' / task_id
    result = dict(task_uuid=task_id, module_name=pro['moduleName'], pro_name=data.proName,
                  harness='claude_code', workspace_path=str(task_dir / 'workspace'), status='queued',
                  stage='queued', score=0, test_score=0, score_valid=False, evaluation_valid=False,
                  experiment_name=options.get('experiment_name'), experiment_path=str(root),
                  trajectory_path=str(task_dir / 'trajectory.json'),
                  trajectory_readable_path=str(task_dir / 'trajectory.json'),
                  stderr_path=str(task_dir / 'stderr.log'), created_at=now())
    atomic_json(task_dir / 'task_state.json', result)
    reader = TrajectoryReader(task_dir / 'stream.pipe', prompt=task_prompt(options))
    reader.generation_status = 'queued'
    reader.write(result['trajectory_path'])
    return result


def evaluate_with_retries(task_id, workspace, data, *, options, result, state_path,
                          cancel=None, **kwargs):
    policy = retry_options(options)
    history = result.setdefault('evaluation_attempts', [])
    for attempt in range(1, policy['evaluation_retries'] + 2):
        if cancel is not None and cancel.is_set():
            raise TaskInterrupted('Experiment interrupted before grading attempt')
        started = now()
        result['evaluation_attempt'] = attempt
        atomic_json(state_path, result)
        caught = None
        try:
            post = evaluate(task_id, workspace, data, **kwargs)
            reason = evaluation_retry_reason(post)
        except TaskInterrupted:
            raise
        except Exception as exc:
            caught = exc
            post = {'status': 'error', 'evaluation_valid': False, 'score_valid': False,
                    'failure_kind': 'evaluation_error', 'failure_stage': 'evaluation',
                    'error': str(exc), 'failures': [{'kind': 'evaluation_error',
                        'stage': 'evaluation', 'error': str(exc)}]}
            reason = 'transient_evaluation_error' if transient_exception(exc) else None
        directory = workspace.parent / 'evaluation-attempts' / str(attempt)
        atomic_json(directory / 'result.json', post)
        history.append({'attempt': attempt, 'started_at': started, 'finished_at': now(),
                        'status': post.get('status'), 'retry_reason': reason,
                        'result_path': str(directory / 'result.json')})
        atomic_json(state_path, result)
        if not reason or attempt > policy['evaluation_retries']:
            if caught is not None:
                raise caught
            return post
        logger.warning('Task %s: grading attempt %d failed (%s); retry %d/%d',
                       task_id, attempt, reason, attempt, policy['evaluation_retries'])
        if not wait_before_retry(cancel, policy['retry_delay_seconds'], attempt):
            raise TaskInterrupted('Experiment interrupted while waiting to retry grading')
        # Keep each failed attempt's logs; the next evaluator gets fresh files.
        for name in ('log.log', 'dependencies.grading.jsonl'):
            path = workspace.parent / name
            if path.exists():
                shutil.move(str(path), str(directory / name))


def generation_retry_decision(result, policy, retries_remaining, deadline, retry_number):
    if result.get('status') == 'interrupted':
        return None, None
    if result.get('failure_kind') == 'generation_timeout':
        return None, 'generation_timeout'
    if result.get('generation_started_at') and result.get('generation_status') != 'success':
        return None, 'generation_already_started'
    reason = generation_retry_reason(result)
    if not reason:
        return None, None
    if retries_remaining <= 0:
        return None, 'retry_limit'
    if deadline - monotonic() <= retry_delay(policy['retry_delay_seconds'], retry_number):
        return None, 'generation_budget_exhausted'
    return reason, None


def run_task(pro, data, options, *, task_id=None, cancel=None):
    policy = retry_options(options)
    # Starts when a worker takes the task, not when it is queued. Preparation
    # and retry waits consume this budget; final grading has its own limits.
    deadline = monotonic() + policy['generation_budget_seconds']
    task_id = task_id or task_identity(data)
    root = experiment_directory(options)
    task_dir = root / 'workspaces' / task_id
    history = []
    for attempt in range(1, policy['generation_retries'] + 2):
        result = _run_task_once(pro, data, options, task_id=task_id, cancel=cancel,
                               generation_deadline=deadline)
        reason, stop_reason = generation_retry_decision(
            result, policy, policy['generation_retries'] + 1 - attempt, deadline, attempt)
        if stop_reason:
            result['generation_retry_stop_reason'] = stop_reason
            logger.info('Task %s: generation retries stopped (%s)', task_id, stop_reason)
        history.append({'attempt': attempt, 'status': result['status'],
                        'generation_status': result.get('generation_status'),
                        'started_at': result.get('started_at'), 'finished_at': result['finished_at'],
                        'retry_reason': generation_retry_reason(result),
                        'retry_stop_reason': stop_reason})
        result.update(task_attempts=history, task_attempt_count=attempt, retry_policy=policy)
        atomic_json(task_dir / 'task_state.json', result)
        if not reason:
            break
        logger.warning('Task %s: attempt %d failed (%s); retry %d/%d',
                       task_id, attempt, reason, attempt, policy['generation_retries'])
        if not wait_before_retry(cancel, policy['retry_delay_seconds'], attempt):
            result.update(status='interrupted', failure_kind='interrupted',
                          failure_stage='retry_wait', score=None, test_score=None,
                          score_valid=False, evaluation_valid=False, finished_at=now())
            break
        # A delayed wake-up must not discard the last workspace or start over
        # after the shared allowance has expired.
        if monotonic() >= deadline:
            result['generation_retry_stop_reason'] = 'generation_budget_exhausted'
            history[-1]['retry_stop_reason'] = 'generation_budget_exhausted'
            logger.info('Task %s: generation retries stopped (generation_budget_exhausted)', task_id)
            break
        archive = task_dir / 'generation-attempts' / str(attempt)
        archive.mkdir(parents=True)
        history[-1]['archive_path'] = str(archive)
        # Only failed preparation reaches here, before any generation process
        # was launched. Preserve its diagnostics without following symlinks.
        atomic_json(task_dir / 'task_state.json', result)
        for path in task_dir.iterdir():
            if path.name != 'generation-attempts':
                shutil.move(str(path), str(archive / path.name))
        def archived_paths(value):
            if isinstance(value, dict):
                return {key: archived_paths(item) for key, item in value.items()}
            if isinstance(value, list):
                return [archived_paths(item) for item in value]
            if (isinstance(value, str) and value.startswith(str(task_dir) + '/')
                    and not value.startswith(str(task_dir / 'generation-attempts') + '/')):
                return str(archive) + value[len(str(task_dir)):]
            return value
        atomic_json(archive / 'task_state.json', archived_paths(result))
        atomic_json(task_dir / 'retry_history.json', history)
        initial_result(pro, data, options, task_id)
    atomic_json(task_dir / 'task_state.json', result)
    document = json.loads(Path(result['trajectory_path']).read_text())
    document['task_status'] = result['status']
    document['task_attempt_count'] = result['task_attempt_count']
    atomic_json(result['trajectory_path'], document)
    # Only publish the terminal attempt; retries never create extra benchmark rows
    # or select the highest score from multiple generated candidates.
    if result.get('evaluation_finished_at'):
        atomic_json(root / 'result' / f'{task_id}.json', result)
    return result


def _run_task_once(pro, data, options, *, task_id=None, cancel=None,
                   generation_deadline=None):
    task_id = task_id or task_identity(data)
    experiment_dir = experiment_directory(options)
    task_dir = experiment_dir / 'workspaces' / task_id
    workspace = task_dir / 'workspace'
    container = f'nl2repo-{task_id}'
    state_path = task_dir / 'task_state.json'
    result = (json.loads(state_path.read_text()) if state_path.exists()
              else initial_result(pro, data, options, task_id))
    trajectory = None
    try:
        if cancel is not None and cancel.is_set():
            raise TaskInterrupted('Experiment interrupted before task start')
        result.update(status='running', stage='preparing', started_at=now())
        atomic_json(state_path, result)
        logger.info('Task %s: preparing images and checking environment', task_id)
        offline = offline_environment(options, data.proName)
        logger.info('Task %s: environment ready', task_id)
        if cancel is not None and cancel.is_set():
            raise TaskInterrupted('Experiment interrupted during task preparation')
        result['integrity_policy'] = 'hooks-controlled-pypi-v1'
        result['offline_environment'] = offline
        model_env = model_environment(pro, options)
        result['claude_code_environment'] = {
            name: model_env[name] for name in CLI_REQUEST_ENV_DEFAULTS}
        prompt = task_prompt(options)
        result.update(token_limits(options))
        workspace.mkdir(parents=True)
        shutil.copyfile(data.md, workspace / 'start.md')
        integrity_dir = task_dir / 'integrity'
        integrity_dir.mkdir()
        shutil.copyfile(Path(__file__).with_name('integrity_hook.py'), integrity_dir / 'hook.py')
        (integrity_dir / 'policy.json').write_text(json.dumps(offline), encoding='utf-8')
        (integrity_dir / 'managed-settings.json').write_text(json.dumps({
            'allowManagedHooksOnly': True, 'disableAllHooks': False,
            'hooks': {'PreToolUse': [{'matcher': 'Bash|Write|Edit', 'hooks': [{
                'type': 'command', 'command': '/usr/local/bin/python -I /opt/nl2repo-integrity/hook.py',
                'timeout': 10}]}]}}), encoding='utf-8')
        # Claude Code's unattended permission mode requires a non-root user.
        uid, gid = (os.getuid(), os.getgid()) if os.getuid() else (1000, 1000)
        if os.getuid() == 0:
            os.chown(workspace, uid, gid)
            os.chown(workspace / 'start.md', uid, gid)
        command = [
            'docker', 'run', '--rm', '--init', '--name', container,
            '--user', f'{uid}:{gid}',
            '--pull', 'never', '--network', 'none', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges',
            '--volume', f'{workspace}:/workspace:rw', '--workdir', '/workspace',
        ]
        result.update(stage='generation', generation_status='running', generation_started_at=now())
        atomic_json(state_path, result)
        with ExitStack() as resources, \
                open(result['stderr_path'], 'w', encoding='utf-8') as stderr, \
                capture_trajectory(result['trajectory_path'], prompt=prompt) as (trajectory, stdout):
            channel_dir, model_env = resources.enter_context(model_channel(
                model_env, host_base_url=options.get('host_base_url'),
                max_output_tokens=options.get('max_output_tokens'),
                task_id=task_id,
                timeout_seconds=options.get('model_timeout_seconds', 3600)))
            package_dir, package_env = resources.enter_context(dependency_channel(
                offline, task_dir / 'dependencies.generation.jsonl'))
            model_env.update(package_env)
            relay = Path(__file__).with_name('container_relay.cjs').resolve()
            package_relay = Path(__file__).with_name('package_relay.py').resolve()
            command.extend(['--volume', f'{channel_dir}:/run/nl2repo-model:ro',
                            '--volume', f'{relay}:/opt/nl2repo-relay.cjs:ro',
                            '--volume', f'{package_dir}:/run/nl2repo-packages:ro',
                            '--volume', f'{package_relay}:/opt/nl2repo-package-relay.py:ro',
                            '--volume', f'{integrity_dir}:/opt/nl2repo-integrity:ro',
                            '--volume', f'{integrity_dir}/managed-settings.json:/etc/claude-code/managed-settings.json:ro',
                            '--entrypoint', 'python'])
            for name in model_env:
                command.extend(['--env', name])
            command.extend([
                offline['generation_image'], '-I', '/opt/nl2repo-package-relay.py',
                'node', '/opt/nl2repo-relay.cjs',
                '-p', prompt, '--model', pro['moduleName'],
                '--dangerously-skip-permissions',
                '--tools', 'Bash,Read,Write,Edit,Glob,Grep',
                '--max-turns', str(options.get('max_turns', 100)),
                '--output-format', 'stream-json', '--verbose',
                '--include-partial-messages',
            ])
            try:
                timeout = options.get('timeout_seconds', 7200)
                if generation_deadline is not None:
                    timeout = min(timeout, generation_deadline - monotonic())
                result['generation_timeout_seconds'] = max(0, timeout)
                atomic_json(state_path, result)
                if timeout <= 0:
                    raise subprocess.TimeoutExpired(command, 0)
                completed = run_generation(
                    command, env={**os.environ, **model_env}, stdin=subprocess.DEVNULL,
                    stdout=stdout, stderr=stderr,
                    timeout=timeout, cancel=cancel,
                )
                result['exit_code'] = completed.returncode
                if completed.returncode != 0:
                    trajectory.generation_status = 'failed'
            except subprocess.TimeoutExpired:
                result['generation_status'] = 'timeout'
                trajectory.generation_status = 'timeout'
                raise
            except TaskInterrupted:
                trajectory.generation_status = 'interrupted'
                raise
            except Exception:
                trajectory.generation_status = 'failed'
                raise
            finally:
                stdout.flush()
                # A killed Docker client does not necessarily stop its container.
                subprocess.run(['docker', 'rm', '-f', container],
                               capture_output=True, timeout=30, check=False)
        final_event = trajectory.final_event
        generation_error = final_result_error(final_event)
        if completed.returncode != 0:
            generation_error = f'CLI exited with code {completed.returncode}'
        ok = generation_error is None
        result['generation_status'] = 'success' if ok else 'failed'
        if generation_error:
            result['generation_error'] = generation_error
        result['agent_result'] = final_event
        if not ok:
            api_error = ((final_event or {}).get('api_error_status') is not None
                         or (final_event or {}).get('terminal_reason') == 'api_error')
            result.update(failure_kind='upstream_api_error' if api_error else 'generation_error',
                          failure_stage='generation')
        if cancel is not None and cancel.is_set():
            raise TaskInterrupted('Experiment interrupted before grading')
        result['stage'] = 'evaluation'
        atomic_json(state_path, result)
        # Score partial output too, but preserve the generation failure separately.
        try:
            post = evaluate_with_retries(
                task_id, workspace, data, options=options, result=result,
                state_path=state_path, cancel=cancel, grading_image=offline['grading_image'],
                dependency_policy=offline,
                install_timeout_seconds=options.get('install_timeout_seconds', 600),
                grading_timeout_seconds=options.get('grading_timeout_seconds', 1800))
        finally:
            result['evaluation_finished_at'] = now()
        result['post_process_result'] = post
        result['test_score'] = post.get('pytest_results', {}).get('passed', 0)
        result['score'] = result['test_score']
        result['score_valid'] = bool(ok and post.get('score_valid', False))
        result['artifact_install_status'] = post.get('artifact_install', {}).get('status', 'not_checked')
        result['evaluation_valid'] = bool(ok and post.get('status') == 'success'
            and post.get('evaluation_valid', post.get('score_valid', False)
                         and result['artifact_install_status'] == 'success'))
        result['candidate_failures'] = post.get('candidate_failures', [])
        result['coverage_limited'] = post.get('pytest_results', {}).get('coverage_limited', False)
        if (post.get('status') != 'success' or not post.get('evaluation_valid',
                post.get('score_valid', False) and result['artifact_install_status'] == 'success')):
            evaluation_stage = post.get('failure_stage') or (
                'artifact_install' if result['artifact_install_status'] != 'success' else 'grading_tests')
            result['evaluation_failures'] = post.get('failures') or [{
                'kind': post.get('failure_kind') or 'evaluation_error', 'stage': evaluation_stage}]
            if ok:
                result.update(failure_kind=result['evaluation_failures'][0]['kind'],
                              failure_stage=evaluation_stage)
        result['status'] = ('completed' if result['evaluation_valid'] and post['status'] == 'success'
                            else 'failed')
        if cancel is not None and cancel.is_set():
            raise TaskInterrupted('Experiment interrupted during grading')
    except TaskInterrupted as exc:
        result.update(status='interrupted', failure_kind='interrupted', failure_stage=result.get('stage'),
                      error=str(exc), score=None, test_score=None, score_valid=False, evaluation_valid=False)
        if result.get('generation_status') in (None, 'queued', 'running'):
            result['generation_status'] = 'interrupted'
    except Exception as exc:
        result['status'] = 'error'
        stage = result.get('stage')
        kind = ('environment_error' if stage == 'preparing'
                else 'generation_timeout' if result.get('generation_status') == 'timeout'
                else 'generation_error' if stage == 'generation' else 'evaluation_error')
        if stage == 'evaluation':
            result.setdefault('evaluation_failures', []).append({
                'kind': kind, 'stage': stage, 'error': str(exc)})
        result.setdefault('failure_kind', kind)
        result.setdefault('failure_stage', stage)
        if result.get('generation_status') == 'running':
            result['generation_status'] = 'failed'
        result['error'] = str(exc)
        result['transient_execution_error'] = transient_exception(exc)
        logger.error('Task %s failed: %s', task_id, exc)
    result['finished_at'] = now()
    atomic_json(state_path, result)
    document = json.loads(Path(result['trajectory_path']).read_text())
    document['task_status'] = result['status']
    if document.get('generation_status') in ('queued', 'running', 'no_final_result'):
        document['generation_status'] = result.get('generation_status', result['status'])
    atomic_json(result['trajectory_path'], document)
    return result


def start_claude_code(config):
    from test_data_service import test_data_list
    options = dict(config.get('claude_code', {}))
    for key in ('experiment_name', 'output_dir'):
        if key in config:
            options[key] = config[key]
    retry_options(options)
    experiment_dir = experiment_directory(options)
    workers = config.get('max_pool_size', 1)
    for name, value in [('max_pool_size', workers),
                        ('max_turns', options.get('max_turns', 100)),
                        ('timeout_seconds', options.get('timeout_seconds', 7200)),
                        ('install_timeout_seconds', options.get('install_timeout_seconds', 600)),
                        ('grading_timeout_seconds', options.get('grading_timeout_seconds', 1800))]:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
    data_by_name = {data.proName: data for data in test_data_list}
    jobs = []
    for pro in config['startPro']:
        model_environment(pro, options)
        names = pro['proNameList']
        if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
            raise ValueError('proNameList must be a non-empty list of task names or ["*"]')
        if names == ['*']:
            names = sorted(data_by_name)
        elif '*' in names:
            raise ValueError('Use ["*"] alone to select all tasks')
        for name in dict.fromkeys(names):
            if name not in data_by_name:
                raise ValueError(f'Unknown task: {name}')
            jobs.append((pro, data_by_name[name]))
    if not jobs:
        raise ValueError('No tasks configured')
    cancel = threading.Event()
    previous = {}
    if threading.current_thread() is threading.main_thread():
        previous = {sig: signal.signal(sig, lambda *_: cancel.set())
                    for sig in (signal.SIGTERM, signal.SIGINT)}
    planned = [(pro, data, task_identity(data)) for pro, data in jobs]
    try:
        for pro, data, task_id in planned:
            initial_result(pro, data, options, task_id)
        logger.info('Running %d tasks; experiment=%s; output=%s',
                    len(jobs), options.get('experiment_name', '(unnamed)'), experiment_dir)
        # Preparation belongs to run_task, so queued tasks do not pull images
        # and ready tasks can generate while other workers prepare their images.
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(run_task, pro, data, options, task_id=task_id, cancel=cancel)
                       for pro, data, task_id in planned]
            return [future.result() for future in concurrent.futures.as_completed(futures)]
    finally:
        try:
            finalize_unfinished(experiment_dir, status='interrupted' if cancel.is_set() else 'error')
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
