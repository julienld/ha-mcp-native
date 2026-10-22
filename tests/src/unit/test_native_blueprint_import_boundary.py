"""Cold native imports reuse pure contracts without starting a remote client."""

import subprocess
import sys
from pathlib import Path


def test_native_bindings_and_restore_are_independent_of_remote_client_imports():
    program = """
import importlib.abc
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
class RejectRemote(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ha_mcp.client' or fullname.startswith('ha_mcp.client.'):
            raise AssertionError('Native binding import reached ' + fullname)
sys.meta_path.insert(0, RejectRemote())
from tests.src.unit._embedded_stubs import install
install()
package = ModuleType('custom_components.ha_mcp_tools')
package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
sys.modules[package.__name__] = package
sys.modules['homeassistant.helpers.json'] = SimpleNamespace(json_bytes=lambda value: json.dumps(value).encode())
from ha_mcp.runtime.blueprint_tools import BlueprintTools
from ha_mcp.runtime.blueprint_values import parse_blueprint_body
from custom_components.ha_mcp_tools.native_blueprint_tools import blueprint_definition
from custom_components.ha_mcp_tools.native_operations.blueprint_restore import NativeBlueprintRestore
from ha_mcp.runtime.pipeline_tools import PipelineTools
from ha_mcp.runtime.pipeline_values import validate_sentence
from custom_components.ha_mcp_tools.native_pipeline_tools import pipeline_definition
assert BlueprintTools and blueprint_definition and NativeBlueprintRestore
assert PipelineTools and pipeline_definition and validate_sentence
assert parse_blueprint_body('value: !input selected') == {'value': {'__input__': 'selected'}}
"""
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
