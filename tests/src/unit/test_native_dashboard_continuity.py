"""Native dashboard replies retain the existing public describe/edit contracts."""

import copy

from ha_mcp.tool_api import CallContext
from ha_mcp.utils.config_hash import compute_config_hash

from .test_native_dashboard_effects import describe_runtime as describe_runtime
from .test_native_dashboard_storage import dashboards as dashboards
from .test_native_dashboards import service as service
from .test_native_engine_backend import native as native
from .test_native_resource_service import resources as resources


async def test_empty_card_type_lists_the_same_card_catalog_as_omitting_it(
    describe_runtime,
):
    engine, _owner, producer = describe_runtime
    producer.return_value = {
        "success": True,
        "card_types": [{"type": "tile", "future": False}],
        "custom_status": {"available": True},
    }
    actor = CallContext("owner")
    listed = await engine.call("ha_config_get_dashboard", {"describe": True}, actor)
    blank = await engine.call(
        "ha_config_get_dashboard", {"describe": True, "card_type": ""}, actor
    )
    assert not blank.is_error and not listed.is_error
    assert blank.data == listed.data
    assert blank.data["card_types"] == [{"type": "tile", "future": False}]


async def test_patch_with_metadata_is_refused_without_a_capture_or_write(service):
    manager, engine, owner, collection, _ = service
    before = copy.deepcopy(owner._data["config"])
    metadata = copy.deepcopy(collection.data)
    result = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "patch": [{"op": "replace", "path": "/views", "value": []}],
            "config_hash": compute_config_hash(before),
            "show_in_sidebar": False,
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "not_started"
    assert result.data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert result.data["write_committed"] is False
    assert result.data["action"] == "patch" and result.data["url_path"] == "my-dash"
    assert result.data["error"]["suggestion"]
    assert collection.data == metadata and owner._data["config"] == before
    assert not owner.calls
    manager.snapshots.dashboard_backup.assert_not_awaited()


async def test_metadata_survives_a_stale_config_without_claiming_the_body_was_saved(
    service,
):
    manager, engine, owner, collection, _ = service
    before = copy.deepcopy(owner._data["config"])
    result = await engine.call(
        "ha_config_set_dashboard",
        {
            "url_path": "my-dash",
            "config": {"views": [{"title": "Rejected"}]},
            "title": "Accepted metadata",
            "config_hash": "stale",
        },
        CallContext("owner"),
    )
    assert result.is_error and result.status == "confirmed", result.data
    assert result.data["metadata_updated"] is True
    assert result.data["dashboard_created"] is False
    assert result.data["write_committed"] is True
    assert result.data["config_write_committed"] is False
    assert "conflict" in result.data["error"]["message"].lower()
    assert collection.data["my_dash"]["title"] == "Accepted metadata"
    assert owner._data["config"] == before and not owner.calls
    manager.snapshots.dashboard_backup.assert_awaited_once()
    captured = manager.snapshots.dashboard_backup.await_args.args[0]["current_config"]
    assert captured["dashboard"]["title"] == "Before"
    assert captured["config"] == before


async def test_metadata_commit_does_not_hide_an_uncertain_configuration_save(
    service, monkeypatch
):
    manager, engine, owner, collection, _ = service
    save = owner.async_save

    async def mutate_then_raise(config):
        await save(config)
        raise RuntimeError("Persistence failed after changing the live config")

    monkeypatch.setattr(owner, "async_save", mutate_then_raise)
    candidate = {"views": [{"title": "Attempted"}]}
    result = await engine.call(
        "ha_config_set_dashboard",
        {"url_path": "my-dash", "config": candidate, "title": "Accepted metadata"},
        CallContext("owner"),
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert result.data["error"]["code"] == "OUTCOME_UNKNOWN"
    assert result.data["write_committed"] is True
    assert result.data["config_applied"] is None
    assert result.data.get("config_write_committed") is not False
    assert owner.calls == [candidate]
    assert collection.data["my_dash"]["title"] == "Accepted metadata"
    manager.snapshots.dashboard_backup.assert_awaited_once()
