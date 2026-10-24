"""Developer/report/policy bindings load no legacy client or global settings."""

import subprocess
import sys
from pathlib import Path


def test_admin_tools_cold_import_and_contracts_use_only_native_owners():
    code = r"""
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class RejectLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        denied = ('ha_mcp.client', 'ha_mcp.tools.helpers', 'ha_mcp.tools.component_api', 'ha_mcp.tools.tools_dev', 'ha_mcp.tools.tools_bug_report')
        if any(fullname == name or fullname.startswith(name + '.') for name in denied):
            raise AssertionError('Native administrator binding loaded legacy authority: ' + fullname)
sys.meta_path.insert(0, RejectLegacy())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from custom_components.ha_mcp_tools.native_dev_tools import dev_definitions
from custom_components.ha_mcp_tools.native_report_tools import report_definition
from custom_components.ha_mcp_tools.native_security_policy_tools import security_policy_definition
manager = SimpleNamespace(backend=object())
definitions = [*dev_definitions(manager), report_definition(manager), security_policy_definition(manager)]
assert len(definitions) == 4
"""
    # The frozen baseline is checked in-process elsewhere; this subprocess also
    # proves construction, not only import, stays independent from remote code.
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
