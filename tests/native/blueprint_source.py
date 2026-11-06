"""A disposable YAML origin on the inspected Docker gateway, outside Core."""

import ipaddress
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PATH = "/native-blueprint.yaml"
_YAML = b"""blueprint:
  name: Native imported blueprint
  domain: automation
  input: {}
triggers:
  - trigger: event
    event_type: native_import_never_fired
actions:
  - event: native_import_fixture_action
"""


class _Source(BaseHTTPRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10)

    def do_GET(self) -> None:
        if self.path != _PATH:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(_YAML)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(_YAML)

    def log_message(self, _format: str, *args: object) -> None:
        return None


@contextmanager
def blueprint_source(gateway: str) -> Iterator[str]:
    """The runner supplies Docker's observed bridge address; never bind all NICs."""
    address = ipaddress.ip_address(gateway)
    if (
        address.version != 4
        or not address.is_private
        or address.is_loopback
        or address.is_unspecified
    ):
        raise ValueError("Expected the inspected private IPv4 Docker bridge gateway")
    server = ThreadingHTTPServer((str(address), 0), _Source)
    server.daemon_threads = False
    server.block_on_close = True
    worker = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.05},
        name="blueprint-test-origin",
    )
    try:
        worker.start()
        yield f"http://{address}:{server.server_port}{_PATH}"
    finally:
        if worker.is_alive():
            server.shutdown()
            worker.join()
        # Join every active request, including a disconnected Core importer.
        server.server_close()
