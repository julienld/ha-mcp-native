"""Qualify the small standard MCP proxy against a real HTTP endpoint."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["contracts", "revoked", "drop"])
def test_native_proxy_real_http(case: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.src.unit._native_proxy_probe import run; asyncio.run(run(sys.argv[1]))",
            case,
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_proxy_forwards_bound_approval_continuations(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.src.unit._native_approval_probe import run; asyncio.run(run(through_proxy=True))",
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
