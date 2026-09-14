"""Persistent task states and cancellation, also usable by the supervisor."""

import json
from pathlib import Path
import subprocess
import time

from claude_code.trajectory import atomic_json, now


class TaskInterrupted(Exception):
    pass


def run_generation(command, *, cancel=None, timeout, **kwargs):
    deadline = time.monotonic() + timeout
    with subprocess.Popen(command, **kwargs) as process:
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise TaskInterrupted('Experiment interrupted')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    code = process.wait(timeout=min(.2, remaining))
                    return subprocess.CompletedProcess(command, code)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def finalize_unfinished(directory, *, status='interrupted'):
    """Called only after workers exit, so completed results cannot be overwritten."""
    for path in (Path(directory) / 'workspaces').glob('*/task_state.json'):
        result = json.loads(path.read_text())
        if result.get('status') not in ('queued', 'running'):
            continue
        result.update(status=status, failure_kind=status, failure_stage=result.get('stage'),
                      score=None, test_score=None, score_valid=False, evaluation_valid=False,
                      finished_at=now())
        if result.get('generation_status') in (None, 'running', 'queued'):
            result['generation_status'] = status
        atomic_json(path, result)
        trajectory = Path(result['trajectory_path'])
        if trajectory.is_file():
            document = json.loads(trajectory.read_text())
            document['task_status'] = status
            if document.get('generation_status') in ('running', 'queued', 'no_final_result'):
                document['generation_status'] = status
            atomic_json(trajectory, document)
