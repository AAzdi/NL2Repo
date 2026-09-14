"""Build per-experiment Phoenix routes without changing the private template."""

from dataclasses import dataclass
import json
from urllib.parse import urlsplit

from claude_code.generation import generation_parameters


PHOENIX_API_BASE = 'http://phoenix-gw-eval.alibaba.com/eval/v1'
AZURE_API_BASE = 'http://phoenix-gw-eval.alibaba.com/eval/azure'
MULTICLOUD_PROXY = 'accio-agentic-rl-multicloud-gateway-aws.vipserver:80'


@dataclass(frozen=True)
class PhoenixRoute:
    api_base: str
    template_name: str
    proxy_env: str
    default_proxy: str = ''


# Route defaults live here; machine-specific overrides belong in gateway.env.
ROUTES = {
    'legacy': PhoenixRoute(PHOENIX_API_BASE, 'litellm.phoenix.yaml', 'PHOENIX_DOMAIN_PROXY'),
    'aws': PhoenixRoute(PHOENIX_API_BASE, 'litellm.phoenix.yaml',
                        'PHOENIX_MULTICLOUD_PROXY', MULTICLOUD_PROXY),
    'azure': PhoenixRoute(AZURE_API_BASE, 'litellm.phoenix-azure.yaml',
                          'PHOENIX_AZURE_DOMAIN_PROXY'),
}
AWS_BACKEND_REGION = 'us_aws'
AWS_TIMEOUT_SECONDS = 3600
AZURE_HEADERS = ('x-eval-token', 'x-eval-domain-proxy', 'tenant', 'empid', 'iai-tag')


def normalize_proxy(value):
    if not isinstance(value, str):
        raise ValueError('Phoenix proxy must be a host:port or HTTP(S) origin')
    value = value.strip()
    if value.startswith('proxy_'):
        value = value[len('proxy_'):]
    if '://' not in value:
        value = 'http://' + value
    parsed = urlsplit(value)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')
            or any(c.isspace() or ord(c) < 32 for c in value)
            or parsed.port == 0):
        raise ValueError('Phoenix proxy must be a host:port or HTTP(S) origin')
    return value.rstrip('/')


def resolve_environment(value, environment):
    if isinstance(value, dict):
        return {key: resolve_environment(item, environment) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_environment(item, environment) for item in value]
    if isinstance(value, str) and value.startswith('os.environ/'):
        name = value[len('os.environ/'):]
        if not environment.get(name):
            raise ValueError(f'Set the environment variable {name}')
        return environment[name]
    return value


def routing_mode(value, environment):
    routing = value if value is not None else environment.get('PHOENIX_ROUTING', 'legacy')
    if routing not in ROUTES:
        raise ValueError('PHOENIX_ROUTING must be legacy, aws or azure')
    return routing


def default_template(root, routing):
    return root / '.nl2repo-local' / ROUTES[routing].template_name


def _setting(explicit, environment, variable, fallback):
    """CLI > environment > template/default, including explicit empty values."""
    value = explicit if explicit is not None else environment.get(variable, fallback)
    return resolve_environment(value, environment)


def header_value(value, name, environment):
    value = resolve_environment(value, environment)
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(ord(c) < 32 or ord(c) > 126 for c in value)):
        raise ValueError(f'Set a non-empty printable value for {name}')
    return value


def _load_source(path):
    config = json.loads(path.read_text(encoding='utf-8'))
    model_list = config.get('model_list') if isinstance(config, dict) else None
    if not isinstance(model_list, list) or any(not isinstance(m, dict) for m in model_list):
        raise ValueError('Phoenix config must contain a model_list array of objects')
    models = [m for m in model_list if m.get('model_name') == 'repo-model']
    if len(models) != 1:
        raise ValueError('Expected exactly one repo-model route in Phoenix config')
    source = models[0].get('litellm_params')
    if not isinstance(source, dict):
        raise ValueError('Phoenix repo-model must contain a litellm_params object')
    if not isinstance(source.get('extra_headers', {}), dict):
        raise ValueError('Phoenix extra_headers must be an object')
    return source


def _proxy_target(route, headers, proxy, environment, *, use_template=True):
    fallback = headers.get('x-eval-domain-proxy', route.default_proxy) if use_template else route.default_proxy
    return normalize_proxy(_setting(proxy, environment, route.proxy_env, fallback))


def _azure_headers(headers, proxy, environment):
    # Only cloud headers may reach the tenant gateway, even with a GPU template.
    headers = {name: headers.get(name, '') for name in AZURE_HEADERS}
    headers['x-eval-domain-proxy'] = _proxy_target(ROUTES['azure'], headers, proxy, environment)
    headers['x-eval-token'] = _setting(None, environment, 'PHOENIX_AZURE_EVAL_TOKEN',
                                     headers['x-eval-token'])
    return {name: header_value(value, name, environment) for name, value in headers.items()}


def _legacy_headers(headers, proxy, environment):
    target = _proxy_target(ROUTES['legacy'], headers, proxy, environment)
    headers['x-eval-domain-proxy'] = target
    headers['x-backend-trajectoryid'] = 'proxy_' + urlsplit(target).netloc
    return headers


def _aws_headers(headers, proxy, backend_host, backend_region, trajectory_id, environment):
    # A backend-host marks a multicloud template. Do not inherit a legacy proxy
    # or an IP-derived trajectory ID when switching clusters.
    template_multicloud = bool(headers.get('x-backend-host'))
    target = _proxy_target(ROUTES['aws'], headers, proxy, environment,
                           use_template=template_multicloud)
    headers['x-eval-domain-proxy'] = urlsplit(target).netloc
    host = _setting(backend_host, environment, 'SGLANG_IP_PORT', headers.get('x-backend-host', ''))
    host = header_value(host, 'SGLANG_IP_PORT / --phoenix-backend-host', environment)
    parsed_host = urlsplit('//' + host)
    if (not parsed_host.hostname or not parsed_host.port or parsed_host.username is not None
            or parsed_host.password is not None or parsed_host.path or parsed_host.query
            or parsed_host.fragment or any(c.isspace() for c in host)):
        raise ValueError('Phoenix backend host must be host:port without a URL scheme or path')
    region = _setting(backend_region, environment, 'PHOENIX_BACKEND_REGION',
                      headers.get('x-backend-region', AWS_BACKEND_REGION))
    region = header_value(region, 'PHOENIX_BACKEND_REGION', environment)
    trajectory = _setting(trajectory_id, environment, 'SANDBOX_TRAJECTORY_ID',
                          headers.get('x-backend-trajectoryid', '') if template_multicloud else '')
    trajectory = header_value(trajectory, 'SANDBOX_TRAJECTORY_ID / --phoenix-trajectory-id', environment)
    headers.update({'x-backend-host': host, 'x-backend-region': region,
                    'x-smg-routing-key': trajectory, 'x-backend-trajectoryid': trajectory})
    return headers


def _apply_timeout(source, headers, routing, timeout):
    explicit = timeout is not None
    if not explicit:
        timeout = source.get('timeout', AWS_TIMEOUT_SECONDS if routing == 'aws' else None)
    if timeout is not None or routing == 'aws' or 'timeout' in source:
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError(f'Phoenix {routing} timeout must be a positive integer in seconds')
    if explicit or routing == 'aws':
        headers['x-eval-timeout'] = str(timeout)
    if routing == 'aws':
        headers['x-backend-timeout'] = str(timeout)
    return timeout


def _private_headers(headers, routing, upstream_key):
    """Keep credentials and multicloud IDs out of experiment snapshots."""
    secrets = {'NL2REPO_EVAL_UPSTREAM_KEY': upstream_key}
    public_headers = {}
    for index, (name, value) in enumerate(headers.items()):
        if (name in ('x-eval-domain-proxy', 'x-eval-timeout', 'x-backend-timeout')
                or (routing == 'legacy' and name == 'x-backend-trajectoryid')):
            public_headers['X-Backend-TrajectoryID' if name == 'x-backend-trajectoryid' else name] = value
        else:
            variable = f'NL2REPO_EVAL_PHOENIX_HEADER_{index}'
            secrets[variable] = str(value)
            public_headers[name] = 'os.environ/' + variable
    return public_headers, secrets


def prepare_phoenix(path, *, proxy=None, model=None, routing=None,
                    backend_host=None, backend_region=None, trajectory_id=None,
                    timeout=None, environment):
    """Resolve one route and its timeouts without modifying the private template."""
    routing = routing_mode(routing, environment)
    if routing != 'aws' and any(v is not None for v in (backend_host, backend_region, trajectory_id)):
        raise ValueError('Phoenix backend/trajectory options require --phoenix-routing aws')
    source = _load_source(path)
    headers = {k.lower(): v for k, v in source.get('extra_headers', {}).items()}
    if routing == 'azure':
        headers = _azure_headers(headers, proxy, environment)
    elif routing == 'legacy':
        headers = _legacy_headers(headers, proxy, environment)
    else:
        headers = _aws_headers(headers, proxy, backend_host, backend_region, trajectory_id, environment)
    timeout = _apply_timeout(source, headers, routing, timeout)
    if routing != 'azure':
        # Switching to AWS must not reuse a legacy template's credential.
        if routing == 'aws' and not environment.get('PHOENIX_EVAL_TOKEN'):
            raise ValueError('Set PHOENIX_EVAL_TOKEN for the AWS route')
        default_token = '' if routing == 'aws' else headers.get('x-eval-token', '')
        headers['x-eval-token'] = environment.get('PHOENIX_EVAL_TOKEN', default_token)
    headers = resolve_environment(headers, environment)
    if not headers['x-eval-token']:
        raise ValueError('Set PHOENIX_EVAL_TOKEN or x-eval-token in the Phoenix config')
    selected_model = 'openai/' + model if model is not None else source.get('model', 'openai/default')
    if (not isinstance(selected_model, str) or not selected_model.startswith('openai/')
            or not selected_model[len('openai/'):].strip()):
        raise ValueError('Phoenix config model must use openai/<served-model-name>')
    # IAI uses the Bearer value as its tenant selector when Authorization is
    # present. LiteLLM always sends that header, so "dummy" overrides the real
    # tenant header and produces "tenant=dummy config is not exist".
    upstream_key = headers['tenant'] if routing == 'azure' else resolve_environment(
        source.get('api_key') or 'dummy', environment)
    public_headers, secrets = _private_headers(headers, routing, upstream_key)
    params = {'model': selected_model, 'api_base': ROUTES[routing].api_base,
              'api_key': 'os.environ/NL2REPO_EVAL_UPSTREAM_KEY',
              'extra_headers': public_headers}
    if routing == 'azure':
        # LiteLLM can automatically route GPT tool requests to Responses.
        # This tenant endpoint implements Chat Completions explicitly.
        params['_skip_responses_api_bridge'] = True
    if timeout is not None:
        params['timeout'] = timeout
    params.update(generation_parameters(source))
    return params, secrets
