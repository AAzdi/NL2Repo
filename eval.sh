#!/usr/bin/env bash
# One-command Claude Code evaluation. See ./eval.sh --help.
set -euo pipefail
eval_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$eval_repo_dir/.nl2repo-local/gateway.env" ]]; then
    source "$eval_repo_dir/.nl2repo-local/gateway.env"
fi
eval_python="${NL2REPO_PYTHON:-$eval_repo_dir/.nl2repo-local/venv/bin/python}"
if [[ ! -x "$eval_python" ]]; then
    echo "Python not found: $eval_python (set NL2REPO_PYTHON to override)" >&2
    exit 1
fi
exec "$eval_python" "$eval_repo_dir/claude_code/launch.py" "$@"
