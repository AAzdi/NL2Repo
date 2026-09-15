import ast
import json
import logging
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

from grading.post_processor import (analyze_pytest_results, create_dockerfile,
                                    create_workspace_zip, is_pytest_command)
from grading.reference_repairs import repair


class GradingRepairTests(unittest.TestCase):
    def test_new_image_builds_apply_repairs_before_copying_candidate(self):
        for project in ('box', 'python-pytest-cases'):
            with self.subTest(project=project), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root/'workspace'
                workspace.mkdir()
                dockerfile = Path(create_dockerfile(str(workspace), project + ':1.0',
                                                    logging.getLogger()))
                text = dockerfile.read_text()
                step = 'RUN python -I -S /tmp/reference_repairs.py ' + project
                self.assertLess(text.index(step), text.index('COPY workspace /workspace'))
                self.assertTrue((root/'reference_repairs.py').is_file())

    def test_zip_never_dereferences_files_or_directory_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)/'workspace'
            root.mkdir()
            secret = Path(directory)/'outside'
            secret.write_text('must not archive outside contents')
            (root/'python').symlink_to('/missing/container/python')
            (root/'outside').symlink_to(secret)
            (root/'dir').symlink_to('/missing/directory', target_is_directory=True)
            path = create_workspace_zip(str(root), logging.getLogger())
            with zipfile.ZipFile(path) as archive:
                self.assertEqual(set(archive.namelist()), {'python', 'outside', 'dir'})
                self.assertEqual(archive.read('python'), b'/missing/container/python')
                self.assertEqual(archive.read('outside'), str(secret).encode())
                for info in archive.infolist():
                    self.assertTrue(stat.S_ISLNK(info.external_attr >> 16))

    def test_wrapped_pytest_and_non_pytest_commands(self):
        for command in ['pytest tests', 'python3 -m pytest -q',
                        'env AWS_DEFAULT_REGION=us-east-1 pytest tests',
                        'xvfb-run -a pytest tests',
                        'xvfb-run --server-args "-screen 0 800x600x24" python -m pytest']:
            self.assertTrue(is_pytest_command(command), command)
        for command in ['pip install pytest', 'echo pytest', 'python -c "pytest"']:
            self.assertFalse(is_pytest_command(command), command)

    def test_nested_pytester_output_does_not_invalidate_outer_results(self):
        output = '''collected 211 items
=================================== FAILURES ===================================
___________________________ test_child ___________________________
----------------------------- Captured stdout call -----------------------------
collected 10 items
INTERNALERROR> child process failed
1 error in 0.01s
=========================== short test summary info ============================
FAILED test_child
============ 44 failed, 140 passed, 27 skipped in 114.28s (0:01:54) ============
'''
        result = analyze_pytest_results([dict(command='pytest tests', output=output,
                                              exit_code=1, status='completed')], 184, logging.getLogger())
        self.assertEqual(result['collected'], 211)
        self.assertEqual(result['passed'], 140)
        self.assertTrue(result['score_valid'], result)

    def test_reference_packaging_is_repaired_without_touching_candidate_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'setup.py').write_text('import pkg_resources\n')
            repair(root, 'pylama')
            self.assertIn('setuptools==80.9.0', (root/'pyproject.toml').read_text())
            self.assertEqual((root/'setup.py').read_text(), 'import pkg_resources\n')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'pyproject.toml').write_text('[build-system]\nrequires=["setuptools"]\n')
            repair(root, 'tenacity', reference_version='8.2.3')
            self.assertIn('include = tenacity\n    tenacity.*', (root/'setup.cfg').read_text())
            before = (root/'setup.cfg').read_text()
            repair(root, 'tenacity', reference_version='9.0')
            self.assertEqual((root/'setup.cfg').read_text(), before)

    def test_reference_helper_repairs_are_idempotent_and_keep_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tests/util').mkdir(parents=True)
            p = root/'tests/util/test_aggregation.py'
            p.write_text('import pytest\nfrom deslib.util.aggregation import *\nassert np is not None\n')
            f = root/'tests/util/test_faiss.py'
            f.write_text('from deslib.tests.test_des_integration import load_dataset\n')
            (root/'tests/test_des_integration.py').write_text('def load_dataset(): pass\n')
            for _ in range(2): repair(root, 'deslib')
            self.assertEqual(p.read_text().count('import numpy as np'), 1)
            self.assertIn('assert np is not None', p.read_text())
            self.assertIn('from ..test_des_integration', f.read_text())
            p = root/'tests/os_helper.py'
            p.write_text('import os, stat\ndef cleanup():\n'
                         '    def inner():\n            from test.support import _force_run\n'
                         '            return _force_run\n')
            for _ in range(2): repair(root, 'pathlib2')
            self.assertNotIn('from test.support', p.read_text())
            ast.parse(p.read_text(), feature_version=(3, 7))

    def test_pytest_cases_keeps_legacy_build_api_and_reference_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            setup = 'import pkg_resources\npkg_resources.require("setuptools>=39.2")\n'
            (root/'setup.py').write_text(setup)
            (root/'pyproject.toml').write_text('[build-system]\nrequires=["setuptools>=39.2", "wheel"]\n')
            for _ in range(2):
                repair(root, 'python-pytest-cases')
            self.assertIn('setuptools==80.9.0', (root/'pyproject.toml').read_text())
            self.assertEqual((root/'setup.py').read_text(), setup)

    def test_box_uses_existing_pure_python_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'setup.py').write_text(
                'try:\n    from Cython.Build import cythonize\n'
                'except ImportError:\n    extra = None\n'
                'else:\n    extra = cythonize(["box/box.py"])\n')
            for _ in range(2):
                repair(root, 'box')
            namespace = {}
            exec((root/'setup.py').read_text(), namespace)
            self.assertIsNone(namespace['extra'])

    def test_offline_http_tests_keep_parameter_count_and_assertions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'tests').mkdir()
            p = root/'tests/test_internet.py'
            p.write_text("import pytest\nurls = ['https://one', 'https://two']\n"
                         "@pytest.mark.parametrize('url', urls)\n"
                         'def test_pagination(url):\n    assert next(r.html)\n'
                         'def test_async_run():\n    for url in urls:\n        assert url\n')
            repair(root, 'requests-html')
            repair(root, 'requests-html')
            self.assertIn('range(5)', p.read_text())
            self.assertIn('assert next(r.html)', p.read_text())
            self.assertIn('indirect=True', p.read_text())
            self.assertIn('fixture_server + path', p.read_text())
            self.assertIn("('127.0.0.1', 0)", (root/'tests/conftest.py').read_text())

    def test_reference_command_configuration_uses_argv_semantics(self):
        root = Path(__file__).resolve().parents[1]/'test_files'
        binary = json.loads((root/'binaryalert/test_commands.json').read_text())
        self.assertEqual(binary, ['env AWS_DEFAULT_REGION=us-east-1 pytest --continue-on-collection-errors tests'])
        parse = json.loads((root/'parse/test_commands.json').read_text())
        self.assertEqual(parse[:2], ['pip install -e .', 'pip install -r tests/requirements.txt'])
        autorccar = json.loads((root/'autorccar/test_commands.json').read_text())
        self.assertEqual(autorccar, ['pytest --continue-on-collection-errors test'])
        mechanicalsoup = json.loads((root/'mechanicalsoup/test_commands.json').read_text())
        self.assertEqual(mechanicalsoup, ['pytest --continue-on-collection-errors tests'])
        pytz = json.loads((root/'pytz/test_commands.json').read_text())
        self.assertEqual(len(pytz), 2)
        self.assertIn("find_spec('pytz')", pytz[0])
        self.assertIn("Path('/workspace') in Path(spec.origin).resolve().parents", pytz[0])
        self.assertEqual(pytz[1], 'env PYTHONPATH=/workspace/src:/workspace pytest '
                                 '--continue-on-collection-errors -v '
                                 'test_docs.py test_lazy.py test_tzinfo.py')
