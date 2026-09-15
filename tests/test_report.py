"""Report arithmetic and task coverage, without Docker or model calls."""

import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from claude_code.report import build_report, write_report
from claude_code.trajectory import atomic_json


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def plan(self, names, totals=None, model='model'):
        tasks = [dict(task_uuid=name, module_name=model, pro_name=name,
                      official_test_count=(totals or {}).get(name, 10)) for name in names]
        atomic_json(self.root / 'task-plan.json', {'tasks': tasks})
        return tasks

    def result(self, name, passed, total=10, status='completed', valid=True, model='model',
               publish=True, **extra):
        value = dict(task_uuid=name, module_name=model, pro_name=name, status=status,
                     score=passed, score_valid=valid, evaluation_valid=status == 'completed',
                     generation_status='success', evaluation_finished_at='2026-09-15T01:00:00+00:00',
                     post_process_result={'pytest_results': {'passed': passed, 'total': total}}, **extra)
        atomic_json(self.root / 'workspaces' / name / 'task_state.json', value)
        if publish:
            atomic_json(self.root / 'result' / f'{name}.json', value)
        return value

    def test_full_average_includes_failed_positive_zero_missing_and_ungraded(self):
        self.plan(['small', 'large', 'api', 'timeout', 'zero', 'ungraded', 'missing'],
                  {'large': 1000})
        self.result('small', 5)
        self.result('large', 100, total=1000)
        self.result('api', 4, status='failed', valid=False, failure_kind='upstream_api_error')
        self.result('timeout', 3, status='failed', valid=False, failure_kind='evaluation_error')
        self.result('zero', 0)
        atomic_json(self.root / 'workspaces/ungraded/task_state.json', dict(
            task_uuid='ungraded', pro_name='ungraded', module_name='model', score=0,
            status='error', generation_status='timeout', score_valid=False))
        # Neither duplicate final records nor earlier retry scores may inflate the result.
        atomic_json(self.root / 'workspaces/api/evaluation-attempts/1/result.json', {'score': 10})
        report = write_report(self.root)
        self.assertEqual(report['metrics']['task_count'], 7)
        self.assertAlmostEqual(report['metrics']['full_task_average_score'], (0.5 + 0.1 + 0.4 + 0.3) / 7)
        self.assertEqual(report['metrics']['scored_task_count'], 5)
        self.assertEqual(report['metrics']['evaluated_task_count'], 5)
        self.assertEqual(report['metrics']['passed'], 112)
        rows = {row['pro_name']: row for row in report['tasks']}
        self.assertTrue(rows['api']['score_valid'])
        self.assertFalse(rows['api']['original_score_valid'])
        self.assertFalse(rows['api']['evaluation_valid'])
        self.assertEqual(rows['api']['status'], 'failed')
        self.assertEqual(rows['missing']['score_rate'], 0)
        self.assertEqual(rows['missing']['status'], 'missing')
        self.assertEqual(report['issues'], [])
        with (self.root / 'report.csv').open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 7)
        self.assertEqual(json.loads((self.root / 'report.json').read_text()), report)
        self.assertIn('全量任务平均分：18.5714%', (self.root / 'report.md').read_text())
        self.assertFalse(json.loads((self.root / 'result/api.json').read_text())['score_valid'])

    def test_plan_excludes_previous_runs_and_caps_each_task_at_one(self):
        self.plan(['current'])
        self.result('current', 12)
        self.result('old', 100)
        report = build_report(self.root)
        self.assertEqual(report['metrics']['task_count'], 1)
        self.assertEqual(report['metrics']['full_task_average_score'], 1)
        self.assertEqual(report['metrics']['full_score_task_count'], 1)
        self.assertTrue(report['issues'])

    def test_backfill_config_includes_missing_tasks_and_uses_historical_counts(self):
        atomic_json(self.root / 'config.json', {'startPro': [dict(
            moduleName='model', proNameList=['graded', 'ungraded', 'missing', 'graded'],
            sk='do-not-publish', baseUrl='http://private-host')]})
        (self.root / 'benchmark.log').write_text(
            'Project graded has 10 test cases (from file: test_case_count.txt)\n'
            'Project ungraded has 20 test cases (from file: test_case_count.txt)\n'
            'Project missing has 30 test cases (from file: test_case_count.txt)\n')
        self.result('graded', 5, valid=False, status='failed')
        atomic_json(self.root / 'workspaces/ungraded/task_state.json', dict(
            task_uuid='ungraded', pro_name='ungraded', module_name='model',
            score=0, score_valid=False, status='error'))
        report = write_report(self.root)
        self.assertEqual(report['metrics']['task_count'], 3)
        self.assertAlmostEqual(report['metrics']['full_task_average_score'], 0.5 / 3)
        self.assertEqual([row['official_total'] for row in report['tasks']], [10, 30, 20])
        for name in ('report.json', 'report.csv', 'report.md'):
            self.assertNotIn('do-not-publish', (self.root / name).read_text())
            self.assertNotIn('private-host', (self.root / name).read_text())

    def test_multiple_models_count_each_configured_job(self):
        tasks = self.plan(['first', 'second', 'missing'])
        tasks[0].update(pro_name='same', module_name='a')
        tasks[1].update(pro_name='same', module_name='b')
        tasks[2].update(module_name='b')
        atomic_json(self.root / 'task-plan.json', {'tasks': tasks})
        self.result('first', 5, model='a')
        self.result('second', 8, model='b')
        report = build_report(self.root)
        self.assertAlmostEqual(report['metrics']['full_task_average_score'], 1.3 / 3)
        self.assertEqual([(m['module_name'], m['task_count'], m['full_task_average_score'])
                          for m in report['models']], [('a', 1, 0.5), ('b', 2, 0.4)])

    def test_positive_score_without_denominator_is_not_silently_zeroed(self):
        self.plan(['task'], {'task': None})
        self.result('task', 3, total=None)
        report = write_report(self.root)
        self.assertIsNone(report['metrics']['full_task_average_score'])
        self.assertTrue(report['issues'])
        self.assertIn('无法计算', (self.root / 'report.md').read_text())

    def test_legacy_recorded_rate_is_used_when_denominator_is_unavailable(self):
        self.plan(['task'], {'task': None})
        result = self.result('task', 3, total=None)
        result['post_process_result']['pytest_results']['success_rate'] = 0.3
        atomic_json(self.root / 'result/task.json', result)
        report = build_report(self.root)
        self.assertEqual(report['metrics']['full_task_average_score'], 0.3)
        self.assertTrue(report['issues'])

    def test_corrupt_published_result_falls_back_to_state(self):
        self.plan(['task'])
        self.result('task', 3)
        (self.root / 'result/task.json').write_text('{')
        report = build_report(self.root)
        self.assertEqual(report['metrics']['full_task_average_score'], 0.3)
        self.assertEqual(report['tasks'][0]['source_path'], 'workspaces/task/task_state.json')
        self.assertTrue(report['issues'])

    def test_startup_failure_reports_all_configured_tasks_as_zero(self):
        config = {'startPro': [dict(moduleName='model', proNameList=['a', 'b'])]}
        report = write_report(self.root, config=config, run_state={
            'status': 'failed', 'exit_code': 1, 'task_count': 2})
        self.assertEqual(report['metrics']['full_task_average_score'], 0)
        self.assertEqual(report['metrics']['status_counts'], {'missing': 2})
        self.assertEqual(report['run']['exit_code'], 1)

    def test_wildcard_backfill_recovers_tasks_not_present_in_results(self):
        atomic_json(self.root / 'config.json', {'startPro': [dict(moduleName='model', proNameList=['*'])]})
        (self.root / 'benchmark.log').write_text(
            'Project graded has 10 test cases (from file: test_case_count.txt)\n'
            'Project missing has 30 test cases (from file: test_case_count.txt)\n')
        self.result('graded', 5)
        report = build_report(self.root)
        self.assertEqual(report['metrics']['task_count'], 2)
        self.assertEqual(report['metrics']['full_task_average_score'], 0.25)

    def test_command_line_backfill(self):
        self.plan(['task'])
        self.result('task', 5)
        result = subprocess.run([sys.executable, '-m', 'claude_code.report', str(self.root)],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=True)
        self.assertIn('全量任务平均分：50.0000%', result.stdout)
        self.assertIn(str(self.root / 'report.md'), result.stdout)

    def test_unknown_or_mismatched_scope_does_not_present_subset_as_full_average(self):
        self.result('only-discovered', 5)
        report = build_report(self.root)
        self.assertFalse(report['task_scope_complete'])
        self.assertIsNone(report['metrics']['full_task_average_score'])
        self.plan(['only-discovered'])
        report = build_report(self.root, run_state={'task_count': 2})
        self.assertFalse(report['task_scope_complete'])
        self.assertIsNone(report['metrics']['full_task_average_score'])
        self.assertTrue(report['issues'])


if __name__ == '__main__':
    unittest.main()
