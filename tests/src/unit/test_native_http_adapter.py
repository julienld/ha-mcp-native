"""Exercise real HTTP libraries in a process untouched by HA's unit-test stubs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["streaming", "oversized", "buffered", "fastmcp"])
def test_native_http_adapter(case: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.src.unit._native_http_probe import run; asyncio.run(run(sys.argv[1]))",
            case,
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
