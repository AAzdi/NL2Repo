"""Versioned repairs to trusted reference files, before any candidate is copied.

Keep compatible with Python 3.7. Never import the candidate or change assertions.
"""
import json
from pathlib import Path
import re
import sys
import site
from email.parser import Parser

REVISION = 'h100-grading-20260915-v2'


def replace(path, old, new):
    text = path.read_text()
    if new and new in text:
        return
    if not new and old not in text:
        return
    if old not in text:
        raise ValueError('Reference repair does not match: ' + str(path))
    path.write_text(text.replace(old, new))


def installed_reference_version(project):
    for directory in site.getsitepackages():
        for path in Path(directory).glob(project + '-*.dist-info/METADATA'):
            metadata = Parser().parsestr(path.read_text())
            if metadata.get('Name', '').lower() == project and metadata.get('Version'):
                return metadata['Version']
    raise ValueError('Missing trusted reference version for ' + project)


def repair(root, project, reference_version=None):
    root = Path(root)
    changed = []
    if project in ('pylama', 'python-pytest-cases'):
        # Its reference setup.py imports pkg_resources. Bind the build tool
        # version instead of letting an isolated build choose an incompatible one.
        if 'import pkg_resources' not in (root / 'setup.py').read_text():
            return dict(revision=REVISION, project=project, files=[])
        (root / 'pyproject.toml').write_text(
            '[build-system]\nrequires = ["setuptools==80.9.0", "wheel"]\n'
            'build-backend = "setuptools.build_meta"\n')
        changed.append('pyproject.toml')
    elif project == 'box':
        # The reference installer opportunistically compiles every .py file
        # when Cython happens to be installed. Grade the submitted Python
        # implementation using the installer's existing pure-Python fallback.
        replace(root / 'setup.py',
                '    from Cython.Build import cythonize\n',
                '    raise ImportError("Reference grading uses pure Python")\n')
        changed.append('setup.py')
    elif project == 'tenacity':
        # Official base has build-system only, with no package metadata at all.
        if (root / 'setup.cfg').exists() or (root / 'setup.py').exists():
            return dict(revision=REVISION, project=project, files=[])
        if '[project]' in (root / 'pyproject.toml').read_text():
            return dict(revision=REVISION, project=project, files=[])
        version = reference_version or installed_reference_version(project)
        (root / 'setup.cfg').write_text(
            '[metadata]\nname = tenacity\nversion = ' + version + '\n'
            '[options]\npackages = find:\n'
            '[options.packages.find]\ninclude = tenacity\n    tenacity.*\n')
        changed.append('setup.cfg')
    elif project == 'deslib':
        replace(root / 'tests/util/test_aggregation.py',
                'import pytest\n', 'import pytest\nimport numpy as np\n')
        if not (root / 'tests/test_des_integration.py').is_file():
            raise ValueError('Missing deslib reference test helper')
        replace(root / 'tests/util/test_faiss.py',
                'from deslib.tests.test_des_integration import load_dataset',
                'from ..test_des_integration import load_dataset')
        changed.extend(['tests/util/test_aggregation.py', 'tests/util/test_faiss.py'])
    elif project == 'pathlib2':
        # The helper was copied from CPython but retained a dependency on its
        # optional test package. Keep the permission-retry operation local.
        helper = '''def _force_run(path, func, *args):
    try:
        return func(*args)
    except OSError:
        os.chmod(path, stat.S_IRWXU)
        return func(*args)

'''
        path = root / 'tests/os_helper.py'
        replace(path, '            from test.support import _force_run\n', '')
        text = path.read_text()
        if helper not in text:
            path.write_text(text + '\n\n' + helper)
        changed.append('tests/os_helper.py')
    elif project == 'requests-html':
        path = root / 'tests/test_internet.py'
        text = path.read_text()
        if 'indirect=True' not in text:
            text, count = re.subn(r'urls = \[.*?\]',
                'urls = ["/site-%d/1" % i for i in range(5)]', text, count=1, flags=re.S)
            if count != 1:
                raise ValueError('Reference internet URL list does not match')
            text = text.replace("@pytest.mark.parametrize('url', urls)",
                                "@pytest.mark.parametrize('url', urls, indirect=True)")
            text = text.replace('def test_async_run():', 'def test_async_run(fixture_server):')
            text = text.replace('for url in urls:', 'for url in [fixture_server + path for path in urls]:')
            path.write_text(text)
        fixture = Path(__file__).with_name('http_fixture.py').read_text()
        conftest = root / 'tests/conftest.py'
        if conftest.exists() and conftest.read_text() != fixture:
            raise ValueError('Unexpected requests-html reference conftest')
        conftest.write_text(fixture)
        changed.extend(['tests/test_internet.py', 'tests/conftest.py'])
    return dict(revision=REVISION, project=project, files=changed)


if __name__ == '__main__':
    print(json.dumps({'reference_repairs': repair('/workspace', sys.argv[1])}))
