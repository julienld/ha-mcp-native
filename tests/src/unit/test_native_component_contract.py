"""Startup's version check reads metadata without loading a remote HA transport."""

import subprocess
import sys
from pathlib import Path


def test_native_version_check_never_imports_remote_tools() -> None:
    code = """
import importlib.abc
import sys
from pathlib import Path
from types import ModuleType
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
from custom_components.ha_mcp_tools.embedded_setup import _read_min_component_version
class ForbidRemote(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'ha_mcp.tools.tools_filesystem', 'ha_mcp.client'} or fullname.startswith('ha_mcp.client.'):
            raise AssertionError('Compatibility check imported a remote runtime: ' + fullname)
sys.meta_path.insert(0, ForbidRemote())
version = _read_min_component_version()
assert version and all(part.isdigit() for part in version.split('.'))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
    )
    assert result.returncode == 0, result.stdout + result.stderr
