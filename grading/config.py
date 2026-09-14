"""Trusted, per-task artifact installation settings."""


def artifact_install_commands(entry):
    commands = entry.get('artifact_install_commands', ['python -m pip install -e .'])
    if (not isinstance(commands, list) or not commands
            or any(not isinstance(command, str) or not command.strip() for command in commands)):
        raise ValueError('artifact_install_commands must be a non-empty list of command strings')
    return list(commands)
