"""The administrator snapshot panel uses the same files and pin locks as tools."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.native_server_policy import NativePolicy  # noqa: E402
from custom_components.ha_mcp_tools.native_settings import NativeSettings  # noqa: E402
from ha_mcp.runtime.snapshot_store import SnapshotStore  # noqa: E402


def runtime(tmp_path):
    async def executor(function, *args):
        return function(*args)

    store = SnapshotStore(tmp_path / "backups")
    manager = SimpleNamespace(
        hass=SimpleNamespace(
            config=SimpleNamespace(path=lambda _subdir: str(tmp_path)),
            async_add_executor_job=executor,
        ),
        snapshots=SimpleNamespace(_store=lambda: store),
        policy=NativePolicy(),
        is_running=True,
        refresh_policy=AsyncMock(),
    )
    return NativeSettings(manager), store


def request(method="GET", **query):
    return SimpleNamespace(method=method, query=query)


async def test_panel_lists_and_reads_the_snapshot_written_by_tools(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture(
        "helper_input_boolean",
        "lamp",
        {"id": "lamp", "name": "Before edit"},
        "ha_config_set_helper",
    )
    listed = await ui.handle(request(), "api/settings/backups")
    assert listed.json_body["backups"][0]["name"] == snapshot.name
    viewed = await ui.handle(request(), f"api/settings/backups/{snapshot.name}")
    assert viewed.json_body["data"]["config"]["name"] == "Before edit"


async def test_panel_cannot_delete_a_snapshot_held_by_capture_or_restore(
    tmp_path,
) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture("helper_input_boolean", "lamp", {"id": "lamp"}, "test")
    store._protect_snapshot(snapshot.name)
    blocked = await ui.handle(
        request("DELETE"), f"api/settings/backups/{snapshot.name}"
    )
    assert blocked.status == 409 and snapshot.exists()
    store._unprotect_snapshot(snapshot.name)
    deleted = await ui.handle(
        request("DELETE"), f"api/settings/backups/{snapshot.name}"
    )
    assert deleted.json_body["deleted"] == [snapshot.name]
    assert not snapshot.exists()


async def test_bulk_delete_requires_and_honors_an_explicit_filter(tmp_path) -> None:
    ui, store = runtime(tmp_path)
    first = store.capture("helper_input_boolean", "first", {"id": "first"}, "test")
    second = store.capture("helper_input_boolean", "second", {"id": "second"}, "test")
    blocked = await ui.handle(request("DELETE"), "api/settings/backups")
    assert blocked.status == 400 and first.exists() and second.exists()
    deleted = await ui.handle(
        request("DELETE", entity_id="first"), "api/settings/backups"
    )
    assert deleted.json_body["deleted"] == [first.name]
    assert not first.exists() and second.exists()


async def test_unported_restore_reports_failure_without_modifying_snapshot(
    tmp_path,
) -> None:
    ui, store = runtime(tmp_path)
    snapshot = store.capture("helper_input_boolean", "lamp", {"id": "lamp"}, "test")
    original = snapshot.read_bytes()
    response = await ui.handle(
        request("POST"), f"api/settings/backups/{snapshot.name}/restore"
    )
    assert response.status == 501
    assert snapshot.read_bytes() == original
