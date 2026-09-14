"""Run a command alongside the container-local dependency socket relay."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import signal
import socket
import subprocess
import sys
import threading


class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(600)
        self.sock.connect('/run/nl2repo-packages/api.sock')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        upstream = Connection('localhost', timeout=600)
        try:
            upstream.request('GET', self.path)
            response = upstream.getresponse()
            self.send_response(response.status)
            for key in ('Content-Type', 'Content-Length'):
                if response.getheader(key):
                    self.send_header(key, response.getheader(key))
            self.end_headers()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        finally:
            upstream.close()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 18081), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    child = subprocess.Popen(sys.argv[1:])
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda signum, frame: child.send_signal(signum))
    try:
        raise SystemExit(child.wait())
    finally:
        server.shutdown()
        server.server_close()
