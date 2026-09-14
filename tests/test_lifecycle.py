import json
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import threading
import unittest

from claude_code.lifecycle import run_generation, TaskInterrupted, finalize_unfinished
from claude_code.trajectory import capture_trajectory, atomic_json


class LifecycleTests(unittest.TestCase):
    def test_sigterm_cooperatively_finishes_running_and_queued_tasks(self):
        script = '''
import signal,sys
from types import SimpleNamespace
import test_data_service
from claude_code import runner
before = signal.getsignal(signal.SIGTERM)
test_data_service.test_data_list = [SimpleNamespace(proName=n) for n in ('a','b')]
runner.offline_environment = lambda *args: {}
def task(pro, data, options, *, task_id, cancel):
    print('ready', flush=True)
    assert cancel.wait(5)
    return {}
runner.run_task = task
runner.start_claude_code({'output_dir':sys.argv[1], 'max_pool_size':1,
    'startPro':[{'moduleName':'test','baseUrl':'http://mock.test','sk':'dummy','proNameList':['*']}]})
assert signal.getsignal(signal.SIGTERM) == before
'''
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen([sys.executable, '-B', '-c', script, directory],
                                       cwd=Path(__file__).resolve().parents[1],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(process.stdout.readline().strip(), 'ready')
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertFalse((Path(directory) / 'result').exists())
                results = list((Path(directory) / 'workspaces').glob('*/task_state.json'))
                self.assertEqual(len(results), 2)
                self.assertTrue(all(json.loads(p.read_text())['status'] == 'interrupted' for p in results))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()

    def test_local_child_timeout_and_cancellation_keep_partial_transcript(self):
        for cancel_task in (False, True):
            with self.subTest(cancel_task=cancel_task), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / 'trajectory.json'
                cancelled = threading.Event()
                timer = threading.Timer(.25, cancelled.set)
                command = [sys.executable, '-u', '-c',
                    'import json,time; print(json.dumps({"type":"assistant","message":{"content":"partial"}})); time.sleep(20)']
                with capture_trajectory(destination, interval=.01) as (reader, output):
                    if cancel_task:
                        timer.start()
                    try:
                        with self.assertRaises(TaskInterrupted if cancel_task else subprocess.TimeoutExpired):
                            run_generation(command, stdout=output, stderr=subprocess.DEVNULL,
                                           timeout=5 if cancel_task else .25, cancel=cancelled)
                        reader.generation_status = 'interrupted' if cancel_task else 'timeout'
                    finally:
                        timer.cancel()
                document = json.loads(destination.read_text())
                self.assertEqual(document['messages'][0]['content'][0]['text'], 'partial')
                self.assertEqual(document['generation_status'], 'interrupted' if cancel_task else 'timeout')
                self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_supervisor_fills_queued_and_running_but_preserves_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for status in ('queued', 'running', 'completed'):
                trajectory = root / 'workspaces' / status / 'trajectory.json'
                atomic_json(trajectory, {'generation_status': 'running', 'messages': [{'role': 'assistant', 'content': 'partial'}]})
                atomic_json(root / 'workspaces' / status / 'task_state.json', {
                    'status': status, 'stage': 'generation', 'trajectory_path': str(trajectory)})
            atomic_json(root / 'result/completed.json', {'status': 'completed'})
            published = (root / 'result/completed.json').read_bytes()
            original = (root / 'workspaces/completed/task_state.json').read_bytes()
            finalize_unfinished(root)
            self.assertEqual((root / 'result/completed.json').read_bytes(), published)
            self.assertEqual(list((root / 'result').iterdir()), [root / 'result/completed.json'])
            self.assertEqual((root / 'workspaces/completed/task_state.json').read_bytes(), original)
            for status in ('queued', 'running'):
                result = json.loads((root / 'workspaces' / status / 'task_state.json').read_text())
                self.assertEqual(result['status'], 'interrupted')
                self.assertFalse(result['evaluation_valid'])
                trace = json.loads((root / 'workspaces' / status / 'trajectory.json').read_text())
                self.assertEqual(trace['generation_status'], 'interrupted')
                self.assertEqual(trace['messages'][0]['content'], 'partial')
