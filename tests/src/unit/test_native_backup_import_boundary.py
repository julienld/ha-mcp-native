"""Backup facade and native owners load without standalone clients or handlers."""

import subprocess
import sys
from pathlib import Path


def test_native_backup_imports_do_not_load_remote_clients_or_tool_helpers() -> None:
    code = """
import importlib.abc,json,sys
from pathlib import Path
from types import ModuleType,SimpleNamespace
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('ha_mcp.client','ha_mcp.tools.helpers','ha_mcp.tools.component_api')):
            raise AssertionError('Native backup crossed import boundary: '+fullname)
sys.meta_path.insert(0,Boundary())
from tests.src.unit._embedded_stubs import install
install()
package=ModuleType('custom_components.ha_mcp_tools')
package.__path__=[str(Path.cwd()/'custom_components'/'ha_mcp_tools')]
sys.modules[package.__name__]=package
sys.modules['homeassistant.helpers.json']=SimpleNamespace(json_bytes=lambda value:json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_backup_tools import backup_definition
from ha_mcp.runtime.backup_tools import BackupFacade
from ha_mcp.runtime.backup_controls import gate_backup_combo
assert backup_definition and BackupFacade
gate_backup_combo('edits','view')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
