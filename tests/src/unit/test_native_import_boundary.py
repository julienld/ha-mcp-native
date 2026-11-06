"""Native settings and search reuse cannot activate a remote HA transport."""

import math
import os
import subprocess
import sys
from pathlib import Path

import pytest


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
import ha_mcp.policy.events
import ha_mcp.policy.mrtr
import ha_mcp.visibility.enforcement
import ha_mcp.config_backup_validation
from ha_mcp.transforms import CategorizedSearchTransform, DEFAULT_PINNED_TOOLS
assert DEFAULT_PINNED_TOOLS and CategorizedSearchTransform
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=Path(__file__).resolve().parents[3],
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("dotenv", [False, True])
def test_native_import_probes_bound_stalled_children(monkeypatch, tmp_path, dotenv):
    calls = []

    def stalled(command, **kwargs):
        calls.append(command)
        budget = kwargs.get("timeout", float("inf"))
        assert budget > 0 and math.isfinite(budget)
        assert kwargs["cwd"] == Path(__file__).resolve().parents[3]
        raise subprocess.TimeoutExpired(command, budget)

    monkeypatch.setattr(subprocess, "run", stalled)
    with pytest.raises(subprocess.TimeoutExpired):
        if dotenv:
            test_native_settings_introspection_does_not_load_dotenv(tmp_path)
        else:
            test_native_settings_imports_never_load_a_remote_client()
    assert len(calls) == 1


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
        cwd=Path(__file__).resolve().parents[3],
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
