"""A cold native radio import cannot bring in the removable remote adapter."""

import subprocess
import sys
from pathlib import Path


def test_radio_native_binding_never_loads_remote_clients_or_optional_integrations():
    program = """
import importlib
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('ha_mcp.client', 'homeassistant.components.zha', 'homeassistant.components.zwave_js', 'homeassistant.components.matter', 'homeassistant.components.otbr', 'homeassistant.components.thread')):
            raise AssertionError('Cold radio binding crossed optional boundary: ' + fullname)
sys.meta_path.insert(0, Boundary())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_radio_tools import radio_definition
from ha_mcp.runtime.radio_specs import SUPPORTED
from ha_mcp.runtime.radio_tools import RadioFacade
assert radio_definition and RadioFacade and len(SUPPORTED) == 4
for owner in ('access', 'manager', 'matter', 'otbr', 'thread', 'zigbee', 'zigbee_groups', 'zwave', 'zwave_network', 'zwave_parameters'):
    importlib.import_module('custom_components.ha_mcp_tools.native_operations.radio_' + owner)
assert not any(name == 'ha_mcp.client' or name.startswith('ha_mcp.client.') for name in sys.modules)
"""
    process = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stdout + process.stderr
