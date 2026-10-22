"""Dashboard native reuse never imports the old HA REST/WebSocket client."""

import subprocess
import sys
from pathlib import Path


def test_native_dashboard_and_screenshot_imports_do_not_load_a_remote_client():
    code = """
import importlib.abc
import json
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path
class ForbidRemoteClient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ha_mcp.client' or fullname.startswith('ha_mcp.client.'):
            raise AssertionError('Native dashboard loaded a remote client: ' + fullname)
sys.meta_path.insert(0, ForbidRemoteClient())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules['custom_components.ha_mcp_tools'] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from ha_mcp.runtime.dashboard_tools import DashboardTools
from ha_mcp.runtime.dashboard_values import get_dashboard_result
from ha_mcp.dashboard_screenshot.capture import _normalize_dashboard_path
assert _normalize_dashboard_path('test-dashboard/main') == 'test-dashboard/main'
from custom_components.ha_mcp_tools.native_screenshots import NativeScreenshots
from custom_components.ha_mcp_tools.native_dashboard_tools import build_dashboard_tools
from custom_components.ha_mcp_tools.native_operations.dashboard_restore import NativeDashboardRestore
assert NativeScreenshots and NativeDashboardRestore and build_dashboard_tools and DashboardTools
assert get_dashboard_result('test-dashboard', {'views': []}, original_url_path=None, view_path=None)['config'] == {'views': []}
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
