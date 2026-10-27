"""App bindings cannot load legacy clients or discover Supervisor at import time."""

import subprocess
import sys
from pathlib import Path


def test_native_apps_import_without_remote_clients_or_supervisor() -> None:
    program = """
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('ha_mcp.client', 'ha_mcp.tools.helpers', 'aiohasupervisor', 'homeassistant.components.hassio')):
            raise AssertionError('Native app import crossed boundary: ' + fullname)
sys.meta_path.insert(0, Boundary())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_app_tools import app_definitions
from ha_mcp.runtime.app_tools import AppTools
from ha_mcp.runtime.app_arrays import _apply_array_ops
assert app_definitions and AppTools
assert _apply_array_ops([], [{'op': 'add', 'item': {'id': 'new'}}], 'id')[0] == [{'id': 'new'}]
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
