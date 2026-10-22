"""App transport cannot replay an opaque GET or WebSocket handshake."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case", ["http_drop", "http_redirect", "ws_drop", "ws_redirect", "ws_command"]
)
def test_app_transport_real_server_sends_only_one_request(
    case: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from tests.src.unit._native_app_transport_probe import run; asyncio.run(run(sys.argv[1]))",
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
