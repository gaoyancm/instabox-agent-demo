"""Loopback-only, repository-owned API fixture with deterministic conditional GETs."""
import hashlib
import http.server
import json


class FixtureHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass  # No URL/query/header logging, including from browser requests.

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.server.request_counts[path] = self.server.request_counts.get(path, 0) + 1
        if path == "/rate-limited.json" and self.server.request_counts[path] == 1:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        if path in {"/etag.json", "/changing.json", "/rate-limited.json"}:
            data = {"name": "Alpha", "value": 42,
                    "revision": self.server.fixture_revision if path == "/changing.json" else 1}
            payload = json.dumps(data, sort_keys=True).encode()
            etag = '"' + hashlib.sha256(payload).hexdigest() + '"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", "Thu, 12 Nov 2026 00:00:00 GMT")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        return super().do_GET()


def fixture_server(directory, port=8765):
    import functools
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port),
        functools.partial(FixtureHandler, directory=str(directory)))
    server.request_counts = {}
    server.fixture_revision = 1
    return server
