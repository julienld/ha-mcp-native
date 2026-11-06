"""Scheduling, redaction and receipt guards; no live-runtime claims."""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from . import compare as bench
from .evidence import scan_log
from .protocol_fixture.provenance import tree_digest, wheel_package_digest
from .wire import Observation, WireError


async def test_warmups_are_excluded_and_pair_order_alternates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = []

    async def block(target: Any, width: int, index: int) -> dict[str, Any]:
        calls.append((target, width, index))
        return {"wall_ms": 2, "requests": [{"duration_ms": 1, "valid": True}] * width}

    monkeypatch.setattr(bench, "sample_block", block)
    report: dict[str, Any] = {"blocks": []}
    await bench.measurements(
        {"baseline": "A", "candidate": "B"}, 4, 1, report, tmp_path / "report.json"
    )
    assert len(calls) == 20  # Two warmup blocks plus eight measured blocks per width.
    assert len(report["blocks"]) == 16
    for width in (1, 4):
        selected = [b for b in report["blocks"] if b["concurrency"] == width]
        assert [b["role"] for b in selected] == [
            "baseline",
            "candidate",
            "candidate",
            "baseline",
        ] * 2
        assert [b["block"] for b in selected] == [0, 0, 1, 1, 2, 2, 3, 3]
    assert bench.summaries(report["blocks"])["4"]["candidate"]["requests"] == 16


async def test_four_request_blocks_overlap_and_send_same_read_selection_to_each_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: dict[str, list[int]] = {"A": [], "B": []}
    active = 0
    peak = 0
    barrier = asyncio.Event()

    async def read(target: Any, index: int) -> dict[str, Any]:
        nonlocal active, peak
        selected[target].append(index)
        active += 1
        peak = max(peak, active)
        if active == 4:
            barrier.set()
        async with asyncio.timeout(2):
            await barrier.wait()
        active -= 1
        return {"valid": True, "duration_ms": 1}

    monkeypatch.setattr(bench, "read_once", read)
    await bench.sample_block("A", 4, 1)
    barrier.clear()
    await bench.sample_block("B", 4, 1)
    assert peak == 4 and active == 0
    assert len(selected["A"]) == 4 and selected["A"] == selected["B"]


def test_log_window_counts_transport_failures_without_exporting_secrets(
    tmp_path: Path,
) -> None:
    log = tmp_path / "server.log"
    prefix = b"Traceback (most recent call last)\n"
    log.write_bytes(
        prefix
        + b"ASGI exception https://secret.invalid/private_token\nClosedResourceError\n"
    )
    evidence = scan_log(log, len(prefix))
    assert evidence["counts"]["asgi_failure"] == 1
    assert evidence["counts"]["connection_failure"] == 1
    assert evidence["counts"]["traceback"] == 0
    assert "secret" not in json.dumps(evidence)


def test_artifact_digest_detects_real_source_change_but_ignores_bytecode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "component.py"
    source.write_text("one")
    before = tree_digest(tmp_path)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "component.pyc").write_bytes(b"generated")
    assert tree_digest(tmp_path) == before
    source.write_text("two")
    assert tree_digest(tmp_path) != before


def test_public_observation_does_not_export_rpc_error_text_or_payload() -> None:
    observation = Observation(
        400,
        "application/json",
        1,
        10,
        {"error": {"code": -32022, "message": "secret-token", "data": "private-url"}},
    )
    assert observation.public()["rpc_error_code"] == -32022
    assert "secret-token" not in json.dumps(observation.public())
    assert "private-url" not in json.dumps(observation.public())


def test_imported_package_tree_must_match_the_paired_wheel_bytes(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "paired.whl"
    package = tmp_path / "ha_mcp"
    package.mkdir()
    (package / "__init__.py").write_bytes(b"original")
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ha_mcp/__init__.py", b"original")
        archive.writestr("ha_mcp-1.dist-info/METADATA", b"metadata")
    expected = wheel_package_digest(wheel)
    assert expected == tree_digest(package)
    (package / "__init__.py").write_bytes(b"stale installed build")
    assert expected != tree_digest(package)


async def test_receipt_endpoint_cannot_attest_a_different_mcp_instance() -> None:
    async with httpx.AsyncClient(trust_env=False) as http:
        with pytest.raises(WireError, match="mcp_and_fixture_must_share_core_origin"):
            bench.Target(
                http,
                {
                    "fixture_url": "http://127.0.0.1:8123/api/protocol_fixture",
                    "mcp_url": "http://127.0.0.1:8124/mcp",
                },
            )
