import logging
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

from grading.post_processor import analyze_pytest_results, pytest_progress_counts


class PytestResultsTests(unittest.TestCase):
    HEADER = '================ test session starts ================\ncollected 20 items\n'

    def test_timeout_recovers_compact_progress_without_claiming_completion(self):
        result = analyze_pytest_results([dict(command='pytest tests', status='timeout',
            exit_code=None, output=self.HEADER + 'tests/test_one.py ..F [ 15%]\n'
            '..Fs [ 35%]\ntests/test_two.py .F')], 20, logging.getLogger())
        self.assertEqual((result['passed'], result['failed'], result['skipped']), (5, 3, 1))
        self.assertFalse(result['summary_complete'])
        self.assertFalse(result['score_valid'])  # Runner separately credits any positive count.
        self.assertTrue(result['coverage_limited'])
        self.assertIn('abnormal_exit', result['invalid_reasons'])

    def test_verbose_progress_handles_stdout_and_deduplicates_nodeids(self):
        output = self.HEADER + ('tests/test_one.py::test_a PASSED\n'
            'tests/test_one.py::test_a PASSED\n'
            'tests/test_one.py::test_b[param] application output\nmore output\n'
            '\x1b[32mPASSED\x1b[0m\n'
            'tests/test_one.py::test_c FAILED [ 15%]\n'
            'tests/test_one.py::test_hanging ')
        counts = pytest_progress_counts(output)
        self.assertEqual((counts['passed'], counts['failed']), (2, 1))

    def test_summary_overrides_progress_instead_of_double_counting(self):
        output = self.HEADER.replace('20 items', '2 items') + (
            'tests/test_one.py .. [100%]\n2 passed in 0.1s')
        result = self.analyze([('pytest tests', output)])
        self.assertEqual(result['passed'], 2)
        self.assertNotIn('partial_progress', result)

    def test_progress_ignores_details_and_rejects_ambiguous_output(self):
        output = self.HEADER + ('tests/test_one.py .F [ 10%]\n'
            '================ FAILURES ================\n'
            'tests/test_child.py ................\n')
        self.assertEqual(pytest_progress_counts(output)['passed'], 1)
        for output in ('tests/test_one.py ...\n', self.HEADER + 'arbitrary ... output\n',
                       self.HEADER + 'tests/test_one.py .\rtests/test_one.py ..\n',
                       self.HEADER + 'tests/test_one.py ..\n' + self.HEADER,
                       self.HEADER.replace('20 items', '1 item') + 'tests/test_one.py ..\n'):
            self.assertIsNone(pytest_progress_counts(output), output)

    def test_verbose_collection_prefix_and_carriage_returns(self):
        for prefix in ('collecting ... collected', 'collecting ...\rcollected',
                       '\x1b[1mcollecting ... \x1b[0mcollected'):
            with self.subTest(prefix=prefix):
                result = self.analyze([('pytest tests',
                    prefix + ' 18 items / 1 error\n18 passed, 1 error in 1.89s')])
                self.assertEqual(result['collected'], 18)
                self.assertEqual(result['errors'], 1)
                self.assertTrue(result['score_valid'])
                self.assertTrue(result['coverage_limited'])
                missing = self.analyze([('pytest tests',
                    prefix + ' 18 items\n2 passed in 0.1s')])
                self.assertIn('collection_mismatch', missing['invalid_reasons'])

    def test_assertion_text_does_not_override_collection_count(self):
        result = self.analyze([('pytest tests',
            'collecting ... collected 2 items\n'
            'E assert "collected 900 items"\n1 passed, 1 failed in 0.1s')])
        self.assertEqual(result['collected'], 2)
        self.assertTrue(result['score_valid'])

    def analyze(self, outputs):
        return analyze_pytest_results(
            [{'command': command, 'output': output, 'exit_code': 0} for command, output in outputs],
            423, logging.getLogger(__name__))

    def test_collection_errors_are_not_counted_twice(self):
        result = self.analyze([('pytest --continue-on-collection-errors tests',
            'collected 101 items / 24 errors\n'
            '============= 100 failed, 1 passed, 1 warning, 24 errors in 0.21s ==============')])
        self.assertEqual(result['passed'], 1)
        self.assertEqual(result['failed'], 100)
        self.assertEqual(result['errors'], 24)
        self.assertEqual(result['success_rate'], 1 / 423)

    def test_quiet_ansi_summary_and_multiple_commands(self):
        result = self.analyze([
            ('pip install pytest', '100 passed in 1.0s'),
            ('pytest -q unit', 'assert "900 passed"\n\x1b[32m2 passed, 1 skipped in 1.2s\x1b[0m'),
            ('pytest other', 'collected 0 items / 1 error\n=== 1 error in 0.01s ===')])
        self.assertEqual((result['passed'], result['failed'], result['errors']), (2, 0, 1))

    def test_incomplete_output_does_not_invent_final_counts(self):
        result = self.analyze([('pytest', 'collected 3 items / 1 error\nassert "400 passed"')])
        self.assertEqual((result['passed'], result['failed'], result['errors']), (0, 0, 0))

    def test_abnormal_exit_never_produces_valid_score(self):
        for code in (2, 3, 4, 5, -1, None, 137):
            with self.subTest(code=code):
                result = analyze_pytest_results([{'command': 'pytest', 'exit_code': code,
                    'output': 'collected 2 items\n2 passed in 0.1s'}], 2, logging.getLogger())
                self.assertFalse(result['score_valid'])
                self.assertEqual(result['passed'], 2)  # Retain partial counts for diagnosis.

    def test_normal_assertion_failures_allow_partial_score(self):
        result = analyze_pytest_results([{'command': 'pytest', 'exit_code': 1,
            'output': 'collected 3 items\n1 failed, 2 passed in 0.1s'}], 3, logging.getLogger())
        self.assertTrue(result['score_valid'])
        self.assertEqual(result['success_rate'], 2 / 3)

    @unittest.skipUnless(importlib.util.find_spec('pytest'), 'requires pytest for subprocess integration')
    def test_real_missing_candidate_import_is_a_valid_zero_or_partial_score(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'candidate.py').write_text('def existing(): pass\n')
            (root / 'test_missing.py').write_text('from candidate import missing\n')
            (root / 'test_good.py').write_text('def test_good(): assert True\n')
            for flags, passed, exit_code in [([], 0, 2), (['--continue-on-collection-errors'], 1, 1)]:
                with self.subTest(flags=flags):
                    run = subprocess.run([sys.executable, '-m', 'pytest', *flags], cwd=root,
                        env={**os.environ, 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1', 'PYTEST_ADDOPTS': ''},
                        capture_output=True, text=True, timeout=15)
                    self.assertEqual(run.returncode, exit_code, run.stdout + run.stderr)
                    result = analyze_pytest_results([{'command': 'python -m pytest',
                        'exit_code': run.returncode, 'output': run.stdout + run.stderr}], 2, logging.getLogger())
                    self.assertTrue(result['score_valid'], result)
                    self.assertEqual(result['passed'], passed)
                    self.assertEqual(result['errors'], 1)
                    self.assertEqual(result['success_rate'], passed / 2)

    def test_internal_error_or_keyboard_interrupt_invalidates_error_summary(self):
        for output in ('INTERNALERROR framework crashed\n1 error in 0.1s',
                       'KeyboardInterrupt\n1 error in 0.1s'):
            result = analyze_pytest_results([{'command': 'pytest', 'exit_code': 2,
                'output': output}], 2, logging.getLogger())
            self.assertFalse(result['score_valid'])

    def test_missing_outcomes_and_warning_only_summary_are_invalid(self):
        for output in ('collected 92 items\n2 passed in 0.1s', '1 warning in 0.1s'):
            result = self.analyze([('pytest', output)])
            self.assertFalse(result['score_valid'])
        result = self.analyze([('pytest', 'collected 92 items\n2 passed, 90 skipped in 0.1s')])
        self.assertTrue(result['score_valid'])
        self.assertTrue(result['coverage_limited'])

    def test_collection_skips_and_deselection_are_accounted_for(self):
        for output in ('collected 2 items / 1 skipped\n2 passed, 1 skipped in 0.1s',
                       'collected 3 items / 1 deselected / 2 selected\n2 passed, 1 deselected in 0.1s'):
            self.assertTrue(self.analyze([('pytest', output)])['score_valid'])

    def test_each_command_must_be_valid_and_not_run_output_is_ignored(self):
        result = analyze_pytest_results([
            {'command': 'pytest first', 'exit_code': 0, 'output': '2 passed in 0.1s'},
            {'command': 'pytest second', 'exit_code': None, 'status': 'not_run',
             'output': '100 passed in 0.1s'}], 102, logging.getLogger())
        self.assertFalse(result['score_valid'])
        self.assertEqual(result['passed'], 2)


if __name__ == '__main__':
    unittest.main()
