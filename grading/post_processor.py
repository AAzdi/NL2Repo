import os
import json
import zipfile
import shutil
import subprocess
import re
import uuid
import tempfile
import time
import stat
import shlex
from contextlib import contextmanager
from datetime import datetime
from typing import List, Dict, Any
from test_data_service import TestData
from grading.config import artifact_install_commands
from grading.reference_repairs import REVISION
from docker_self.docker_service import (
    DockerHostInfo,
    create_docker_client,
    build_image,
    create_advanced_container,
    remove_container,
    execute_command_in_container,
    collect_container_diagnostics,
)


def log_to_both(original_logger, log_file_path: str, level: str, message: str):
    """
    Log a message to both the original logger and a log file.

    Args:
        original_logger: Logger object for logging
        log_file_path: Path to the log file
        level: Log level (info, warning, error, debug)
        message: Log message
    """
 
    if level == 'info':
        original_logger.info(message)
    elif level == 'warning':
        original_logger.warning(message)
    elif level == 'error':
        original_logger.error(message)
    elif level == 'debug':
        original_logger.debug(message)


    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_line = f"{timestamp} - {level.upper()} - {message}\n"

    try:
        with open(log_file_path, 'a', encoding='utf-8') as f:
            f.write(log_line)
    except Exception as e:
        # If writing to file fails, log the error via the original logger
        original_logger.error(f"Log to file failed: {str(e)}")


class DualLogger:
    """
    A logger that outputs to both the original logger and a log file.

    """

    def __init__(self, original_logger, log_file_path: str):
        self.original_logger = original_logger
        self.log_file_path = log_file_path

        # Make sure the directory exists
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

        # Initialize 
        try:
            with open(log_file_path, 'w', encoding='utf-8') as f:
                f.write(f"=== Post Process Log Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        except Exception as e:
            original_logger.error(f"Initialize log file failed: {str(e)}")

    def info(self, message: str):
        log_to_both(self.original_logger, self.log_file_path, 'info', message)

    def warning(self, message: str):
        log_to_both(self.original_logger, self.log_file_path, 'warning', message)

    def error(self, message: str):
        log_to_both(self.original_logger, self.log_file_path, 'error', message)

    def debug(self, message: str):
        log_to_both(self.original_logger, self.log_file_path, 'debug', message)


def create_workspace_zip(workspace_path: str, logger) -> str:
    """
    Create a zip file of the workspace folder.

    Args:
        workspace_path: workspace path
        logger: logger

    Returns:
        zip path
    """
    logger.info(f"Start creating zip file: {workspace_path}")

    zip_path = workspace_path + ".zip"

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(workspace_path):
                # Preserve links as links. Their targets may exist only inside
                # the generation container, and must never be read on the host.
                links = [name for name in dirs if os.path.islink(os.path.join(root, name))]
                dirs[:] = [name for name in dirs if name not in links]
                for file in files + links:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, workspace_path)
                    if os.path.islink(file_path):
                        info = zipfile.ZipInfo(arcname)
                        info.create_system = 3
                        info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        zipf.writestr(info, os.readlink(file_path))
                    else:
                        zipf.write(file_path, arcname)

        logger.info(f"Successfully created zip file: {zip_path}")
        return zip_path

    except Exception as e:
        logger.error(f"Failed to create zip file: {str(e)}")
        raise


def remove_package_files(workspace_path: str, logger):
    """
    Delete package management files in the workspace

    Args:
        workspace_path: workspace path
        logger: logger
    """
    logger.info("Start removing package files")

    package_files = [
        "setup.py",
        "pyproject.toml",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
        "tox.ini",
        "pytest.ini",
        "poetry.lock",
        "Pipfile",
        "Pipfile.lock",
        "environment.yml",
        "conda-env.yaml",
        "manifest.in",
        "MANIFEST.in"
    ]

    removed_files = []

    for root, dirs, files in os.walk(workspace_path):
        for file in files:
            if file in package_files:
                file_path = os.path.join(root, file)
                try:
                    os.remove(file_path)
                    removed_files.append(file_path)
                    logger.info(f"删除包管理文件: {file_path}")
                except Exception as e:
                    logger.warning(f"删除文件失败 {file_path}: {str(e)}")

    logger.info(f"共删除了 {len(removed_files)} 个包管理文件")


def remove_test_files(workspace_path: str, test_files: List[str], logger):
    """
    根据测试文件列表删除workspace中的同名文件或文件夹

    Args:
        workspace_path: workspace路径
        test_files: 测试文件列表
        logger: 日志记录器
    """
    logger.info(f"开始删除测试文件，共 {len(test_files)} 个文件/文件夹")

    removed_items = []

    for test_file in test_files:
        target_path = os.path.join(workspace_path, test_file)

        try:
            if os.path.exists(target_path):
                if os.path.isdir(target_path):
                    shutil.rmtree(target_path)
                    logger.info(f"Deleting directory: {target_path}")
                else:
                    os.remove(target_path)
                    logger.info(f"Deleting file: {target_path}")
                removed_items.append(target_path)
            else:
                logger.warning(f"File or directory does not exist: {target_path}")

        except Exception as e:
            logger.error(f"Failed to delete {target_path}: {str(e)}")

    logger.info(f"Successfully deleted {len(removed_items)} files/directories")


@contextmanager
def pinned_base_image(image_id, host_info, *, allow_tag=False):
    """BuildKit cannot use a local image ID directly in FROM; tag that exact ID."""
    if image_id is None:
        yield None
        return
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
        if allow_tag and re.fullmatch(r'[a-z0-9][a-z0-9./_-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?', image_id):
            yield image_id
            return
        raise ValueError('Offline grading requires an immutable local image ID')
    client = create_docker_client(host_info)
    tag = f'nl2repo-offline-base:{uuid.uuid4().hex}'
    client.image.tag(image_id, tag)
    try:
        yield tag
    finally:
        client.image.remove(tag, prune=False)


def create_dockerfile(workspace_path: str, base_image_tag: str, logger, *, base_image=None,
                      artifact_install=False) -> str:
    """
    Create Dockerfile 

    Args:
        workspace_path: workspace path
        base_image_tag: base image tag
        logger: logger

    Returns:
        Dockerfile path
    """
    logger.info("Start creating Dockerfile")

    dockerfile_dir = os.path.dirname(workspace_path)
    dockerfile_path = os.path.join(dockerfile_dir, "Dockerfile")

    base_image = base_image or f"ghcr.io/multimodal-art-projection/nl2repobench/{base_image_tag}"
    ownership = ('USER root\nRUN mkdir -p /workspace && chown 1000:1000 /workspace\n'
                 'USER 1000:1000\n') if artifact_install else ''
    copy_options = '--chown=1000:1000 ' if artifact_install else ''
    preparation = ''
    if not artifact_install:
        shutil.copyfile(os.path.join(os.path.dirname(__file__), 'grading_prepare.py'),
                        os.path.join(dockerfile_dir, 'grading_prepare.py'))
        preparation = ('COPY grading_prepare.py /tmp/nl2repo-grading-prepare.py\n'
                       'RUN python -I -S /tmp/nl2repo-grading-prepare.py\n')
        project = base_image_tag.split(':')[0]
        if project in ('pylama', 'python-pytest-cases', 'box', 'tenacity',
                       'deslib', 'pathlib2', 'requests-html'):
            for helper in ('reference_repairs.py', 'http_fixture.py'):
                shutil.copyfile(os.path.join(os.path.dirname(__file__), helper),
                                os.path.join(dockerfile_dir, helper))
            preparation += ('COPY reference_repairs.py http_fixture.py /tmp/\n'
                            'RUN python -I -S /tmp/reference_repairs.py ' + project + '\n')
    dockerfile_content = f"""FROM --platform=linux/amd64 {base_image}

ENTRYPOINT []
{ownership}
{preparation}

# Copy workspace content to container
COPY {copy_options}workspace /workspace

# Set working directory
WORKDIR /workspace

# Set environment variables
ENV PYTHONPATH=/workspace:$PYTHONPATH

# Keep container running
CMD ["tail", "-f", "/dev/null"]
"""

    try:
        with open(dockerfile_path, 'w', encoding='utf-8') as f:
            f.write(dockerfile_content)

        logger.info(f"Successfully created Dockerfile: {dockerfile_path}")
        return dockerfile_path

    except Exception as e:
        logger.error(f"Failed to create Dockerfile: {str(e)}")
        raise


def build_test_image(dockerfile_path: str, image_tag: str, host_info: DockerHostInfo, logger,
                     *, timeout_seconds=600) -> str:
    """
    Construct test image

    Args:
        dockerfile_path: Dockerfile path
        image_tag: image tag
        host_info: Docker host info
        logger: logger

    Returns:
        image id
    """
    logger.info(f"Start building test image: {image_tag}")  

    try:
        build_context = os.path.dirname(dockerfile_path)

        image, build_logs = build_image(host_info, dockerfile_path, image_tag, build_context,
                                        timeout_seconds=timeout_seconds)

        # 记录构建日志
        for log_line in build_logs:
            logger.info(f"Build: {log_line}")

        logger.info(f"Successfully build image: {image_tag}, ID: {image.id}")
        return image.id

    except Exception as e:
        logger.error(f"Failed to build image: {str(e)}")
        raise


def run_test_commands(image_tag: str, container_name: str, test_commands: List[str], test_case_count: int,
                      host_info: DockerHostInfo, logger, *, isolated=False, dependency_access=None,
                      timeout_seconds=1800, setup_timeout_seconds=600,
                      candidate_setup=False, candidate_modules=()) -> Dict[str, Any]:
    """Run bounded setup/tests, retaining partial output and always removing the container."""
    for name, value in [('timeout_seconds', timeout_seconds),
                        ('setup_timeout_seconds', setup_timeout_seconds)]:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f'{name} must be positive')
    deadline = time.monotonic() + timeout_seconds
    command_results = []
    last_exit_code = None
    setup_status = 'success'
    execution_status = 'success'
    halted = False
    container = None
    result = {}
    diagnostics = {}
    try:
        isolation = dict(network_mode='none', cap_drop=['ALL'],
                         security_options=['no-new-privileges'],
                         env_vars={'PIP_NO_INDEX': '1', 'PIP_DISABLE_PIP_VERSION_CHECK': '1',
                                   'npm_config_offline': 'true'}) if isolated else {}
        command = ["tail", "-f", "/dev/null"]
        if dependency_access:
            if not isolated:
                raise ValueError('Dependency channel requires network isolation')
            directory, environment, relay = dependency_access
            isolation['env_vars'].update(environment)
            isolation['volumes'] = [(directory, '/run/nl2repo-packages', 'ro'),
                                    (relay, '/opt/nl2repo-package-relay.py', 'ro')]
            command = ['python', '-I', '/opt/nl2repo-package-relay.py', *command]
        container = create_advanced_container(
            host_info=host_info, image_name=image_tag, container_name=container_name,
            working_dir="/workspace", command=command, **isolation)
        for command in test_commands:
            if halted:
                command_results.append({'command': command, 'exit_code': None,
                                        'status': 'not_run', 'output': 'Earlier command failed or timed out'})
                continue
            pytest = is_pytest_command(command)
            remaining = deadline - time.monotonic()
            limit = min(remaining, setup_timeout_seconds) if not pytest else remaining
            status = 'completed'
            try:
                if limit <= 0:
                    raise subprocess.TimeoutExpired(command, timeout_seconds)
                last_exit_code, output = execute_command_in_container(
                    host_info=host_info, container_id=container_name, command=command,
                    workdir="/workspace", timeout_seconds=limit)
            except subprocess.TimeoutExpired as exc:
                output = exc.output or ''
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                last_exit_code = None
                status = execution_status = 'timeout'
                halted = True
            except Exception as exc:
                output = str(exc)
                last_exit_code = None
                status = execution_status = 'error'
                halted = True
            command_results.append({'command': command, 'exit_code': last_exit_code,
                                    'status': status, 'output': output})
            candidate_import = bool(pytest and status == 'completed' and last_exit_code == 4
                                    and candidate_conftest_error(output, candidate_modules))
            if candidate_import:
                command_results[-1]['candidate_import_error'] = True
                result['failure_kind'] = 'candidate_error'
            logger.info(f'Command {command}: status={status}, exit_code={last_exit_code}\n{output}')
            if not pytest and (status != 'completed' or last_exit_code != 0):
                setup_status = 'timeout' if status == 'timeout' else 'failed'
                if execution_status == 'success':
                    execution_status = 'failed'
                    # A completed installer rejecting submitted code is a candidate
                    # failure. Transport/tooling failures still invalidate evaluation.
                    local_install = bool(re.search(r"\bpip\s+install\s+(?:-e\s+)?\.(?:\s|$|['\";])", command))
                    if (candidate_setup or local_install) and last_exit_code in (1, 2) and not re.search(
                            r'ConnectionError|Connection refused|Connection reset|'
                            r'NewConnectionError|Max retries exceeded|Retrying \(|'
                            r'Temporary failure in name resolution|Network is unreachable|'
                            r'No space left on device|Cannot connect to the Docker daemon|'
                            r'Error response from daemon|command not found|'
                            r'ReadTimeout|Dependency upstream unavailable|'
                            r'HTTP (?:error )?(?:413|502|503|504)', output, re.I):
                        result['failure_kind'] = 'candidate_error'
                halted = True
            elif pytest and not candidate_import and (status != 'completed' or last_exit_code not in (0, 1, 2)):
                if execution_status == 'success':
                    execution_status = 'error'
                halted = True
        pytest_results = analyze_pytest_results(command_results, test_case_count, logger)
        if (setup_status == 'success' and execution_status == 'success'
                and pytest_results['score_valid'] and (pytest_results['failed'] or pytest_results['errors'])):
            result['failure_kind'] = 'candidate_error'
        result.update(command_results=command_results, last_exit_code=last_exit_code,
                      setup_status=setup_status, execution_status=execution_status,
                      pytest_results=pytest_results, container_id=container.id,
                      container_diagnostics=diagnostics)
        return result
    except Exception as exc:
        # The outer post-processor persists these even if container creation fails.
        exc.container_diagnostics = diagnostics
        exc.container_failure_kind = 'environment_error' if container is None else 'evaluation_error'
        raise
    finally:
        try:
            diagnostics.update(collect_container_diagnostics(host_info, container_name))
        except Exception as exc:
            diagnostics['collection_error'] = str(exc)
        logger.info('Container diagnostics before cleanup: ' + json.dumps(diagnostics, ensure_ascii=False))
        if result and diagnostics.get('state', {}).get('Running') is False:
            # The container's main process must remain alive throughout grading.
            # A Docker exec failure here is not an ordinary candidate test failure.
            result.update(failure_kind='environment_error', execution_status='error')
            result['pytest_results']['score_valid'] = False
        if result and diagnostics.get('state', {}).get('OOMKilled'):
            result.update(failure_kind='environment_error', failure_reason='oom_killed',
                          execution_status='error')
            result['pytest_results']['score_valid'] = False
        # Force removal also terminates descendants of a timed-out docker exec.
        # Try cleanup even if creation raised after Docker allocated the name.
        try:
            remove_container(host_info, container_name, force=True)
        except Exception as exc:
            logger.warning(f'Failed to remove test container {container_name}: {exc}')


def candidate_conftest_error(output, modules):
    if 'ImportError while loading conftest' not in output:
        return False
    for module in modules:
        name = re.escape(module)
        if re.search(r"(?:No module named |from |<class )['\"]" + name + r"(?:[.'\"])", output):
            return True
    return False


def is_pytest_command(command):
    """Recognize supported launchers without matching pip/python string arguments."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    # Reference commands are individual argv commands, not shell programs.
    while words:
        name = os.path.basename(words[0])
        if name == 'env':
            words = words[1:]
            while words and (words[0] == '--' or re.match(r'^[A-Za-z_]\w*=', words[0])):
                words = words[1:]
        elif name == 'xvfb-run':
            words = words[1:]
            while words and words[0].startswith('-'):
                flag = words.pop(0)
                if flag == '--':
                    break
                if flag in ('-e', '-f', '-n', '-p', '-s', '--error-file', '--auth-file',
                            '--server-num', '--xauth-protocol', '--server-args'):
                    words = words[1:]
        else:
            return name == 'pytest' or (bool(re.fullmatch(r'python[0-9.]*', name))
                                       and words[1:3] == ['-m', 'pytest'])
    return False


def pytest_progress_counts(output):
    """Recover observed outcomes from an unfinished outer pytest session.

    Require a session/collection header and pytest progress syntax. Never count
    dots in arbitrary output, failure details or nested pytest sessions. Verbose
    node IDs are deduplicated; compact output is accepted only without redraws.
    The caller must prefer a final summary whenever one exists.
    """
    counts = dict(passed=0, failed=0, errors=0, skipped=0, xfailed=0, xpassed=0)
    labels = dict(PASSED='passed', FAILED='failed', ERROR='errors',
                  SKIPPED='skipped', XFAIL='xfailed', XPASS='xpassed')
    symbols = {'.': 'passed', 'F': 'failed', 'E': 'errors', 's': 'skipped',
               'x': 'xfailed', 'X': 'xpassed'}
    text = re.sub(r'\x1b\[[0-9;]*m', '', output)
    session = False
    collected = None
    mode = None
    pending = None
    nodes = {}
    compact_file = False
    for raw in text.split('\n'):
        line = raw.strip()
        if re.fullmatch(r'=+ test session starts =+', line):
            if session:
                return None
            session = True
            continue
        if not session:
            continue
        if collected is None:
            match = re.search(r'(?:^|\r|collecting\s*\.\.\.\s*)collected (\d+) items?\b', line)
            if match:
                collected = int(match[1])
            continue
        if line.strip('= ').strip() in ('FAILURES', 'ERRORS', 'short test summary info'):
            break
        if line.startswith(('INTERNALERROR', 'KeyboardInterrupt', '!', 'Timeout (')):
            break
        if '\r' in raw:
            return None  # Redrawn compact progress cannot be deduplicated safely.
        verbose = re.match(r'^(\S+\.py::\S+)(?:\s+(.*))?$', line)
        if verbose:
            if mode == 'compact':
                return None
            mode = 'verbose'
            pending = verbose[1]
            tail = verbose[2] or ''
            status = re.fullmatch(r'(?:<- .*? )?(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)'
                                  r'(?:\s+\[\s*\d+%\])?', tail)
            if status:
                nodes[pending] = labels[status[1]]
                pending = None
            continue
        standalone = re.fullmatch(r'(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)'
                                  r'(?:\s+\[\s*\d+%\])?', line)
        if pending and standalone:
            nodes[pending] = labels[standalone[1]]
            pending = None
            continue
        compact = re.fullmatch(r'(\S+\.py)\s+([.FEsxX]+)(?:\s+\[\s*\d+%\])?', line)
        continuation = (re.fullmatch(r'([.FEsxX]+)\s+\[\s*\d+%\]', line)
                        if compact_file else None)
        if compact or continuation:
            if mode == 'verbose':
                return None
            mode = 'compact'
            compact_file = True
            for symbol in compact[2] if compact else continuation[1]:
                counts[symbols[symbol]] += 1
        elif line:
            compact_file = False
    for outcome in nodes.values():
        counts[outcome] += 1
    if collected is None or not sum(counts.values()) or sum(counts.values()) > collected:
        return None
    return counts


def analyze_pytest_results(command_results: List[Dict], total_test_cases: int, logger) -> Dict[str, Any]:
    """
    Analyze pytest command results

    Args:
        command_results: Command execution results list
        total_test_cases: Total test case count 
        logger: Logger instance

    Returns:
        pytest analysis results
    """
    logger.info("Start analyzing pytest results")

    pytest_results = {
        'passed': 0,
        'failed': 0,
        'errors': 0,
        'total': total_test_cases,
        'success_rate': 0.0
    }
    pytest_results.update(skipped=0, xfailed=0, xpassed=0, deselected=0,
                          collected=None, summary_complete=True, commands=0,
                          denominator_source='test_case_count.txt', invalid_reasons=[])

    for result in command_results:
        command = result['command']
        output = result['output']

        # 检查是否是pytest命令
        if is_pytest_command(command):
            pytest_results['commands'] += 1
            if result.get('candidate_import_error'):
                # A verified missing candidate API in conftest is an observed
                # candidate error, although pytest prints no session summary.
                pytest_results['errors'] += 1
                continue
            if result.get('status', 'completed') != 'completed' or result.get('exit_code') not in (0, 1, 2):
                pytest_results['invalid_reasons'].append('abnormal_exit')
            if result.get('status') == 'not_run':
                pytest_results['summary_complete'] = False
                continue
            logger.info(f"Analyzing pytest command: {command}")


            # Collection progress and assertion messages can repeat counts.
            # Read only the final pytest summary, including quiet (-q) output.
            summary = None
            collected = None
            in_details = False
            outer_error = False
            lines = output.splitlines()
            for line in lines:
                line = re.sub(r'\x1b\[[0-9;]*m', '', line).strip().strip('=').strip()
                if line in ('FAILURES', 'ERRORS') or line.startswith('short test summary info'):
                    in_details = True
                # Failure details can contain complete child pytest sessions.
                # Only the first outer collection line is authoritative.
                match = re.match(r'(?:collecting\s*\.\.\.\s*)?collected (\d+) items?\b', line)
                if match and collected is None and not in_details:
                    collected = int(match.group(1))
                if not in_details and re.match(r'^(?:INTERNALERROR\b|KeyboardInterrupt\b)', line):
                    outer_error = True
                if re.fullmatch(
                        r'\d+ (?:passed|failed|errors?|warnings?|skipped|deselected|xfailed|xpassed|rerun)'
                        r'(?:, \d+ (?:passed|failed|errors?|warnings?|skipped|deselected|xfailed|xpassed|rerun))*'
                        r'(?: in \d+(?:\.\d+)?s(?: \([^\n]*\))?)?', line):
                    summary = line
            # An interrupted outer session has no normal completed exit. Do not
            # treat an INTERNALERROR quoted by a nested pytester test as ours.
            if outer_error or (result.get('exit_code') not in (0, 1)
                               and re.search(r'(?m)^!+.*KeyboardInterrupt', output)):
                pytest_results['invalid_reasons'].append('pytest_execution_error')
            if summary is not None:
                counts = {}
                for key, label in [('passed', 'passed'), ('failed', 'failed'), ('errors', 'errors?'),
                                   ('skipped', 'skipped'), ('xfailed', 'xfailed'),
                                   ('xpassed', 'xpassed'), ('deselected', 'deselected')]:
                    match = re.search(rf'\b(\d+) {label}\b', summary)
                    if match:
                        counts[key] = int(match.group(1))
                        pytest_results[key] += counts[key]
                outcomes = sum(counts.get(key, 0) for key in
                               ('passed', 'failed', 'skipped', 'xfailed', 'xpassed'))
                if result.get('exit_code') == 2 and not counts.get('errors'):
                    pytest_results['invalid_reasons'].append('abnormal_exit')
                if not outcomes and not counts.get('errors'):
                    pytest_results['invalid_reasons'].append('no_test_outcomes')
                # Candidate collection/setup/teardown errors are valid outcomes;
                # their arithmetic differs from ordinary assertion outcomes.
                selected = collected - counts.get('deselected', 0) if collected is not None else None
                # Module-level collection skips add to skipped without adding
                # collected items. Allow that case, but never missing outcomes
                # or more non-skipped outcomes than collected tests.
                if selected is not None and not counts.get('errors') and not (
                        outcomes - counts.get('skipped', 0) <= selected <= outcomes):
                    pytest_results['invalid_reasons'].append('collection_mismatch')
            else:
                pytest_results['summary_complete'] = False
                progress = pytest_progress_counts(output)
                if progress:
                    for key, count in progress.items():
                        pytest_results[key] += count
                    pytest_results.setdefault('partial_progress', []).append(
                        dict(command=command, **progress))
            if collected is not None:
                pytest_results['collected'] = (pytest_results['collected'] or 0) + collected

    # 计算成功率（使用testData提供的总数）
    if pytest_results['total'] > 0:
        pytest_results['success_rate'] = min(pytest_results['passed'] / pytest_results['total'],1)
    pytest_results['summary_complete'] &= pytest_results['commands'] > 0
    pytest_results['invalid_reasons'] = sorted(set(pytest_results['invalid_reasons']))
    pytest_results['coverage_limited'] = bool(pytest_results.get('partial_progress')
        or pytest_results['errors'] or pytest_results['skipped']
        or pytest_results['deselected'] or 'collection_mismatch' in pytest_results['invalid_reasons'])
    pytest_results['score_valid'] = bool(pytest_results['summary_complete']
        and not pytest_results['invalid_reasons'] and total_test_cases > 0)

    logger.info(f"pytest analysis results: {pytest_results}")
    logger.info(f"Using total test case count from testData: {total_test_cases}")
    return pytest_results


def post_process_task(task_uuid: str, workspace_path: str, test_data: TestData, original_logger,
                      docker_host: str = "localhost", *, isolated=False, base_image=None,
                      dependency_access=None, allow_base_image_tag=False,
                      generation_image=None, install_commands=None, install_timeout_seconds=600,
                      grading_timeout_seconds=1800, candidate_modules=()) -> Dict[str, Any]:
    """
    Post-process a task after its completion

    Args:
        task_uuid: UUID of the task
        workspace_path: Path to the workspace directory
        test_data: TestData object containing test information
        original_logger: Logger object for logging
        docker_host: Hostname of the Docker daemon, default is "localhost"

    Returns:
        A dictionary containing the post-processing results
    """

    log_file_path = os.path.join(os.path.dirname(workspace_path), "log.log")
    logger = DualLogger(original_logger, log_file_path)

    logger.info(f"开始执行任务后处理流程，任务UUID: {task_uuid}")


    host_info = DockerHostInfo(hostname=docker_host)
    stage = 'archive'
    artifact_install = {'status': 'not_checked'}

    def remaining(deadline):
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired(stage, 0)
        return seconds

    try:
        commands = artifact_install_commands({} if install_commands is None else {
            'artifact_install_commands': install_commands})
        
        zip_path = create_workspace_zip(workspace_path, logger)

        
        image_info = {"full_tag": test_data.proName + ":1.0"}

        
        test_image_tag = f"python-test-{task_uuid}"
        # Validate submitted packaging without inheriting reference setup files.
        if generation_image is not None:
            stage = 'artifact_install'
            deadline = time.monotonic() + install_timeout_seconds
            with tempfile.TemporaryDirectory(prefix='nl2repo-install-') as staging:
                candidate = os.path.join(staging, 'workspace')
                shutil.copytree(workspace_path, candidate, symlinks=True)
                install_tag = f'python-install-{task_uuid}'
                with pinned_base_image(generation_image, host_info,
                                       allow_tag=allow_base_image_tag) as reference:
                    dockerfile_path = create_dockerfile(candidate, image_info['full_tag'], logger,
                                                        base_image=reference, artifact_install=True)
                    build_test_image(dockerfile_path, install_tag, host_info, logger,
                                     timeout_seconds=remaining(deadline))
                install = run_test_commands(install_tag, install_tag,
                    commands, 0, host_info, logger,
                    isolated=isolated, dependency_access=dependency_access,
                    timeout_seconds=remaining(deadline),
                    setup_timeout_seconds=install_timeout_seconds, candidate_setup=True)
                artifact_install = {'status': install['setup_status'], **install}

        # The benchmark uses fixed reference tests/packaging. Only modify a copy.
        stage = 'grading_build'
        deadline = time.monotonic() + grading_timeout_seconds
        with tempfile.TemporaryDirectory(prefix='nl2repo-grading-') as staging:
            candidate = os.path.join(staging, 'workspace')
            shutil.copytree(workspace_path, candidate, symlinks=True)
            remove_package_files(candidate, logger)
            remove_test_files(candidate, test_data.pyTestFileList, logger)
            with pinned_base_image(base_image, host_info, allow_tag=allow_base_image_tag) as reference:
                dockerfile_path = create_dockerfile(candidate, image_info['full_tag'], logger,
                                                    base_image=reference)
                image_id = build_test_image(dockerfile_path, test_image_tag, host_info, logger,
                                           timeout_seconds=remaining(deadline))

        container_name = f"python-test-{task_uuid}"
        stage = 'grading_tests'
        test_results = run_test_commands(test_image_tag, container_name, test_data.testShell, test_data.testCaseCount,
                                         host_info, logger, isolated=isolated, dependency_access=dependency_access,
                                         timeout_seconds=remaining(deadline),
                                         setup_timeout_seconds=install_timeout_seconds,
                                         candidate_modules=candidate_modules)

        logger.info("Post-Process task done")

        score_valid = (test_results.get('failure_kind') != 'environment_error'
                       and test_results['setup_status'] == 'success'
                       and test_results['pytest_results']['score_valid'])
        install_valid = (artifact_install['status'] in ('success', 'not_checked')
                         and artifact_install.get('failure_kind') != 'environment_error')
        failures = []
        candidate_failures = []
        for details, failure_stage in ((artifact_install, 'artifact_install'),
                                       (test_results, 'grading_setup' if test_results['setup_status'] != 'success'
                                        else 'grading_tests')):
            if details.get('failure_kind') == 'candidate_error':
                candidate_failures.append({'kind': 'candidate_error', 'stage': failure_stage})
        if not install_valid and artifact_install.get('failure_kind') != 'candidate_error':
            failures.append({'kind': artifact_install.get('failure_kind', 'evaluation_error'),
                             'stage': 'artifact_install'})
        if not score_valid and test_results.get('failure_kind') != 'candidate_error':
            failures.append({'kind': test_results.get('failure_kind', 'evaluation_error'),
                             'stage': 'grading_setup' if test_results['setup_status'] != 'success'
                                      else 'grading_tests'})
        evaluation_valid = not failures
        failure_stage = failures[0]['stage'] if failures else None
        return {
            'status': 'success' if evaluation_valid else 'error',
            'artifact_install': artifact_install,
            'score_valid': score_valid,
            'evaluation_valid': evaluation_valid,
            'failure_stage': failure_stage,
            'failure_kind': failures[0]['kind'] if failures else None,
            'failures': failures,
            'candidate_failures': candidate_failures,
            'reference_repairs_revision': REVISION,
            'task_uuid': task_uuid,
            'zip_path': zip_path,
            'log_path': log_file_path,
            'image_info': image_info,
            'grading_base_image': base_image,
            'test_image_tag': test_image_tag,
            'test_image_id': image_id,
            'test_results': test_results,
            'pytest_results': test_results['pytest_results']
        }

    except Exception as e:
        logger.error(f"Post-Process task failed: {str(e)}")
        timeout = isinstance(e, subprocess.TimeoutExpired)
        output = getattr(e, 'output', '') or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        if stage == 'artifact_install':
            artifact_install = {'status': 'timeout' if timeout else 'failed', 'output': output}
        diagnostics = getattr(e, 'container_diagnostics', {})
        if stage == 'grading_tests' and getattr(e, 'container_failure_kind', None) == 'environment_error':
            stage = 'grading_setup'
        failure_kind = ('environment_error' if diagnostics.get('state', {}).get('Running') is False
                        else getattr(e, 'container_failure_kind', 'evaluation_error'))
        failures = []
        if stage != 'artifact_install' and (
                artifact_install['status'] not in ('success', 'not_checked')
                or artifact_install.get('failure_kind') == 'environment_error') and (
                artifact_install.get('failure_kind') != 'candidate_error'):
            failures.append({'kind': artifact_install.get('failure_kind', 'evaluation_error'),
                             'stage': 'artifact_install'})
        failures.append({'kind': failure_kind, 'stage': stage})
        return {
            'status': 'error',
            'score_valid': False,
            'evaluation_valid': False,
            'failure_stage': failures[0]['stage'],
            'failure_kind': failures[0]['kind'],
            'failures': failures,
            'candidate_failures': ([{'kind': 'candidate_error', 'stage': 'artifact_install'}]
                                   if artifact_install.get('failure_kind') == 'candidate_error' else []),
            'container_diagnostics': diagnostics,
            'execution_status': 'timeout' if timeout else 'error',
            'artifact_install': artifact_install,
            'output': output,
            'task_uuid': task_uuid,
            'error': str(e),
            'log_path': log_file_path
        }
