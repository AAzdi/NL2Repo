"""Task-scoped PyPI wheel broker. No candidate code executes on the host."""

from contextlib import contextmanager
from email.parser import BytesParser
import hashlib
from html import escape
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import tempfile
import threading
import time
import shutil
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlsplit
import zipfile

from claude_code.model_channel import NoRedirect, Server


def normalized(name):
    return re.sub(r'[-_.]+', '-', name).lower()


class WheelBroker:
    def __init__(self, policy, audit_path):
        self.targets = {normalized(n) for n in policy['target_distributions']}
        self.modules = set(policy['target_modules'])
        self.audit_path = Path(audit_path)
        self.artifacts = {}
        self.lock = threading.Lock()
        self.index_locks = {}
        self.index_cache = {}
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def audit(self, **event):
        with self.lock, self.audit_path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + '\n')

    def fetch(self, url, limit, *, metadata=False, destination=None):
        parsed = urlsplit(url)
        expected = 'pypi.org' if metadata else 'files.pythonhosted.org'
        if parsed.scheme != 'https' or parsed.hostname != expected or parsed.username or parsed.port:
            raise ValueError('Untrusted package destination')
        headers = {'Accept': 'application/vnd.pypi.simple.v1+json'} if metadata else {}
        with self.opener.open(urllib.request.Request(url, headers=headers), timeout=90) as response:
            if destination is None:
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise ValueError('Package response exceeds size limit')
                return body
            size = 0
            deadline = time.monotonic() + 480
            while True:
                chunk = response.read(min(1024 * 1024, limit + 1 - size))
                if not chunk:
                    return size
                size += len(chunk)
                if size > limit:
                    raise ValueError('Package response exceeds size limit')
                if time.monotonic() > deadline:
                    raise TimeoutError('Package download deadline exceeded')
                destination.write(chunk)

    def index(self, name):
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', name):
            raise ValueError('Invalid package name')
        name = normalized(name)
        if name in self.targets:
            self.audit(action='deny', package=name, reason='target distribution')
            raise ValueError('Installing the target project is forbidden')
        with self.lock:
            lock = self.index_locks.setdefault(name, threading.Lock())
        with lock:
            cached = self.index_cache.get(name)
            if cached and time.monotonic() - cached[0] < 300:
                return cached[1]
            body = self._index(name)
            self.index_cache[name] = (time.monotonic(), body)
            return body

    def _index(self, name):
        metadata = json.loads(self.fetch('https://pypi.org/simple/' + name + '/',
                                         16 * 1024 * 1024, metadata=True))
        if normalized(metadata['name']) != name:
            raise ValueError('Registry package name mismatch')
        links = []
        for item in metadata['files']:
            filename = item['filename']
            digest = item.get('hashes', {}).get('sha256', '')
            if not filename.endswith('.whl') or not re.fullmatch('[0-9a-f]{64}', digest):
                continue
            if '/' in filename or '\\' in filename:
                raise ValueError('Invalid wheel filename')
            # Artifact URLs are registry-provided and checked again at fetch time.
            with self.lock:
                self.artifacts[digest] = (name, filename, item['url'])
            attrs = ''
            if item.get('requires-python'):
                attrs += ' data-requires-python="' + escape(item['requires-python'], quote=True) + '"'
            if item.get('yanked'):
                attrs += ' data-yanked="yanked by registry"'
            links.append(f'<a href="../../wheel/{digest}/{quote(filename)}#sha256={digest}"{attrs}>'
                         + escape(filename) + '</a>')
        self.audit(action='index', package=name, wheels=len(links))
        return ('<!DOCTYPE html><html><body>' + '\n'.join(links) + '</body></html>').encode()

    @contextmanager
    def open_wheel(self, digest, filename):
        with self.lock:
            item = self.artifacts.get(digest)
        if not item or item[1] != filename:
            raise ValueError('Unknown wheel; arbitrary URLs and paths are unavailable')
        name, _, url = item
        # Large scientific wheels exceed 256 MiB. Spool to disk and validate
        # completely before exposing any bytes, keeping bounded memory use.
        with tempfile.TemporaryFile() as body:
            self.fetch(url, 2 * 1024 * 1024 * 1024, destination=body)
            size = body.tell()
            body.seek(0)
            hasher = hashlib.sha256()
            while chunk := body.read(1024 * 1024):
                hasher.update(chunk)
            if hasher.hexdigest() != digest:
                raise ValueError('Wheel hash mismatch')
            body.seek(0)
            with zipfile.ZipFile(body) as archive:
                self.validate_wheel(archive, name)
            body.seek(0)
            self.audit(action='wheel', package=name, filename=filename, sha256=digest, bytes=size)
            yield body, size

    def wheel(self, digest, filename):
        """Small-wheel convenience API; the HTTP handler streams open_wheel."""
        with self.open_wheel(digest, filename) as (body, _):
            return body.read()

    def validate_wheel(self, archive, name):
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > 8 * 1024 * 1024 * 1024:
            raise ValueError('Wheel expanded size exceeds limit')
        metadata_paths = [i.filename for i in infos
                          if len(PurePosixPath(i.filename).parts) == 2
                          and i.filename.endswith('.dist-info/METADATA')]
        if len(metadata_paths) != 1:
            raise ValueError('Wheel must have exactly one root METADATA file')
        if archive.getinfo(metadata_paths[0]).file_size > 16 * 1024 * 1024:
            raise ValueError('Wheel metadata exceeds size limit')
        meta = BytesParser().parsebytes(archive.read(metadata_paths[0]))
        if normalized(meta['Name'] or '') != name or name in self.targets:
            raise ValueError('Wheel package identity mismatch or forbidden target')
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in info.filename:
                raise ValueError('Unsafe wheel member path')
            # Include vendored modules and bytecode. This is an additional
            # content check, not proof against renamed/obfuscated copies.
            parts = path.parts
            candidates = {parts[0].split('.')[0]} if parts else set()
            for index, part in enumerate(parts[:-1]):
                if part in ('vendor', '_vendor', 'vendored', 'purelib', 'platlib'):
                    candidates.add(parts[index + 1].split('.')[0])
            if self.modules & candidates:
                raise ValueError('Wheel contains a target module, including vendored content')
        for requirement in meta.get_all('Requires-Dist', []):
            if '@' in requirement or '://' in requirement:
                raise ValueError('Direct-URL dependencies are unavailable')


@contextmanager
def dependency_channel(policy, audit_path):
    broker = WheelBroker(policy, audit_path)
    token = secrets.token_hex(24)
    prefix = '/' + token + '/'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if not self.path.startswith(prefix):
                self.send_error(403)
                return
            path = self.path[len(prefix):]
            try:
                if match := re.fullmatch(r'simple/([a-zA-Z0-9_.-]+)/', path):
                    body = broker.index(match[1].lower())
                    content_type = 'text/html; charset=utf-8'
                elif match := re.fullmatch(r'wheel/([0-9a-f]{64})/([^/?]+)', path):
                    with broker.open_wheel(match[1], unquote(match[2])) as (body, size):
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(size))
                        self.end_headers()
                        shutil.copyfileobj(body, self.wfile, length=1024 * 1024)
                    return
                else:
                    raise ValueError('Only package indexes and approved wheels are available')
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError) as exc:
                broker.audit(action='client_disconnect', path=path[:300], reason=type(exc).__name__)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                broker.audit(action='upstream_error', path=path[:300], reason=type(exc).__name__, detail=str(exc)[:300])
                self.send_error(502, 'Dependency upstream unavailable; see benchmark dependency audit')
            except Exception as exc:
                broker.audit(action='deny', path=path[:300], reason=type(exc).__name__, detail=str(exc)[:300])
                self.send_error(403, 'Dependency request rejected; see benchmark dependency audit')

    with tempfile.TemporaryDirectory(prefix='nl2repo-packages-') as directory:
        os.chmod(directory, 0o755)
        with Server(str(Path(directory) / 'api.sock'), Handler) as server:
            os.chmod(Path(directory) / 'api.sock', 0o666)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
            thread.start()
            try:
                yield directory, dict(PIP_INDEX_URL='http://127.0.0.1:18081' + prefix + 'simple/',
                                      PIP_NO_INDEX='0', PIP_EXTRA_INDEX_URL='',
                                      PIP_DEFAULT_TIMEOUT='600', PIP_RETRIES='2',
                                      PIP_TRUSTED_HOST='127.0.0.1', PIP_DISABLE_PIP_VERSION_CHECK='1')
            finally:
                server.shutdown()
                thread.join()
