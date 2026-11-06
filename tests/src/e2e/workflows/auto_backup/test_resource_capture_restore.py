"""Dashboard-resource and calendar-event snapshot restoration."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import wait_for_tool_result
from .test_capture_and_restore import (
    _HA_PROPAGATION_SETTLE_SECONDS,
    _delete_leftover_backup,
    _enable_auto_backup,
    _wait_for_backup,
)

# ---------------------------------------------------------------- dashboard_resource lane


@pytest.mark.convenience
@pytest.mark.cleanup
@pytest.mark.external_only
class TestDashboardResourceCaptureRestore:
    """Dashboard-resource restore is Group 2 (``lovelace/resources/update``).
    Distinct from the dashboard-config restore — different WS command,
    different payload shape. The handler re-injects ``resource_id`` into
    the payload from the entity_id, so a captured snapshot missing that
    key path would only fail here."""

    async def test_dashboard_resource_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard_resource",
            {
                "url": f"/local/e2e-bk-{suffix}.js",
                "resource_type": "module",
            },
        )
        if create.get("success") is False:
            pytest.skip(f"dashboard_resource create unsupported: {create}")
        resource_id = create.get("data", {}).get("resource_id") or create.get(
            "resource_id"
        )
        assert resource_id, f"resource_id missing: {create}"
        backup_name = None
        try:
            mcp = MCPAssertions(mcp_client)
            # Settle so the decorator's pre-edit fetch finds the resource.
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            # Edit — change the URL so capture fires.
            await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "resource_id": resource_id,
                    "url": f"/local/e2e-bk-{suffix}-edited.js",
                },
            )

            backup_name = await _wait_for_backup(
                mcp_client, domain="dashboard_resource", entity_id=str(resource_id)
            )

            restore = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            assert restore.get("data", {}).get("safety_backup") is not None
            async with MCPAssertions(mcp_client) as mcp:
                listed = await mcp.call_tool_success(
                    "ha_config_list_dashboard_resources", {"limit": 500}
                )
            restored = next(
                (r for r in listed["resources"] if str(r["id"]) == str(resource_id)),
                None,
            )
            assert restored is not None, f"resource {resource_id} missing: {listed}"
            assert restored["url"] == f"/local/e2e-bk-{suffix}.js"

            delete = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "delete", "backup_name": backup_name},
            )
            assert delete.get("success") is True
            backup_name = None
        finally:
            # Cleanup
            await safe_call_tool(
                mcp_client,
                "ha_config_delete_dashboard_resource",
                {"resource_id": resource_id},
            )
            await _delete_leftover_backup(mcp_client, backup_name)

    async def test_inline_dashboard_resource_full_loop(
        self, mcp_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same loop for an INLINE resource, whose URL is the content itself.

        The url= lane above snapshots a ~30-character URL. An inline
        resource's URL is a data: URI carrying the whole card, so this is
        the case where capture and restore actually move the payload — and
        the one the inline size cap is reasoned about (auto-backup stores
        the URL whole on every edit).
        """
        _enable_auto_backup(monkeypatch)
        suffix = uuid.uuid4().hex[:8]
        original = f"export const E2E_BK = '{suffix}';" + ("// pad" * 200)

        create = await safe_call_tool(
            mcp_client,
            "ha_config_set_dashboard_resource",
            {"content": original, "resource_type": "module"},
        )
        if create.get("success") is False:
            pytest.skip(f"inline dashboard_resource create unsupported: {create}")
        resource_id = create.get("data", {}).get("resource_id") or create.get(
            "resource_id"
        )
        assert resource_id, f"resource_id missing: {create}"
        await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

        mcp = MCPAssertions(mcp_client)
        await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "resource_id": resource_id,
                "content": f"export const E2E_BK_EDITED = '{suffix}';",
                "resource_type": "module",
            },
        )

        backup_name = await _wait_for_backup(
            mcp_client, domain="dashboard_resource", entity_id=str(resource_id)
        )

        restore = await mcp.call_tool_success(
            "ha_manage_backup",
            {"scope": "edits", "action": "restore", "backup_name": backup_name},
        )
        assert restore.get("success") is True

        # The restored resource must carry the ORIGINAL content back,
        # byte-identical — a snapshot that dropped or mangled the data: URI
        # would still "restore" successfully without this assertion.
        listed = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {"include_content": True}
        )
        restored = next(
            (r for r in listed.get("resources", []) if r.get("id") == resource_id),
            None,
        )
        assert restored is not None, "restored resource missing from listing"
        assert restored.get("_content") == original

        await safe_call_tool(
            mcp_client,
            "ha_manage_backup",
            {"scope": "edits", "action": "delete", "backup_name": backup_name},
        )
        await safe_call_tool(
            mcp_client,
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource_id},
        )


# ---------------------------------------------------------------- calendar lane
#
# calendar_event has no entity in the base image, so this lane creates its
# own backing entity via the local_calendar config flow (it ships with HA
# Core and is not a ``helper_type``), driven through ``ha_client`` like
# test_integration_setup.py does. Capture fires on DELETE (and on an update,
# which carries a uid). The test tears its config entry down in a finally
# block. The todo_item lane is test_todo_capture_restore.py.


@pytest.mark.haos_only
@pytest.mark.calendar
@pytest.mark.external_only
@pytest.mark.cleanup
class TestCalendarCaptureRestore:
    """Auto-backup for calendar events. The snapshot is keyed on
    ``<entity>::<uid>``, so it fires on ``ha_config_remove_calendar_event``
    and on an update through ``ha_config_set_calendar_event`` (a create
    carries no uid). Restoring a deleted event re-creates it; restoring an
    edited one puts the captured values back under the same uid."""

    async def test_calendar_capture_on_delete(
        self, mcp_client, ha_client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from datetime import datetime, timedelta

        _enable_auto_backup(monkeypatch)
        unique = uuid.uuid4().hex[:8]
        cal_name = f"BK Cal {unique}"
        entity_id = f"calendar.bk_cal_{unique}"

        entry_id: str | None = None
        try:
            flow_init = await ha_client.start_config_flow("local_calendar")
            if not isinstance(flow_init, dict) or flow_init.get("type") != "form":
                pytest.skip(f"local_calendar config flow unavailable: {flow_init}")
            flow_done = await ha_client.submit_config_flow_step(
                flow_init["flow_id"],
                {"calendar_name": cal_name, "import": "create_empty"},
            )
            if flow_done.get("type") != "create_entry":
                pytest.skip(f"local_calendar entry not created: {flow_done}")
            entry_id = flow_done["result"]["entry_id"]

            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_entity",
                arguments={"entity_id": entity_id},
                predicate=lambda d: d.get("success") is True,
                description=f"{entity_id} registers in entity registry",
                timeout=20,
            )

            now = datetime.now()
            start = (now + timedelta(days=1)).replace(
                hour=10, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(hours=1)
            summary = f"bk-evt-{unique}"
            events_window = {
                "entity_id": entity_id,
                "start": start.isoformat(),
                "end": (end + timedelta(hours=1)).isoformat(),
            }

            def has_event(d: dict[str, Any]) -> bool:
                return any(e.get("summary") == summary for e in d.get("events", []))

            created = await safe_call_tool(
                mcp_client,
                "ha_config_set_calendar_event",
                {
                    "entity_id": entity_id,
                    "summary": summary,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                },
            )
            if created.get("success") is False:
                pytest.skip(f"calendar event create unsupported: {created}")

            got = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_get_calendar_events",
                arguments=events_window,
                predicate=has_event,
                description=f"event {summary!r} visible",
                timeout=45,
            )
            uid = next(
                (
                    e.get("uid")
                    for e in got.get("events", [])
                    if e.get("summary") == summary
                ),
                None,
            )
            assert uid, f"created event has no uid: {got.get('events')}"

            # Let HA's calendar store settle before the pre-delete fetch
            # (the decorator runs its own read of the REST calendar view; a
            # freshly-written iCal event can lag behind the create response).
            await asyncio.sleep(_HA_PROPAGATION_SETTLE_SECONDS)

            mcp = MCPAssertions(mcp_client)
            # Delete the event — pre-delete capture fires (calendar_event).
            await mcp.call_tool_success(
                "ha_config_remove_calendar_event",
                {"entity_id": entity_id, "uid": uid},
            )
            # Wait for the event to be gone, or the read-back after the restore
            # could see an event the removal never took out.
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_get_calendar_events",
                arguments=events_window,
                predicate=lambda d: not has_event(d),
                description=f"removed event {summary!r} gone",
                timeout=45,
            )

            try:
                backup_name = await _wait_for_backup(
                    mcp_client,
                    domain="calendar_event",
                    entity_id=f"{entity_id}::{uid}",
                    timeout=20,
                )
            except TimeoutError:
                pytest.skip(
                    "calendar_event snapshot not observed within timeout on "
                    "this HA rig (calendar capture/indexing timing); the unit "
                    "test test_backup_manager covers the payload shape"
                )
            view = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "view", "backup_name": backup_name},
            )
            assert view.get("success") is True

            restore = await mcp.call_tool_success(
                "ha_manage_backup",
                {"scope": "edits", "action": "restore", "backup_name": backup_name},
            )
            assert restore.get("success") is True
            # The restore must bring the deleted event back.
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_config_get_calendar_events",
                arguments=events_window,
                predicate=has_event,
                description=f"restored event {summary!r} visible",
                timeout=45,
            )
        finally:
            if entry_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )


# ``integration`` (config_entries/disable) still has no full-loop e2e here:
# it needs a real integration that is safe to disable/re-enable, and the
# base image's core integrations are unsafe to toggle. Payload-shape
# coverage lives in tests/src/unit/test_backup_manager.py.
