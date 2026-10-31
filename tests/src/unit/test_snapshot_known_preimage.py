"""Known preimages retain the shared capture policy without another remote read."""

import asyncio
import dataclasses
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from .test_backup_manager import _mk_handler, _mk_manager


async def test_known_snapshot_freezes_before_directory_io_and_never_fetches_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _mk_manager(tmp_path)
    handler = _mk_handler(fetched={"new": "remote value"})
    fetch = AsyncMock(
        side_effect=AssertionError("A selected preimage must not be re-read")
    )
    handler = dataclasses.replace(handler, fetch=fetch)
    manager.register(handler)
    source = {"alias": "Before", "nested": {"value": False}}

    async def directory() -> None:
        source["nested"]["value"] = True

    monkeypatch.setattr(manager, "ensure_directory_ready", directory)
    path = await manager.maybe_snapshot_known(
        "automation", "one", source, tool_name="edit"
    )
    assert path is not None
    data = manager.read_snapshot(path.name)
    assert data["config"] == {"alias": "Before", "nested": {"value": False}}
    fetch.assert_not_awaited()


async def test_known_and_fetched_snapshot_share_throttle_and_force(
    tmp_path: Path,
) -> None:
    manager = _mk_manager(tmp_path, auto_backup_throttle_minutes=30)
    manager.register(_mk_handler(fetched={"x": 2}))
    first = await manager.maybe_snapshot_known("automation", "one", {"x": 1})
    assert first is not None
    assert await manager.maybe_snapshot("automation", "one") is None
    assert await manager.maybe_snapshot_known("automation", "one", {"x": 3}) is None
    forced = await manager.maybe_snapshot_known(
        "automation", "one", {"x": 4}, force=True
    )
    assert forced is not None and forced != first
    assert manager.read_snapshot(forced.name)["config"] == {"x": 4}


async def test_known_snapshot_respects_disabled_feature_and_unregistered_domains(
    tmp_path: Path,
) -> None:
    manager = _mk_manager(tmp_path, enable_auto_backup=False)
    manager.register(_mk_handler(fetched={"x": 1}))
    assert await manager.maybe_snapshot_known("automation", "one", {"x": 1}) is None
    assert (
        await manager.maybe_snapshot_known("unknown", "one", {"x": 1}, force=True)
        is None
    )
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.yaml")))
