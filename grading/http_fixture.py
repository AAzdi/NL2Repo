"""Loopback pages for the reference HTTP fetching and pagination tests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest


@pytest.fixture(scope='module')
def fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if not self.path.startswith('/site-'):
                self.send_error(404)
                return
            site = self.path.split('/')[1]
            body = ('<!doctype html><html><head><title>Pagination fixture</title></head>'
                    '<body><p>Reference page</p><a rel="next" href="/' + site +
                    '/2">Next</a></body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def url(request, fixture_server):
    return fixture_server + request.param
