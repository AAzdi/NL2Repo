"""Prepare an isolated experiment and supervise its optional LiteLLM gateway."""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from claude_code.phoenix_config import (PHOENIX_API_BASE, ROUTES, default_template,
                                       prepare_phoenix, resolve_environment, routing_mode)
from claude_code.generation import EFFORTS, template_kwargs, token_limits
from claude_code.retries import retry_options
from claude_code.report import write_report, report_message
from grading.config import artifact_install_commands


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError('must be a non-negative integer')
    return number


def parser():
    result = argparse.ArgumentParser(
        prog='eval.sh',
        description='一条命令启动 Phoenix 网关和 Claude Code 评测。默认后台运行。',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog='密钥通过环境变量传入，不写入配置。例：\n'
               './eval.sh --name smoke-001 --phoenix-proxy proxy_IP:PORT '
               '--tasks six --concurrency 1\n'
               '完整说明：eval.README.zh-CN.md')
    result.add_argument('--name', required=True, help='新实验名；拒绝复用已有目录')
    result.add_argument('--base-url', '--sglang-url',
                        help='SGLang 地址，如 http://GPU:30000；gateway 模式也接受末尾 /v1')
    result.add_argument('--model', help='上游实际模型名；Phoenix 模式默认继承本机配置')
    result.add_argument('--mode', choices=('phoenix', 'gateway', 'direct'),
                        help='默认 phoenix；提供 --base-url 时默认 gateway；direct 复用 Anthropic 接口')
    result.add_argument('--phoenix-proxy',
                        help='Phoenix 代理目标；legacy 为 SGLang 地址，aws 为多云网关域名，azure 为租户网关地址')
    result.add_argument('--phoenix-routing', choices=tuple(ROUTES),
                        help='Phoenix 路由；默认 PHOENIX_ROUTING 或 legacy；B200 使用 aws，云 API 使用 azure')
    result.add_argument('--phoenix-backend-host',
                        help='多云后端 host:port；默认 SGLANG_IP_PORT，再回退到模板')
    result.add_argument('--phoenix-backend-region',
                        help='多云后端区域；默认 PHOENIX_BACKEND_REGION、模板或 us_aws')
    result.add_argument('--phoenix-trajectory-id',
                        help='多云路由标识；默认 SANDBOX_TRAJECTORY_ID，同步两个路由 ID 请求头')
    result.add_argument('--phoenix-config',
                        help='Phoenix 私有模板（JSON 格式的 YAML）；默认 .nl2repo-local/litellm.phoenix.yaml，'
                             'azure 路由默认 litellm.phoenix-azure.yaml')
    result.add_argument('--api-key-env', default='SGLANG_API_KEY',
                        help='上游密钥变量名；默认变量未设置时使用 dummy（无鉴权服务）')
    result.add_argument('--config', default=str(ROOT / 'config.claude_code.json'),
                        help='基础配置，继承各任务镜像和完整性策略')
    result.add_argument('--output-dir', default=str(ROOT / 'experiments'), help='实验输出根目录')
    result.add_argument('--tasks', default='*', help='逗号分隔任务名；"*" 为全量')
    result.add_argument('--skip-python-version-check', action='store_true',
                        help='跳过生成与评分环境的 Python 版本一致性检查；仍记录实际版本')
    result.add_argument('--max-turns', type=positive, default=100, help='每任务最大 agent 轮数')
    result.add_argument('--max-token', '--max-tokens', '--max-output-tokens',
                        dest='max_output_tokens', type=positive,
                        help='上游 max_tokens：单次思考与正文的总 token 上限；由宿主机转发层设置')
    result.add_argument('--context-length', type=positive,
                        help='Claude Code 上下文窗口；上游服务须已支持同样的长度')
    result.add_argument('--model-timeout-seconds', type=positive,
                        help='模型请求超时；托管网关和宿主机模型通道使用此值')
    result.add_argument('--chat-template-kwargs', type=json.loads,
                        help='上游模型模板参数 JSON；仅适用于支持这些参数的 Phoenix/gateway 部署')
    result.add_argument('--reasoning-effort', choices=EFFORTS,
                        help='上游推理强度；需模型支持，不等于独立思考 token 预算')
    result.add_argument('--incremental', action='store_true',
                        help='增加先实现最小可运行版本、再通过本地测试完善的任务指令')
    result.add_argument('--timeout-seconds', type=positive, default=7200,
                        help='单次生成超时秒数；也是默认的多次尝试共享预算，不含评分')
    result.add_argument('--generation-budget-seconds', type=positive,
                        help='准备、生成及重试等待的共享预算；默认等于 --timeout-seconds，不含评分')
    result.add_argument('--install-timeout-seconds', type=positive,
                        help='产物安装阶段及每条评分安装命令超时；默认 600 秒')
    result.add_argument('--grading-timeout-seconds', type=positive,
                        help='官方安装与测试命令合计超时；默认 1800 秒')
    result.add_argument('--generation-retries', type=nonnegative,
                        help='生成开始前，环境准备临时故障的额外重试次数；默认 1，0 禁用；不重新生成代码')
    result.add_argument('--evaluation-retries', type=nonnegative,
                        help='评分阶段临时故障的额外重试次数；复用已有代码；默认 2，0 禁用')
    result.add_argument('--retry-delay-seconds', type=float,
                        help='首次重试等待秒数，随后指数退避，上限 60 秒；默认 5')
    result.add_argument('--concurrency', type=positive, default=16, help='本实验任务并发数')
    result.add_argument('--gateway-port', type=int, default=0, help='本机网关端口；0 自动分配')
    result.add_argument('--startup-timeout', type=positive, default=120, help='网关启动等待秒数')
    result.add_argument('--keepalive-idle', type=positive, default=30, help='TCP 空闲探测等待秒数')
    result.add_argument('--keepalive-interval', type=positive, default=15, help='TCP 探测间隔秒数')
    result.add_argument('--keepalive-count', type=positive, default=4, help='TCP 失败探测次数')
    result.add_argument('--foreground', action='store_true', help='前台运行直到实验结束')
    result.add_argument('--dry-run', action='store_true', help='只显示配置；不写文件、不启动、不联网')
    return result


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def write_json(path, value):
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def prepare(args):
    if not re.fullmatch(r'\w[\w.-]*', args.name):
        raise ValueError('Invalid --name: use letters, digits, underscores, dots and hyphens')
    if not 0 <= args.gateway_port <= 65535:
        raise ValueError('--gateway-port must be between 0 and 65535')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', args.api_key_env):
        raise ValueError('--api-key-env must be an environment variable name')
    mode = args.mode or ('gateway' if args.base_url else 'phoenix')
    if mode == 'phoenix' and args.base_url:
        raise ValueError('Phoenix mode uses --phoenix-proxy, not --base-url')
    if mode != 'phoenix' and any(getattr(args, name) is not None for name in (
            'phoenix_config', 'phoenix_proxy', 'phoenix_routing', 'phoenix_backend_host',
            'phoenix_backend_region', 'phoenix_trajectory_id')):
        raise ValueError('Phoenix routing options require Phoenix mode')
    if mode == 'direct' and (args.chat_template_kwargs is not None or args.reasoning_effort is not None):
        raise ValueError('Upstream generation overrides require phoenix/gateway mode')
    if mode != 'phoenix' and (not args.base_url or not args.model):
        raise ValueError('--base-url and --model are required in gateway/direct mode')
    key = os.environ.get(args.api_key_env)
    if mode != 'phoenix' and not key and args.api_key_env != 'SGLANG_API_KEY':
        raise ValueError(f'Set the environment variable {args.api_key_env}')
    key = key or 'dummy'
    url = (args.base_url or PHOENIX_API_BASE).rstrip('/')
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('--base-url must be an HTTP(S) URL without credentials, query or fragment')
    parsed.port  # Validate malformed/non-numeric ports before starting any processes.
    if parsed.path.endswith(('/messages', '/chat/completions', '/responses', '/count_tokens')):
        raise ValueError('Supply a base URL, not a specific API endpoint')
    if mode == 'direct' and parsed.path.endswith('/v1'):
        raise ValueError('direct mode --base-url must exclude /v1')
    if args.model is not None and not args.model.strip():
        raise ValueError('--model must not be empty')
    directory = Path(args.output_dir).expanduser().resolve() / args.name
    if directory.exists():
        raise ValueError(f'Experiment directory already exists: {directory}; choose a new --name')
    config = json.loads(Path(args.config).expanduser().read_text(encoding='utf-8'))
    if config.get('harness') != 'claude_code':
        raise ValueError('--config must select harness=claude_code')
    tasks = [task.strip() for task in args.tasks.split(',')]
    if not all(tasks) or ('*' in tasks and tasks != ['*']):
        raise ValueError('--tasks must be "*" alone or comma-separated task names')
    available = {p.parent.name for p in (ROOT / 'test_files').glob('*/start.md')}
    selected = sorted(available) if tasks == ['*'] else list(dict.fromkeys(tasks))
    if not selected or set(selected) - available:
        raise ValueError('Unknown or empty task selection: ' + ', '.join(sorted(set(selected) - available)))
    options = config.setdefault('claude_code', {})
    if args.skip_python_version_check:
        options['check_python_version'] = False
    if not isinstance(options.get('check_python_version', True), bool):
        raise ValueError('check_python_version must be a boolean')
    for task in selected:
        if not isinstance(options.get('offline_environments', {}).get(task), dict):
            raise ValueError(f'Missing offline_environments entry for {task} in --config')
        artifact_install_commands(options['offline_environments'][task])
    # A template's old route must never override the new service address.
    options.pop('host_base_url', None)
    options.update(max_turns=args.max_turns, timeout_seconds=args.timeout_seconds)
    for name, default in [('generation_retries', 1), ('evaluation_retries', 2),
                          ('retry_delay_seconds', 5)]:
        value = getattr(args, name)
        options[name] = options.get(name, default) if value is None else value
    if args.generation_budget_seconds is not None:
        options['generation_budget_seconds'] = args.generation_budget_seconds
    options.update(retry_options(options))
    for name, default in [('install_timeout_seconds', 600), ('grading_timeout_seconds', 1800)]:
        value = getattr(args, name)
        if value is None:
            value = options.get(name, default)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f'{name} must be a positive integer')
        options[name] = value
    if args.incremental:
        options['incremental'] = True
    if not isinstance(options.get('incremental', False), bool):
        raise ValueError('incremental must be boolean')
    for limit_name in ('context_length', 'max_output_tokens', 'model_timeout_seconds'):
        if getattr(args, limit_name) is not None:
            options[limit_name] = getattr(args, limit_name)
    token_limits(options)
    config.update(experiment_name=args.name, output_dir=str(directory.parent),
                  max_pool_size=args.concurrency)
    gateway = None
    phoenix_environment = {}
    if mode in ('gateway', 'phoenix'):
        if mode == 'phoenix':
            route = routing_mode(args.phoenix_routing, os.environ)
            template = args.phoenix_config or default_template(ROOT, route)
            params, phoenix_environment = prepare_phoenix(
                Path(template).expanduser(), proxy=args.phoenix_proxy,
                model=args.model, routing=route,
                backend_host=args.phoenix_backend_host, backend_region=args.phoenix_backend_region,
                trajectory_id=args.phoenix_trajectory_id,
                timeout=options.get('model_timeout_seconds'), environment=os.environ)
            if route == 'aws':
                # Keep the local model channel and both proxy hops on one budget.
                options['model_timeout_seconds'] = params['timeout']
            key = phoenix_environment['NL2REPO_EVAL_UPSTREAM_KEY']
        else:
            params = {'model': 'openai/' + args.model,
                      'api_base': url if parsed.path.endswith('/v1') else url + '/v1',
                      'api_key': 'os.environ/NL2REPO_EVAL_UPSTREAM_KEY'}
            if 'model_timeout_seconds' in options:
                params['timeout'] = options['model_timeout_seconds']
        if args.chat_template_kwargs is not None:
            extra = params.setdefault('extra_body', {})
            extra['chat_template_kwargs'] = {
                **extra.get('chat_template_kwargs', {}), **template_kwargs(args.chat_template_kwargs)}
        if args.reasoning_effort is not None:
            params['reasoning_effort'] = args.reasoning_effort
        if 'max_output_tokens' in options:
            # Request max_tokens is enforced by model_channel. A template's
            # max_completion_tokens must not override it after conversion.
            params.pop('max_tokens', None)
            params.pop('max_completion_tokens', None)
        gateway = {
            'model_list': [{'model_name': 'repo-model', 'litellm_params': params}],
            'general_settings': {'master_key': 'os.environ/NL2REPO_EVAL_GATEWAY_KEY'},
            'litellm_settings': {
                'use_chat_completions_url_for_anthropic_messages': True,
                'callbacks': ['claude_code.phoenix_adapter.callback']}}
    config['startPro'] = [{
        'moduleName': 'repo-model' if gateway else args.model,
        'baseUrl': f'http://127.0.0.1:{args.gateway_port}' if gateway else url,
        'apiKeyEnv': 'NL2REPO_EVAL_GATEWAY_KEY' if gateway else 'NL2REPO_EVAL_UPSTREAM_KEY',
        'proNameList': selected}]
    environment = dict(os.environ)
    environment.update(phoenix_environment)
    environment.update(
        NL2REPO_GATEWAY_DIAGNOSTICS=str(directory / 'gateway.requests.jsonl'),
        PYTHONPATH=str(ROOT) + (os.pathsep + environment['PYTHONPATH'] if environment.get('PYTHONPATH') else ''),
        PYTHONDONTWRITEBYTECODE='1', LITELLM_LOCAL_MODEL_COST_MAP='True',
        AIOHTTP_SO_KEEPALIVE='true', AIOHTTP_TCP_KEEPIDLE=str(args.keepalive_idle),
        AIOHTTP_TCP_KEEPINTVL=str(args.keepalive_interval), AIOHTTP_TCP_KEEPCNT=str(args.keepalive_count),
        NL2REPO_EVAL_UPSTREAM_KEY=key, NL2REPO_EVAL_GATEWAY_KEY='sk-' + secrets.token_hex(24))
    plan = {'experiment_name': args.name, 'directory': str(directory), 'mode': mode,
            'startup_timeout': args.startup_timeout, 'task_count': len(selected),
            'keepalive': {k: environment[k] for k in ('AIOHTTP_SO_KEEPALIVE', 'AIOHTTP_TCP_KEEPIDLE',
                                                    'AIOHTTP_TCP_KEEPINTVL', 'AIOHTTP_TCP_KEEPCNT')},
            'config': config, 'gateway': gateway}
    if mode == 'phoenix':
        plan['phoenix_routing'] = route
    return plan, environment


def wait_for_gateway(process, url, key, timeout):
    """Require our authenticated gateway, not merely a listener on the same port."""
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Gateway exited during startup; see gateway.log')
        try:
            request = Request(url + '/v1/models', headers={'Authorization': 'Bearer ' + key})
            with opener.open(request, timeout=min(2, max(0.1, deadline - time.monotonic()))) as response:
                data = json.load(response)
                if any(item.get('id') == 'repo-model' for item in data.get('data', [])):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError('Gateway startup timed out; see gateway.log')


def stop_process(process, *, group=False, cooperative=False):
    if process is None or process.poll() is not None:
        return
    try:
        if group and not cooperative:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if group:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()


def supervise(plan, environment):
    directory = Path(plan['directory'])
    state = {k: plan[k] for k in ('experiment_name', 'mode', 'task_count', 'keepalive')}
    if 'phoenix_routing' in plan:
        state['phoenix_routing'] = plan['phoenix_routing']
    state.update(status='starting', started_at=timestamp(), supervisor_pid=os.getpid(),
                 experiment_path=str(directory), max_turns=plan['config']['claude_code']['max_turns'],
                 timeout_seconds=plan['config']['claude_code']['timeout_seconds'],
                 max_pool_size=plan['config']['max_pool_size'])
    state.update(token_limits(plan['config']['claude_code']))
    state['retry_policy'] = retry_options(plan['config']['claude_code'])
    gateway = benchmark = None
    runtime_directory = None
    code = 1

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        write_json(directory / 'run-state.json', state)
        with (directory / 'gateway.log').open('a') as gateway_log, (directory / 'benchmark.log').open('a') as benchmark_log:
            if plan['gateway']:
                # LiteLLM does not resolve environment references in nested headers.
                # Keep resolved credentials in a private temporary file, outside results.
                runtime_directory = tempfile.TemporaryDirectory(prefix='nl2repo-gateway-')
                runtime_config = Path(runtime_directory.name) / 'gateway.yaml'
                with runtime_config.open('x', encoding='utf-8') as output:
                    runtime_config.chmod(0o600)
                    json.dump(resolve_environment(plan['gateway'], environment), output)
                command = [str(ROOT / '.nl2repo-local/venv/bin/litellm'), '--config',
                           str(runtime_config), '--host', '127.0.0.1', '--port',
                           str(urlsplit(plan['config']['startPro'][0]['baseUrl']).port)]
                gateway = subprocess.Popen(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=gateway_log, stderr=subprocess.STDOUT, start_new_session=True)
                state['gateway_pid'] = gateway.pid
                write_json(directory / 'gateway.pid', gateway.pid)
                write_json(directory / 'run-state.json', state)
                wait_for_gateway(gateway, plan['config']['startPro'][0]['baseUrl'],
                                 environment['NL2REPO_EVAL_GATEWAY_KEY'], plan['startup_timeout'])
            benchmark = subprocess.Popen([sys.executable, str(ROOT / 'main.py'), '--config',
                                          str(directory / 'config.json')], cwd=ROOT, env=environment,
                                         stdin=subprocess.DEVNULL, stdout=benchmark_log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            state.update(status='running', benchmark_pid=benchmark.pid)
            write_json(directory / 'benchmark.pid', benchmark.pid)
            write_json(directory / 'run-state.json', state)
            print(f"Experiment running: {directory}", flush=True)
            while benchmark.poll() is None:
                if gateway is not None and gateway.poll() is not None:
                    raise RuntimeError('Gateway exited while benchmark was running; see gateway.log')
                time.sleep(0.5)
            code = benchmark.returncode
            state.update(status='completed' if code == 0 else 'failed', exit_code=code)
    except KeyboardInterrupt:
        code = 130
        state.update(status='interrupted', exit_code=code)
    except Exception as exc:
        state.update(status='failed', error=str(exc), exit_code=1)
        print(str(exc), file=sys.stderr, flush=True)
    finally:
        # Only this supervisor's subprocesses and task containers are stopped.
        if benchmark is not None and benchmark.poll() is None:
            stop_process(benchmark, group=True, cooperative=True)
        if benchmark is not None and state['status'] != 'completed':
            containers = [prefix + p.name for p in (directory / 'workspaces').glob('*-cc-*') if p.is_dir()
                          for prefix in ('nl2repo-', 'python-install-', 'python-test-')]
            if containers:
                try:
                    subprocess.run(['docker', 'rm', '-f', *containers], capture_output=True, timeout=30)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        from claude_code.lifecycle import finalize_unfinished
        try:
            finalize_unfinished(directory, status='interrupted' if state['status'] == 'interrupted' else 'error')
        except Exception as exc:
            state['result_finalization_error'] = str(exc)
        stop_process(gateway, group=True)
        if runtime_directory is not None:
            runtime_directory.cleanup()
        state['finished_at'] = timestamp()
        try:
            report = write_report(directory, config=plan['config'], run_state=state)
            state['report_path'] = str(directory / 'report.md')
            state['full_task_average_score'] = report['metrics']['full_task_average_score']
            print(report_message(directory, report), flush=True)
        except Exception as exc:
            state['report_error'] = str(exc)
            if code == 0:
                code = 1
                state.update(status='failed', exit_code=code)
            print(f'Failed to generate experiment report: {exc}', file=sys.stderr, flush=True)
        write_json(directory / 'run-state.json', state)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return code


def launch(args):
    plan, environment = prepare(args)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    subprocess.run(['docker', 'info', '--format', '{{.ServerVersion}}'],
                   check=True, stdout=subprocess.DEVNULL, timeout=30)
    # Task workers prepare their own images after acquiring a concurrency slot.
    if plan['gateway']:
        binary = ROOT / '.nl2repo-local/venv/bin/litellm'
        if not os.access(binary, os.X_OK):
            raise ValueError(f'LiteLLM executable missing: {binary}')
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', args.gateway_port))
            port = listener.getsockname()[1]
        plan['config']['startPro'][0]['baseUrl'] = f'http://127.0.0.1:{port}'
    directory = Path(plan['directory'])
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    write_json(directory / 'config.json', plan['config'])
    if plan['gateway']:
        write_json(directory / 'gateway.yaml', plan['gateway'])
    write_json(directory / 'launch.json', plan)
    if args.foreground:
        return supervise(plan, environment)
    with (directory / 'launcher.log').open('a') as log:
        supervisor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--_worker',
                                       str(directory / 'launch.json')], cwd=ROOT, env=environment,
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                      start_new_session=True)
    write_json(directory / 'supervisor.pid', supervisor.pid)
    deadline = time.monotonic() + args.startup_timeout + 10
    while time.monotonic() < deadline:
        state_path = directory / 'run-state.json'
        if state_path.exists():
            state = json.loads(state_path.read_text())
            if state['status'] in ('running', 'completed'):
                print(f"已启动：{args.name}\n目录：{directory}\n监督进程 PID：{supervisor.pid}\n"
                      f"任务数：{plan['task_count']}；并发：{args.concurrency}；轮数：{args.max_turns}\n"
                      f"状态：{state_path}\n日志：{directory / 'benchmark.log'}\n"
                      f"结束后报告：{directory / 'report.md'}")
                return 0
            if state['status'] in ('failed', 'interrupted'):
                raise RuntimeError(f"Startup failed; see {state_path} and gateway.log / benchmark.log")
        if supervisor.poll() is not None:
            raise RuntimeError(f"Supervisor exited; see {directory / 'launcher.log'}")
        time.sleep(0.2)
    stop_process(supervisor)
    raise RuntimeError(f'Startup timed out; see {directory}')


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--_worker':
        return supervise(json.loads(Path(sys.argv[2]).read_text()), dict(os.environ))
    args = parser().parse_args()
    try:
        return launch(args)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'eval.sh: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
