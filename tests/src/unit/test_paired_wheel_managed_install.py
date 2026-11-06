"""Exact manifest wheel receipts preserve a loaded native generation on rollback."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_paired_wheel import _DIGEST, _SPEC, _URL


@pytest.fixture
def managed(tmp_path, monkeypatch):
    from ._embedded_stubs import install

    install()
    from custom_components.ha_mcp_tools import paired_wheel

    (tmp_path / "installed" / "ha_mcp").mkdir(parents=True)
    monkeypatch.setattr(paired_wheel.wheel_payload, "sys", SimpleNamespace(modules={}))

    from .test_embedded_server import DATA_LAST_PIP_SPEC, _manager, _PkgEnv, es

    manager, hass, entry = _manager(tmp_path, data={DATA_LAST_PIP_SPEC: _SPEC})
    manager._pip_spec = manager._paired_spec = _SPEC
    env = _PkgEnv(monkeypatch, {"ha-mcp": "9.0.0.dev1"})
    monkeypatch.setattr(es, "native_bound_version", lambda: "9.0.0.dev1")
    receipt = {"url": _URL, "archive_info": {"hashes": {"sha256": _DIGEST}}}
    threads = []

    def distribution(_name):
        threads.append(threading.get_ident())
        return SimpleNamespace(
            version="9.0.0.dev1",
            metadata={"Name": "ha-mcp"},
            read_text=lambda _filename: json.dumps(receipt),
            locate_file=lambda member: tmp_path / "installed" / member,
        )

    monkeypatch.setattr(paired_wheel.importlib.metadata, "distribution", distribution)

    async def executor(fn, *args):
        return await asyncio.to_thread(fn, *args)

    hass.async_add_executor_job = AsyncMock(side_effect=executor)
    return manager, env, entry, receipt, threads


async def test_exact_hashed_manifest_wheel_does_not_reinstall_on_native_to_legacy_switch(
    managed,
):
    manager, env, _entry, _receipt, threads = managed
    version = await manager._async_ensure_managed_package()
    assert version == "9.0.0.dev1" and env.calls == []
    assert threads and all(thread != threading.get_ident() for thread in threads)


@pytest.mark.parametrize(
    "reason",
    [
        "override",
        "explicit_reinstall",
        "wrong_receipt",
        "conflicting_distribution",
        "changed_native_generation",
    ],
)
async def test_requested_or_unproven_package_change_still_requires_Core_restart(
    managed, reason, monkeypatch
):
    from .test_embedded_server import DATA_REINSTALL_REQUESTED, es

    manager, env, entry, receipt, _threads = managed
    if reason == "override":
        manager._pip_spec_override = _SPEC
    elif reason == "explicit_reinstall":
        entry.data[DATA_REINSTALL_REQUESTED] = True
    elif reason == "wrong_receipt":
        receipt["archive_info"]["hashes"]["sha256"] = "0" * 64
    elif reason == "conflicting_distribution":
        env.dists["ha-mcp-dev"] = "9.0.0.dev1"
    else:
        monkeypatch.setattr(es, "native_bound_version", lambda: "9.0.0.dev2")
    with pytest.raises(es.EmbeddedServerError, match="restart Home Assistant"):
        await manager._async_ensure_managed_package()
    assert env.calls == []
