"""Collecting OAuth tests must not replace dependencies borrowed by native apps."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "oauth_module",
    ["test_ha_account_oauth_versions.py", "test_oauth_legacy_component.py"],
)
def test_oauth_collection_preserves_native_ingress_url_behavior(
    oauth_module: str,
) -> None:
    # A fresh interpreter catches collection-time module replacement even when
    # this worker has already imported the real dependency in another test.
    environment = dict(os.environ)
    environment.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"src/unit/{oauth_module}",
            "src/unit/test_native_app_binding.py",
            "-k",
            "test_ingress_uses_actual_owner_url_and_actor_session_without_debug_credentials",
            "-q",
            "--tb=short",
            "--timeout-method=thread",
            "-o",
            "addopts=",
            "-o",
            "log_cli=false",
            "-p",
            "no:cacheprovider",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
