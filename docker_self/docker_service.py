from python_on_whales import DockerClient, Image, Container
import os
import json
import subprocess
from typing import List, Dict, Optional, Tuple, Union

from logging_config import get_logger

logger = get_logger(__name__)


class DockerHostInfo:
    """Docker Host Info Class"""

    def __init__(self, hostname: str, username: Optional[str] = None, password: Optional[str] = None,
                 tls_verify: Optional[bool] = None, cert_path: Optional[str] = None):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.tls_verify = tls_verify
        self.cert_path = cert_path or "/root/.docker"


def create_docker_client(host_info: DockerHostInfo) -> DockerClient:
    """Create Docker Client Connection

    Args:
        host_info: Docker Host Info

    Returns:
        Docker Client Instance
    """
    try:
        hostname = host_info.hostname

        if hostname in ['localhost', '127.0.0.1', 'local']:
            # 本地连接
            client = DockerClient()
            logger.info(f"Successfully created local Docker client connection: {hostname}")
            return client

        # 判断是否为TLS远程连接
        is_tls = hostname.startswith("https://") or (":2376" in hostname and not hostname.startswith("unix://"))

        if is_tls:

            # Handle TLS remote connection - remove https:// prefix, use tcp://
            if hostname.startswith("https://"):
                docker_host = "tcp://" + hostname[8:]  # Remove "https://" prefix
            elif not hostname.startswith("tcp://"):
                docker_host = "tcp://" + hostname
            else:
                docker_host = hostname

            # Set TLS verification (default to True if not specified)
            tls_verify = host_info.tls_verify if host_info.tls_verify is not None else True

            # Configure TLS environment variables, as python-on-whales uses Docker CLI
            original_environment = {key: os.environ.get(key) for key in (
                'DOCKER_HOST', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')}

            try:
                # Set Docker environment variables for TLS connection
                os.environ['DOCKER_HOST'] = docker_host

                if tls_verify:
                    os.environ['DOCKER_TLS_VERIFY'] = '1'
                    if host_info.cert_path:
                        os.environ['DOCKER_CERT_PATH'] = host_info.cert_path
                else:
                    # Clear TLS verification environment variables if not using TLS
                    if 'DOCKER_TLS_VERIFY' in os.environ:
                        del os.environ['DOCKER_TLS_VERIFY']
                    if 'DOCKER_CERT_PATH' in os.environ:
                        del os.environ['DOCKER_CERT_PATH']

                # Create Docker client, python-on-whales will automatically read environment variables
                client = DockerClient()
                logger.info(f"Successfully created TLS Docker client connection: {docker_host}, TLS verify: {tls_verify}")

                return client
            finally:
                # Restore the caller's environment on both success and failure.
                for key, value in original_environment.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        else:
            # Handle local connection or non-TLS remote connection
            if "/var/run/docker.sock" in hostname or "/docker-cli.sock" in hostname:
                host_url = "unix:///var/run/docker.sock"
            else:

                # Add default port if not specified
                if ":" not in hostname:
                    host_url = f"tcp://{hostname}:2375"
                else:
                    host_url = f"tcp://{hostname}" if not hostname.startswith("tcp://") else hostname

            client = DockerClient(host=host_url)
            logger.info(f"Successfully created non-TLS Docker client connection: {host_url}")

        return client
    except Exception as e:
        logger.error(f"Create Docker client failed: {e}")
        raise RuntimeError(f"Create Docker client failed: {e}")


# ==================== 镜像操作 ====================


def pull_image(host_info: DockerHostInfo, image_name: str, tag: str = "latest") -> Image:
    """Pull Docker image

    Args:
        host_info: Docker host information
        image_name: Image name
        tag: Image tag

    Returns:
        Pulled Docker image object
    """
    try:
        client = create_docker_client(host_info)

        # If image_name already contains tag, use it directly, otherwise add tag
        if ':' in image_name:
            full_image_name = image_name
        else:
            full_image_name = f"{image_name}:{tag}" if tag else image_name

        logger.info(f"Start pulling image: {full_image_name}")

        image = client.image.pull(full_image_name)
        logger.info(f"Successfully pulled image: {full_image_name}")
        return image
    except Exception as e:
        logger.error(f"Failed to pull image: {e}")
        raise RuntimeError(f"Failed to pull image: {e}")


def build_image(host_info: DockerHostInfo, dockerfile_path: str, tag: str, build_context_path: str = None,
                timeout_seconds: float = 600) -> Tuple[Image, List[str]]:
    """Build Docker image

    Args:
        host_info: Docker host information
        dockerfile_path: Dockerfile path
        tag: Image tag
        build_context_path: Build context path, default to Dockerfile directory

    Returns:
        Built Docker image object and build logs
    """
    try:
        client = create_docker_client(host_info)

        if build_context_path is None:
            build_context_path = os.path.dirname(dockerfile_path)

        logger.info(f"Start building image, Dockerfile path: {dockerfile_path}, tag: {tag}")

        result = subprocess.run([*client.docker_cmd, 'build', '--network', 'none',
                                 '--file', dockerfile_path, '--tag', tag, build_context_path],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                timeout=timeout_seconds, check=True)
        logs = result.stdout.splitlines()
        image = client.image.inspect(tag)

        logger.info(f"Successfully built image, tag: {tag}")
        return image, logs
    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        logger.error(f"Failed to build image: {e}")
        raise RuntimeError(f"Failed to build image: {e}")


# ==================== 容器操作 ====================


def remove_container(host_info: DockerHostInfo, container_id: str, force: bool = False,
                     timeout_seconds: float = 30) -> None:
    """Remove Docker container

    Args:
        host_info: Docker host information
        container_id: Container ID or name
        force: Whether to force removal of running containers
    """
    try:
        client = create_docker_client(host_info)
        logger.info(f"Remove container: {container_id}, force: {force}")

        subprocess.run([*client.docker_cmd, 'container', 'rm',
                        *(['--force'] if force else []), container_id],
                       capture_output=True, text=True, check=True, timeout=timeout_seconds)
        logger.info(f"Container removed: {container_id}")
    except Exception as e:
        logger.error(f"Failed to remove container: {e}")
        raise RuntimeError(f"Failed to remove container: {e}")


def create_advanced_container(host_info: DockerHostInfo, image_name: str, container_name: str = None,
                            env_vars: Optional[Dict[str, str]] = None,
                            ports: Optional[Dict[str, int]] = None,
                            volumes: Optional[List[Tuple[str, str, str]]] = None,
                            command: Optional[Union[str, List[str]]] = None,
                            auto_remove: bool = False,
                            extra_hosts: Optional[Dict[str, str]] = None,
                            pull_always: bool = False,
                            network_mode: Optional[str] = None,
                            working_dir: Optional[str] = None,
                            user: Optional[str] = None,
                            privileged: bool = False,
                            **kwargs) -> Container:
    """Create advanced Docker container with specified configuration

    Args:
        host_info: Docker host information
        image_name: Image name
        container_name: Container name
        env_vars: Environment variables dictionary
        ports: Port mappings dictionary {'container_port': host_port}
        volumes: Volume mounts list [('host_path', 'container_path', 'mode')]
        command: Start command
        auto_remove: Whether to remove container when it stops
        extra_hosts: Extra host mappings dictionary {'hostname': 'IP'}
        pull_always: Whether to always pull image
        network_mode: Network mode
        working_dir: Working directory
        user: Run user
        privileged: Whether to run container in privileged mode
        **kwargs: Other container configuration parameters

    Returns:
        Created and started container object
    """
    try:
        client = create_docker_client(host_info)
        logger.info(f"Create advanced container: {container_name or 'auto-named'} based on image: {image_name}")

        # 如果需要总是拉取镜像
        if pull_always:
            logger.info(f"Always pull image mode, start pulling latest image: {image_name}")
            try:
                pull_image(host_info, image_name)
            except Exception as e:
                logger.warning(f"Failed to pull latest image: {e}, continue using local image")

        # 处理卷挂载路径中的波浪线
        if volumes:
            processed_volumes = []
            for host_path, container_path, mode in volumes:
                if host_path.startswith("~"):
                    host_path = os.path.expanduser(host_path)
                    logger.info(f"Replace tilde path with user home directory: {host_path}")
                processed_volumes.append((host_path, container_path, mode))
            volumes = processed_volumes


        # Convert port mappings
        publish = []
        if ports:
            for container_port, host_port in ports.items():

                # Remove protocol suffix if exists
                clean_container_port = container_port.split('/')[0] if '/' in str(container_port) else container_port
                publish.append((host_port, clean_container_port))

        # Convert extra hosts mappings
        add_hosts_list = []
        if extra_hosts:
            for hostname, ip in extra_hosts.items():
                add_hosts_list.append((hostname, ip))

        # Build run kwargs, only include non-None and non-empty values
        run_kwargs = {
            "name": container_name,
            "command": command,
            "publish": publish if publish else None,
            "volumes": volumes,
            "remove": auto_remove,
            "add_hosts": add_hosts_list if add_hosts_list else None,
            "networks": [network_mode] if network_mode else None,
            "workdir": working_dir,
            "user": user,
            "privileged": privileged,
            "detach": True,
            **kwargs
        }

        # Only add env_vars if it's not None
        if env_vars is not None:
            run_kwargs["envs"] = env_vars

        # Remove None values from kwargs
        run_kwargs = {k: v for k, v in run_kwargs.items() if v is not None}

        container = client.container.run(image_name, **run_kwargs)

        logger.info(f"Create and start advanced container: {container_name or 'auto-named'} with ID: {container.id}")
        return container
    except Exception as e:
        logger.error(f"Create and start advanced container failed: {e}")
        raise RuntimeError(f"Create and start advanced container failed: {e}")


def collect_container_diagnostics(host_info: DockerHostInfo, container_id: str) -> Dict:
    """Capture bounded state/log diagnostics before cleanup, without container credentials."""
    diagnostics = {}
    try:
        client = create_docker_client(host_info)
    except Exception as exc:
        return {'collection_error': str(exc)}
    for key, command in (
            ('state', ['container', 'inspect', '--format', '{{json .State}}', container_id]),
            ('logs', ['logs', '--timestamps', '--tail', '200', container_id])):
        try:
            result = subprocess.run([*client.docker_cmd, *command],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, timeout=10, check=False)
            if result.returncode:
                diagnostics[key + '_error'] = result.stdout[-65536:]
            elif key == 'state':
                state = json.loads(result.stdout)
                diagnostics['state'] = {name: state[name] for name in (
                    'Status', 'Running', 'Paused', 'Restarting', 'OOMKilled', 'Dead',
                    'ExitCode', 'Error', 'StartedAt', 'FinishedAt') if name in state}
            else:
                diagnostics['logs'] = result.stdout[-65536:]
                diagnostics['logs_truncated'] = len(result.stdout) > 65536
        except Exception as exc:
            diagnostics[key + '_error'] = str(exc)
    return diagnostics


def execute_command_in_container(host_info: DockerHostInfo, container_id: str, command: Union[str, List[str]],
                               user: Optional[str] = None, workdir: Optional[str] = None,
                               timeout_seconds: Optional[float] = None) -> Tuple[int, str]:
    """Execute command in container

    Args:
        host_info: Docker host information
        container_id: Container ID or name
        command: Command to execute
        user: User to run command as
        workdir: Working directory

    Returns:
        (exit_code, output)
    """
    try:
        client = create_docker_client(host_info)

        # Ensure command format
        if isinstance(command, str):
            # Use shlex.split to handle complex command strings with quotes and escapes
            import shlex
            command_list = shlex.split(command)
        else:
            command_list = command

        logger.info(f"Execute command in container {container_id}: {command_list}")

        if timeout_seconds is not None:
            # Bound the Docker client on the host. The caller must remove the
            # container on timeout: killing docker exec alone leaves code running.
            result = subprocess.run(
                [*client.docker_cmd, 'exec', *(['--user', user] if user else []),
                 *(['--workdir', workdir] if workdir else []), container_id, *command_list],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                timeout=timeout_seconds, check=False)
            return result.returncode, result.stdout

        result = client.container.execute(
            container_id,
            command_list,
            user=user,
            workdir=workdir
        )

        logger.info(f"Command execution completed in container {container_id}")
        # python-on-whales returns string output, exit code is handled via exception
        return 0, result

    except subprocess.TimeoutExpired:
        raise
    except Exception as e:
        logger.error(f"Command execution failed in container {container_id}: {e}")
        # If it's a command execution error, try to get exit code info
        exit_code = getattr(e, 'return_code', 1)
        output = str(e)
        return exit_code, output
