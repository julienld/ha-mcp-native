"""The component file owner revalidates and retains issued I/O through unload."""

import asyncio
import importlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ha_mcp.tool_api import ToolFailure

from .test_native_engine_backend import native as native


@pytest.fixture
def files(native, tmp_path):
    module = importlib.import_module("custom_components.ha_mcp_tools.file_access")
    hass = SimpleNamespace(
        config=SimpleNamespace(
            config_dir=str(tmp_path), path=lambda *parts: str(tmp_path.joinpath(*parts))
        ),
        data={
            "ha_mcp_tools": {"allowed_paths": [], "caller_token": "fixture-capability"}
        },
        services=SimpleNamespace(async_remove=MagicMock()),
        async_add_executor_job=lambda function, *args: asyncio.ensure_future(
            asyncio.to_thread(function, *args)
        ),
    )
    owner = module.FileAccess(hass)
    hass.data["ha_mcp_tools"][module.DATA_FILE_ACCESS] = owner
    (tmp_path / "www").mkdir()
    target = tmp_path / "www" / "notes.txt"
    target.write_text("before", encoding="utf-8")
    return module, owner, target


async def test_file_owner_uses_original_producers_without_a_service_or_network_call(
    files, monkeypatch
):
    module, owner, target = files
    loop_thread = threading.get_ident()
    original = module._is_path_allowed_for_read

    def checked(*args):
        assert threading.get_ident() != loop_thread
        return original(*args)

    monkeypatch.setattr(module, "_is_path_allowed_for_read", checked)
    read = await owner.read_file("www/notes.txt")
    assert read["content"] == "before"
    listed = await owner.list_files("www", "*.txt")
    assert listed["count"] == 1 and listed["files"][0]["name"] == "notes.txt"
    written = await owner.write_file("www/notes.txt", "after", overwrite=True)
    assert (
        written["success"] and not written["created"] and target.read_text() == "after"
    )
    removed = await owner.delete_file("www/notes.txt")
    assert removed["success"] and not target.exists()
    assert not owner.pending


async def test_file_revocation_after_write_preserves_the_committed_outcome(
    files, monkeypatch
):
    module, owner, target = files
    active = [True]
    original = module._write_file_sync

    async def guard(writes):
        if not active[0]:
            raise ToolFailure("AUTH_FORBIDDEN", "revoked", status="not_started")

    def write(*args, **kwargs):
        result = original(*args, **kwargs)
        active[0] = False
        return result

    monkeypatch.setattr(module, "_write_file_sync", write)
    with pytest.raises(ToolFailure) as raised:
        await owner.write_file("www/notes.txt", "after", overwrite=True, guard=guard)
    assert (
        raised.value.status == "outcome_unknown"
        and raised.value.data["write_committed"] is True
    )
    assert target.read_text() == "after"


async def test_two_cancelled_waiters_and_failed_unload_keep_file_io_owned(
    files, monkeypatch
):
    module, owner, _target = files
    component = importlib.import_module("custom_components.ha_mcp_tools")
    entered, release = threading.Event(), threading.Event()
    original = module._read_file_sync

    def blocked(path):
        entered.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(module, "_read_file_sync", blocked)
    pending = asyncio.create_task(owner.read_file("www/notes.txt"))
    assert await asyncio.to_thread(entered.wait, 2)
    owner.stop_seconds = 0.01
    try:
        pending.cancel()
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(RuntimeError, match="remains owned"):
            await component._async_unload_tools_entry(owner.hass, SimpleNamespace())
        assert owner.pending and not pending.done()
        assert owner.hass.data["ha_mcp_tools"][module.DATA_FILE_ACCESS] is owner
        assert owner.hass.data["ha_mcp_tools"]["caller_token"] == "fixture-capability"
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
    assert await component._async_unload_tools_entry(owner.hass, SimpleNamespace())
    assert module.DATA_FILE_ACCESS not in owner.hass.data["ha_mcp_tools"]
    assert not owner.pending and pending.cancelled()


@pytest.mark.parametrize("operation", ["write", "delete"])
def test_file_before_image_change_is_refused_before_sync_mutation(files, operation):
    _module, _owner, target = files
    io = importlib.import_module("custom_components.ha_mcp_tools.file_io")
    before = io.read_file_image(target)
    target.write_text("external")
    state = io.FileWriteState()
    with pytest.raises(ToolFailure) as raised:
        if operation == "write":
            io._write_file_sync(
                target,
                "requested",
                True,
                True,
                target.parent,
                expected=before,
                state=state,
            )
        else:
            io._delete_file_sync(target, expected=before, state=state)
    assert raised.value.code == "CONFIG_CONFLICT" and not state.started
    assert target.read_text() == "external"
