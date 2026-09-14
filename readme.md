# NL2RepoBench

NL2RepoBench evaluates LLMs and coding agents on long-horizon tasks that generate
complete, runnable repositories from scratch. It contains 104 tasks with reference
tests and task-specific Docker environments.

## Setup

The benchmark runs Claude Code with isolated generation and grading containers.
Install Docker and prepare a Python 3.12 environment:

```bash
python3.12 -m venv .nl2repo-local/venv
.nl2repo-local/venv/bin/pip install -r requirements.txt
```

Follow the [runtime setup guide](claude_code/README.zh-CN.md) to build the Claude
Code image, configure task images, and provide private Phoenix gateway settings.
Private configuration and credentials belong in `.nl2repo-local/`, which Git ignores.

## Run an evaluation

For Claude Code through Phoenix + SGLang:

```bash
PHOENIX_DOMAIN_PROXY='proxy_MODEL_HOST:PORT' ./eval.sh \
  --name phoenix-smoke-001 --tasks six --concurrency 1
```

Choose a new experiment name for every run. Results and gateway/benchmark logs go
to `experiments/<name>/`. Add `--dry-run` to preview without starting containers
or a gateway. See the [evaluation guide](eval.README.zh-CN.md) for model overrides,
full evaluations, direct mode, and stopping runs.

To use an already configured endpoint directly:

```bash
.nl2repo-local/venv/bin/python main.py --config config.claude_code.json \
  --experiment-name direct-001 --output-dir experiments
```

`main.py` defaults to `config.claude_code.json`. Configure the endpoint and
credentials before using this lower-level entrypoint; `eval.sh` prepares them
for each run automatically.

## Repository layout

| Path | Purpose |
| --- | --- |
| `claude_code/` | Launcher, generation runtime, model/dependency channels, trajectory capture |
| `grading/` | Artifact installation checks, reference test preparation, scoring |
| `docker_self/` | Docker operations used by the grader |
| `test_files/` | Task descriptions, test commands, reference file lists, test counts |
| `tests/` | Regression tests and optional Docker integration tests |
| `config.claude_code.json` | Task selection, concurrency, generation and grading images |
| `experiments/` | Local experiment configurations, logs, results, and generated workspaces |

`logs/`, `result/`, and `workspaces/` may contain outputs from direct or earlier
runs. Generated outputs and Python caches are ignored by Git.

## Verify

```bash
PYTHONDONTWRITEBYTECODE=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
  .nl2repo-local/venv/bin/python -m unittest discover -s tests -v
```

The default suite uses local mock services. Set `NL2REPO_TEST_DOCKER=1` to include
integration tests that require Docker and prepared task/CLI images. See the
[integrity guide](claude_code/README.integrity.zh-CN.md) for isolation and grading rules.
