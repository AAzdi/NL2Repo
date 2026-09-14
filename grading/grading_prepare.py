"""Repair dangling legacy editable metadata in a disposable official grader.

Run with python -I -S before copying the submission. No package code is imported;
reference tests, packaging and live dependency installations are left intact.
"""

import json
from pathlib import Path
import site


def clean_dangling_editables(site_dirs):
    removed = []
    for directory in dict.fromkeys(site_dirs):
        root = Path(directory)
        dangling = set()
        for link in sorted(root.glob('*.egg-link')):
            lines = link.read_text().splitlines()
            if not lines or not lines[0].strip():
                continue
            target = Path(lines[0].strip())
            if not target.is_absolute():
                target = root / target
            target = target.resolve()
            if target.exists():
                continue
            dangling.add(target)
            link.unlink()
            removed.append(str(link))
        pth = root / 'easy-install.pth'
        if dangling and pth.is_file():
            lines = pth.read_text().splitlines(keepends=True)
            kept = []
            for line in lines:
                value = line.strip()
                if not value or value.startswith(('#', 'import ', 'import\t')):
                    kept.append(line)
                elif (root / value).resolve() not in dangling:
                    kept.append(line)
            if kept != lines:
                pth.write_text(''.join(kept))
                removed.append(str(pth))
    return removed


if __name__ == '__main__':
    print(json.dumps({'grading_preparation': 'dangling_editables',
                      'changed_files': clean_dangling_editables(site.getsitepackages())}))
