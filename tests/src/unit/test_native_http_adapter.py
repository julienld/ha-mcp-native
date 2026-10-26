"""Exercise real HTTP libraries in a process untouched by HA's unit-test stubs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("probe", "case"),
    [
        ("_native_http_probe", case)
        for case in (
            "streaming",
            "oversized",
            "buffered",
            "fastmcp",
            "peer_eof",
            "peer_payload",
            "eof_race",
        )
    ]
    + [
        ("_native_http_contract_probe", case)
        for case in (
            "independent_import",
            "scope_direct",
            "scope_proxy",
            "body_declared_oversized",
            "body_exact",
            "body_empty",
            "framing_head",
            "framing_no_content",
            "framing_error",
            "disconnect_before_receive",
            "disconnect_after_receive",
            "eof_receive_and_late_send",
            "cancellation",
            "incomplete_response",
            "write_backpressure",
            "buffered_cleanup",
        )
    ]
    + [
        ("_native_http_wire_probe", case)
        for case in (
            "head_wire",
            "no_content_wire",
            "not_modified_wire",
            "fastmcp_head_wire",
        )
    ],
)
def test_native_http_adapter(probe: str, case: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio,sys; from importlib import import_module; "
            "asyncio.run(import_module('tests.src.unit.' + sys.argv[1]).run(sys.argv[2]))",
            probe,
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


def test_native_approval_wire_binding(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from tests.src.unit._native_approval_probe import run; asyncio.run(run())",
        ],
        cwd=root,
        env={**os.environ, "HA_MCP_CONFIG_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
