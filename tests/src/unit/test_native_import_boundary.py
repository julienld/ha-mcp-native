"""Native settings and search reuse cannot activate a remote HA transport."""

import os
import subprocess
import sys


def test_native_settings_imports_never_load_a_remote_client():
    code = """
import importlib.abc
import sys
class ForbidRemoteClient(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ha_mcp.client' or fullname.startswith('ha_mcp.client.'):
            raise AssertionError('Native surface loaded a remote client: ' + fullname)
sys.meta_path.insert(0, ForbidRemoteClient())
import ha_mcp.settings_ui
import ha_mcp.policy.handlers
from ha_mcp.transforms import CategorizedSearchTransform, DEFAULT_PINNED_TOOLS
assert DEFAULT_PINNED_TOOLS and CategorizedSearchTransform
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_settings_introspection_does_not_load_dotenv(tmp_path):
    dotenv = tmp_path / "native.env"
    dotenv.write_text(
        "HAMCP_NATIVE_TEST_MARKER=explicit-runtime-only\n"
        "HOMEASSISTANT_URL=http://example.invalid:8123\n"
        "HOMEASSISTANT_TOKEN=fixture-placeholder\n",
        encoding="utf-8",
    )
    environment = dict(os.environ, HAMCP_ENV_FILE=str(dotenv))
    environment.pop("HAMCP_NATIVE_TEST_MARKER", None)
    code = """
import os
from ha_mcp.config_settings import Settings, get_settings
assert Settings.model_fields['enable_mandatory_bps'].default is True
assert 'HAMCP_NATIVE_TEST_MARKER' not in os.environ
get_settings()
assert os.environ['HAMCP_NATIVE_TEST_MARKER'] == 'explicit-runtime-only'
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
