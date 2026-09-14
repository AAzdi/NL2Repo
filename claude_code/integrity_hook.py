"""Best-effort early feedback; the host broker and network namespace enforce policy.

This deliberately does not execute or resolve candidate code/paths. Unsupported
shell syntax and dynamic Python remain subject to the isolated network and broker.
"""
import ast
import json
from pathlib import Path
import re
import shlex
import sys
import textwrap


# Options whose values are not package requirements. In particular, a log file,
# destination directory, or index URL containing the target name is not a fetch.
_VALUE_OPTIONS = {
    '-r', '--requirement', '-c', '--constraint', '-t', '--target', '--prefix',
    '--root', '--src', '--python', '--proxy', '--retries', '--timeout',
    '--exists-action', '--trusted-host', '--cert', '--client-cert', '--cache-dir',
    '--log', '-i', '--index-url', '--extra-index-url', '-f', '--find-links',
    '--platform', '--python-version', '--implementation', '--abi',
    '--config-settings', '-C', '--only-binary', '--no-binary', '--progress-bar',
    '--root-user-action', '--report', '-d', '--dest', '--keyring-provider',
}
_SHELLS = {'sh', 'bash', 'dash', 'zsh', 'ksh'}
_PROCESS_CALLS = {'subprocess.run', 'subprocess.call', 'subprocess.check_call',
                  'subprocess.check_output', 'subprocess.Popen',
                  'subprocess.getoutput', 'subprocess.getstatusoutput',
                  'os.system', 'os.popen'}


def _normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _target_url(value, targets):
    if not re.match(r'(?:git\+)?https?://', value, re.I):
        return None
    for name in targets:
        pattern = r'(?<![a-z0-9_])' + re.escape(name).replace(r'\-', '[-_.]+') + r'(?![a-z0-9_])'
        if re.search(pattern, value.lower()):
            return name
    return None


def _requirement(value, targets):
    if value.startswith(('.', '/', '~', 'file:')):
        return None  # Local project/artifact; never inspect host filesystem paths.
    remote = _target_url(value, targets)
    if remote:
        return remote
    match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(?=\[|[<>=!~@;\s]|$)', value)
    if match and _normalized(match[1]) in targets:
        # Named PEP 508 references to local projects are also local installs.
        if re.search(r'@\s*(?:file:|[./~])', value):
            return None
        return _normalized(match[1])
    return None


def _commands(text):
    """Split unquoted shell operators, retaining quotes for shlex to decode.

    Unlike a regexp over the entire command, this cannot mistake a later test,
    comment, or echo for pip arguments. It is not a full shell interpreter.
    """
    start = 0
    quote = None
    escaped = False
    comment = False
    for i, char in enumerate(text):
        if comment:
            if char == '\n':
                comment = False
                start = i + 1
            continue
        if escaped:
            escaped = False
            continue
        if char == '\\' and quote != "'":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "\"'":
            quote = char
        elif char == '#' and (i == 0 or text[i - 1].isspace()):
            yield text[start:i]
            comment = True
        elif char in ';&|\n()':
            yield text[start:i]
            start = i + 1
    if not comment:
        yield text[start:]


def _argv_reason(argv, targets, depth):
    if not argv or depth > 8:
        return None
    # Ordinary environment assignments and command wrappers.
    while argv and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', argv[0])
                    or argv[0] in ('env', 'command', 'exec', 'sudo',
                                   'if', 'then', 'elif', 'else', 'do', 'while', 'until', '!')):
        argv = argv[1:]
    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable in _SHELLS:
        for i, arg in enumerate(argv[1:], 1):
            if re.fullmatch('-[a-z]*c[a-z]*', arg) and i + 1 < len(argv):
                return _shell_reason(argv[i + 1], targets, depth + 1)
    if re.fullmatch(r'python(?:\d+(?:\.\d+)*)?', executable):
        if '-c' in argv:
            i = argv.index('-c')
            return _python_reason(argv[i + 1], targets, depth + 1) if i + 1 < len(argv) else None
        if '-m' in argv:
            i = argv.index('-m')
            if i + 1 < len(argv) and argv[i + 1] == 'pip':
                return _argv_reason(['pip'] + argv[i + 2:], targets, depth + 1)
    pip = re.fullmatch(r'pip(?:\d+(?:\.\d+)*)?', executable)
    if executable == 'uv' and argv[1:2] == ['pip']:
        argv, pip = argv[1:], True
    if pip:
        installing = False
        skip_value = False
        for arg in argv[1:]:
            if skip_value:
                skip_value = False
                continue
            if arg in _VALUE_OPTIONS:
                skip_value = True
                continue
            if arg.startswith('-'):
                # Editable arguments are requirements too, not destination paths.
                editable = (arg.split('=', 1)[1] if arg.startswith('--editable=')
                            else arg[2:] if arg.startswith('-e') and not arg.startswith('--') else '')
                if installing and editable:
                    target = _requirement(editable, targets)
                    if target:
                        return ('install', target)
                continue
            if not installing:
                if arg in ('install', 'download'):
                    installing = True
                else:
                    return None  # e.g. pip show/list, not an installation.
            else:
                target = _requirement(arg, targets)
                if target:
                    return ('install', target)
    if executable in ('curl', 'wget') or (executable == 'git' and any(
            arg in ('clone', 'fetch') for arg in argv[1:])):
        for arg in argv[1:]:
            target = _target_url(arg.removeprefix('--url='), targets)
            if target:
                return ('download', target)
    return None


def _shell_reason(text, targets, depth=0):
    if depth > 8:
        return None
    # Literal heredocs used to run Python or write a Python script. Analyze the
    # body as code rather than interpreting its comments/strings as shell commands.
    heredoc = re.compile(r"(?m)^[^\n]*<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1[^\n]*\n(.*?)^\t*\2\s*$", re.S)
    for match in heredoc.finditer(text):
        header = match[0].split('\n', 1)[0].split('<<', 1)[0]
        found = _shell_reason(header, targets, depth + 1)
        if found:
            return found
        if re.search(r'>\s*[\'\"]?[^\s\'\"]+\.(?:md|rst|txt)[\'\"]?\s*$', header):
            continue
        found = _python_reason(match[3], targets, depth + 1)
        if found:
            return found
        if re.search(r'\b(?:sh|bash|dash|zsh|ksh)\b|\.(?:sh|bash)\b', header):
            found = _shell_reason(match[3], targets, depth + 1)
            if found:
                return found
    text = heredoc.sub('', text)
    for command in _commands(text):
        try:
            # Preserve redirection operators as tokens, so their paths cannot
            # become pip requirements (e.g. pip install . > retrying).
            lexer = shlex.shlex(command, posix=True, punctuation_chars='<>')
            lexer.whitespace_split = True
            tokens = list(lexer)
            argv = []
            i = 0
            while i < len(tokens):
                if tokens[i] in ('>', '>>', '<', '<>', '>|', '<<', '<<<'):
                    if argv and argv[-1].isdigit():
                        argv.pop()
                    i += 2
                else:
                    argv.append(tokens[i])
                    i += 1
        except ValueError:
            continue  # An incomplete edit is not evidence of a prohibited fetch.
        found = _argv_reason(argv, targets, depth)
        if found:
            return found
    return None


def _call_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _call_name(node.value) + '.' + node.attr
    return ''


def _python_reason(text, targets, depth=0):
    if depth > 8:
        return None
    try:
        tree = ast.parse(textwrap.dedent(text))
    except (SyntaxError, ValueError):
        return None
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = node.module + '.' + alias.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node.func)
        head, separator, tail = name.partition('.')
        name = aliases.get(head, head) + separator + tail
        if name in _PROCESS_CALLS:
            arg = node.args[0] if node.args else next(
                (k.value for k in node.keywords if k.arg in ('args', 'command', 'cmd')), None)
            try:
                if isinstance(arg, (ast.List, ast.Tuple)):
                    value = [('python' if _call_name(item) == 'sys.executable'
                              else ast.literal_eval(item)) for item in arg.elts]
                else:
                    value = ast.literal_eval(arg)
            except (ValueError, TypeError):
                continue
            if isinstance(value, str):
                found = _shell_reason(value, targets, depth + 1)
            elif isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value):
                found = _argv_reason(list(value), targets, depth + 1)
            else:
                continue
            if found:
                return found
        if name in ('urlopen', 'urlretrieve', 'urllib.request.urlopen',
                    'urllib.request.urlretrieve', 'requests.get', 'requests.post',
                    'httpx.get', 'httpx.post'):
            args = node.args[:1] + [k.value for k in node.keywords if k.arg in ('url', 'fullurl')]
            for arg in args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    target = _target_url(arg.value, targets)
                    if target:
                        return ('download', target)
    return None


def reason(event, policy):
    tool = event.get('tool_name')
    value = event.get('tool_input', {})
    targets = {_normalized(n) for n in policy['target_distributions']}
    if tool == 'Bash':
        found = _shell_reason(value.get('command', ''), targets)
    elif tool in ('Write', 'Edit'):
        suffix = Path(value.get('file_path', '')).suffix.lower()
        if suffix in ('.md', '.rst', '.txt'):
            return None
        text = value.get('content', value.get('new_string', ''))
        found = _python_reason(text, targets)
        if not found and suffix in ('.sh', '.bash', ''):
            found = _shell_reason(text, targets)
    else:
        return None
    if found:
        action, target = found
        return (f'Do not retrieve the target project: {action} of {target!r} was detected. '
                'Implement it independently. Installing your local project path and unrelated '
                'third-party dependencies is allowed.')
    return None


if __name__ == '__main__':
    try:
        event = json.load(sys.stdin)
        policy = json.loads(Path('/opt/nl2repo-integrity/policy.json').read_text())
        denied = reason(event, policy)
    except Exception:
        denied = 'Benchmark integrity hook failed; retry after reporting the error.'
    if denied:
        print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse',
              'permissionDecision': 'deny', 'permissionDecisionReason': denied}}))
