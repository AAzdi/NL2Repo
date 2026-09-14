"""Fail closed until trusted, task-specific offline images have been reviewed."""

from functools import lru_cache
import json
import logging
import re
import subprocess
import threading

from grading.config import artifact_install_commands


_grading_pull_lock = threading.Lock()
_grading_image_locks = {}
logger = logging.getLogger(__name__)


def grading_runtime_info(image):
    """Pull registry graders once per process, then pin their exact local image ID."""
    with _grading_pull_lock:
        image_lock = _grading_image_locks.setdefault(image, threading.Lock())
    # Serialize only callers using the same image; unrelated tasks can prepare
    # concurrently within the runner's worker limit.
    with image_lock:
        return _grading_runtime_info(image)


@lru_cache(maxsize=512)
def _grading_runtime_info(image):
    registry = image.split('/')[0]
    remote = '/' in image and ('.' in registry or ':' in registry or registry == 'localhost')
    if remote:
        # Never fall back to a potentially overwritten local tag on pull failure.
        logger.info('Pulling grading image: %s (timeout: 600 seconds)', image)
        subprocess.run(['docker', 'pull', '--platform', 'linux/amd64', image],
                       capture_output=True, text=True, timeout=600, check=True)
        logger.info('Grading image pull complete: %s', image)
    return {**runtime_info(image), 'source': 'registry_pull' if remote else 'local_image'}


def runtime_info(image):
    """Resolve mutable tags before caching probes or starting model work."""
    resolved = subprocess.run(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                              capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', resolved):
        raise ValueError(f'Cannot resolve local image: {image}')
    return {'image': resolved, 'python': python_version(resolved)}


@lru_cache(maxsize=512)
def python_version(image_id):
    result = subprocess.run([
        'docker', 'run', '--rm', '--pull', 'never', '--network', 'none',
        '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--entrypoint', 'python', image_id, '-I', '-c',
        'import platform; print(platform.python_version())'],
        capture_output=True, text=True, timeout=30, check=True)
    version = result.stdout.strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError(f'Unexpected Python version in {image_id}: {version!r}')
    return version


def offline_environment(options, task_name):
    entry = options.get('offline_environments', {}).get(task_name)
    check_residue = options.get('check_image_residue', True)
    if not isinstance(check_residue, bool):
        raise ValueError('check_image_residue must be a boolean')
    check_python = options.get('check_python_version', True)
    if not isinstance(check_python, bool):
        raise ValueError('check_python_version must be a boolean')
    if not isinstance(entry, dict) or (check_residue and entry.get('reviewed') is not True):
        raise ValueError(f'Offline environment not prepared for {task_name}: supply a reviewed '
                         'offline_environments entry with clean base images; dependencies can be installed through the controlled index')
    artifact_install_commands(entry)
    for key in ('generation_image', 'grading_image'):
        reference = entry.get(key, '')
        if check_residue and not re.fullmatch(r'sha256:[0-9a-f]{64}', reference):
            raise ValueError(f'{task_name}.{key} must be a local immutable image ID (sha256:...)')
        if not check_residue and not re.fullmatch(r'[a-z0-9][a-z0-9./_-]*(?::[A-Za-z0-9._-]+)?(?:@sha256:[0-9a-f]{64})?', reference):
            raise ValueError(f'{task_name}.{key} must be a valid image reference')
    targets = entry.get('target_distributions')
    modules = entry.get('target_modules')
    if not isinstance(targets, list) or not targets or any(
            not isinstance(t, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', t) for t in targets):
        raise ValueError(f'{task_name}: declare target_distributions, including package aliases')
    if not isinstance(modules, list) or not modules or any(
            not isinstance(t, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', t) for t in modules):
        raise ValueError(f'{task_name}: declare top-level target_modules')
    expected = entry.get('python_version')
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'\d+\.\d+', expected)):
        raise ValueError(f'{task_name}.python_version must be a major.minor string')
    runtime = {'generation': runtime_info(entry['generation_image']),
               'grading': grading_runtime_info(entry['grading_image'])}
    versions = {role: '.'.join(info['python'].split('.')[:2]) for role, info in runtime.items()}
    if check_python and (versions['generation'] != versions['grading']
                         or (expected and versions['generation'] != expected)):
        raise ValueError(f'{task_name}: Python version mismatch: generation={runtime["generation"]["python"]}, '
                         f'grading={runtime["grading"]["python"]}, expected={expected or "matching major.minor"}')
    entry = {**entry, 'runtime': runtime,
             'python_version_check': 'passed' if check_python else 'skipped',
             'image_references': {role: entry[role + '_image'] for role in runtime},
             **{role + '_image': info['image'] for role, info in runtime.items()}}
    if check_residue:
        for role in ('generation', 'grading'):
            inspect_offline_image(entry[role + '_image'], tuple(targets), tuple(modules), role)
        return entry
    return {**entry, 'image_residue_check': 'skipped'}


@lru_cache(maxsize=512)
def inspect_offline_image(image, targets, modules, role):
    # Images are prepared by the operator, never built from model-provided setup scripts.
    result = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True,
                            text=True, check=True, timeout=30)
    config = json.loads(result.stdout)[0]['Config']
    if config.get('OnBuild') or config.get('Volumes'):
        raise ValueError('Offline images must not contain ONBUILD triggers or implicit volumes')
    script = '''import importlib.metadata as m, importlib.util as u, json, re, sys
targets, modules, role = json.loads(sys.argv[1])
norm = lambda s: re.sub(r"[-_.]+", "-", s).lower()
installed = {norm(d.metadata["Name"]) for d in m.distributions() if d.metadata["Name"]}
found = sorted(set(map(norm, targets)) & installed)
found += [name for name in modules if u.find_spec(name) is not None]
if found: raise SystemExit("Reference implementation present: " + ", ".join(found))
if role == "grading" and u.find_spec("pytest") is None: raise SystemExit("pytest missing from offline grader")
'''
    result = subprocess.run([
        'docker', 'run', '--rm', '--pull', 'never', '--network', 'none', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image,
        '-c', script, json.dumps([targets, modules, role]),
    ], capture_output=True, text=True, timeout=60, check=False)
    if result.returncode:
        raise ValueError(f'Offline {role} image validation failed: {result.stderr.strip()}')
