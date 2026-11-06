"""Run in an isolated installed Core: python -m tests.native.test_backup_agents_contract.

Core's real BackupManager calls in-memory provider and reader/writer adapters.
This qualifies native routing, upload/readback and download handoff, not a cloud
service, encryption, archive validity, Supervisor, or a completed restore.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any


async def memory_chunks(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def memory_agent_type() -> type[Any]:
    from homeassistant.components.backup.agent import BackupAgent
    from homeassistant.components.backup.models import BackupNotFound

    class MemoryAgent(BackupAgent):
        domain = "qualification"
        name = "Isolated memory provider"

        def __init__(self, unique_id: str) -> None:
            self.unique_id = unique_id
            self.rows: dict[str, Any] = {}
            self.streams: dict[str, bytes] = {}
            self.uploads = self.downloads = 0
            self.fail = False

        async def async_upload_backup(
            self, *, open_stream: Any, backup: Any, on_progress: Any, **kwargs: Any
        ) -> None:
            self.uploads += 1
            if self.fail:
                raise RuntimeError("Isolated provider rejected upload")
            self.streams[backup.backup_id] = b"".join(
                [chunk async for chunk in await open_stream()]
            )
            self.rows[backup.backup_id] = backup
            on_progress(bytes_uploaded=len(self.streams[backup.backup_id]))

        async def async_download_backup(self, backup_id: str, **kwargs: Any) -> Any:
            self.downloads += 1

            async def stream() -> Any:
                yield self.streams[backup_id]

            return stream()

        async def async_get_backup(self, backup_id: str, **kwargs: Any) -> Any:
            if backup_id not in self.rows:
                raise BackupNotFound
            return self.rows[backup_id]

        async def async_list_backups(self, **kwargs: Any) -> list[Any]:
            return list(self.rows.values())

        async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
            raise AssertionError("This qualification never deletes archives")

    return MemoryAgent


def memory_writer_type() -> type[Any]:
    from homeassistant.components.backup.manager import (
        BackupReaderWriter,
        NewBackup,
        WrittenBackup,
    )
    from homeassistant.components.backup.models import AgentBackup
    from homeassistant.const import __version__ as core_version

    class MemoryWriter(BackupReaderWriter):
        def __init__(self) -> None:
            self.creates = 0
            self.restores: list[dict[str, Any]] = []

        async def async_create_backup(self, **kwargs: Any) -> Any:
            self.creates += 1
            identifier = f"isolated-{self.creates}"
            payload = b"isolated synthetic archive bytes"
            backup = AgentBackup(
                addons=[],
                backup_id=identifier,
                date="2026-10-01T00:00:00+00:00",
                database_included=kwargs["include_database"],
                extra_metadata=kwargs["extra_metadata"],
                folders=[],
                homeassistant_included=True,
                homeassistant_version=core_version,
                name=kwargs["backup_name"],
                protected=True,
                size=len(payload),
            )

            async def stream() -> Any:
                return memory_chunks(payload)

            async def release() -> None:
                pass

            async def finish() -> Any:
                return WrittenBackup(
                    backup=backup,
                    open_stream=stream,
                    release_stream=release,
                    addon_errors={},
                    folder_errors={},
                )

            return NewBackup(backup_job_id=identifier), asyncio.create_task(finish())

        async def async_restore_backup(self, backup_id: str, **kwargs: Any) -> None:
            downloaded = b"".join(
                [chunk async for chunk in await kwargs["open_stream"]()]
            )
            self.restores.append(
                {
                    "backup_id": backup_id,
                    "bytes": downloaded,
                    "agent_id": kwargs["agent_id"],
                }
            )

        async def async_receive_backup(self, **kwargs: Any) -> Any:
            raise AssertionError("This reader/writer does not receive real archives")

        async def async_resume_restore_progress_after_restart(
            self, **kwargs: Any
        ) -> None:
            pass

        async def async_validate_config(self, **kwargs: Any) -> None:
            pass

    return MemoryWriter


async def run_contract(config_dir: Path) -> dict[str, Any]:
    from homeassistant.components.backup.const import DATA_MANAGER
    from homeassistant.components.backup.manager import BackupManager, IdleEvent
    from homeassistant.core import HomeAssistant

    await asyncio.to_thread(config_dir.mkdir, parents=True, exist_ok=True)
    MemoryAgent, MemoryWriter = memory_agent_type(), memory_writer_type()
    hass = HomeAssistant(str(config_dir))
    hass.config.components.add("backup")
    writer = MemoryWriter()
    owner = BackupManager(hass, writer)
    hass.data[DATA_MANAGER] = owner
    first, second = MemoryAgent("first"), MemoryAgent("second")
    owner.backup_agents = {agent.agent_id: agent for agent in (first, second)}
    config = owner.config.data.create_backup
    config.agent_ids = list(owner.backup_agents)
    config.password = "isolated-test-protection"
    config.name = "Configured isolated archive"
    config.include_database = False
    owner.async_on_backup_event(IdleEvent())

    async def authorize(_actor: Any) -> None:
        pass

    async def visibility(*args: Any, **kwargs: Any) -> None:
        pass

    scheduled: list[Any] = []

    def schedule(operation: str, actor: Any, *, restore_target: Any) -> None:
        assert operation == "backup_restore"
        scheduled.append(restore_target)

    manager = SimpleNamespace(
        hass=hass,
        backend=SimpleNamespace(authorize=authorize),
        policy=SimpleNamespace(flag=lambda key: key == "enable_snapshot_actions"),
        visibility=SimpleNamespace(before=visibility),
        system_lifecycle=SimpleNamespace(schedule=schedule),
    )
    try:
        return await exercise(manager, owner, writer, first, second, scheduled)
    finally:
        await hass.async_stop(force=True)


async def exercise(
    manager: Any, owner: Any, writer: Any, first: Any, second: Any, scheduled: list[Any]
) -> dict[str, Any]:
    from homeassistant.const import __version__ as core_version

    from custom_components.ha_mcp_tools.native_operations.full_backup_restore import (
        schedule_full_restore,
    )
    from custom_components.ha_mcp_tools.native_operations.full_backups import (
        NativeFullBackups,
    )
    from ha_mcp.tool_api import CallContext, ToolFailure

    actor = CallContext("isolated-administrator")
    service = NativeFullBackups(manager, actor)
    result = await service.create(None)
    assert result["backup_id"] == result["backup_job_id"] == "isolated-1"
    assert set(result["backup"]["agents"]) == set(
        owner.config.data.create_backup.agent_ids
    )
    assert first.uploads == second.uploads == 1
    assert "isolated-test-protection" not in json.dumps(result)
    response = await schedule_full_restore(manager, actor, result["backup_id"], None)
    assert response["operation_outcome"] == "scheduled"
    assert response["restore_completed"] is False and not writer.restores
    target = scheduled.pop()
    await target.verify(manager, actor)
    await owner.async_restore_backup(target.backup_id, **target.arguments())
    assert first.downloads == 1 and second.downloads == 0
    assert writer.restores[0]["bytes"] == b"isolated synthetic archive bytes"
    second.fail = True
    try:
        await service.create(None)
    except ToolFailure as error:
        assert error.status == "outcome_unknown"
    else:
        raise AssertionError("A failed selected provider was reported successful")
    assert writer.creates == 2 and first.uploads == second.uploads == 2
    return {
        "core_version": core_version,
        "multi_agent_uploads": True,
        "remote_restore_download_handoff": True,
        "failed_upload_no_replay": True,
        "real_cloud_provider": False,
        "completed_restore": False,
    }


def test_installed_core_backup_agents_in_isolated_process() -> None:
    import pytest

    if os.environ.get("HA_MCP_REAL_CORE_BACKUP_CONTRACT") != "1":
        pytest.skip(
            "Requires isolated installed Core; set HA_MCP_REAL_CORE_BACKUP_CONTRACT=1"
        )
    subprocess.run(
        [sys.executable, "-m", "tests.native.test_backup_agents_contract"],
        check=True,
        timeout=300,
    )


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="ha-mcp-backup-contract-") as directory:
        print(json.dumps(asyncio.run(run_contract(Path(directory))), sort_keys=True))
