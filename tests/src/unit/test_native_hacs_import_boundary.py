"""Cold HACS bindings cannot import a remote client or the optional integration."""

import subprocess
import sys
from pathlib import Path


def test_hacs_binding_and_read_shapes_have_no_remote_or_optional_import():
    program = """
import importlib
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('ha_mcp.client', 'custom_components.hacs', 'aiogithubapi')):
            raise AssertionError('HACS native import crossed boundary: ' + fullname)
sys.meta_path.insert(0, Boundary())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_hacs_tools import hacs_definitions
from ha_mcp.runtime.hacs_tools import HacsFacade
from ha_mcp.runtime.hacs_values import _filter_and_score_repos
assert hacs_definitions and HacsFacade
assert _filter_and_score_repos([], '', False) == []
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
