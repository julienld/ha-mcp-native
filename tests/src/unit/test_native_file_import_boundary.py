"""File/YAML source and native bindings must not activate the legacy client."""

import subprocess
import sys
from pathlib import Path


def test_file_yaml_cold_imports_use_no_legacy_helpers_or_transport():
    code = """
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class RejectLegacyFileImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        denied = ('ha_mcp.client', 'ha_mcp.tools.helpers', 'ha_mcp.tools.component_api')
        if any(fullname == name or fullname.startswith(name + '.') for name in denied):
            raise AssertionError('File/YAML loaded legacy authority: ' + fullname)
sys.meta_path.insert(0, RejectLegacyFileImports())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_filesystem_tools import filesystem_definitions
from custom_components.ha_mcp_tools.native_yaml_tools import yaml_definitions
from custom_components.ha_mcp_tools.native_operations.file_restore import NativeFileRestore
from custom_components.ha_mcp_tools.legacy_backup_files import read_legacy_backup
from ha_mcp.runtime.yaml_lookup import YamlLookup
assert filesystem_definitions and yaml_definitions and NativeFileRestore and YamlLookup and read_legacy_backup
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
