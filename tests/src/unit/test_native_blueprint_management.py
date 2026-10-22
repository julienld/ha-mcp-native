"""Blueprint calls preserve installed images and Core's ownership through awaits."""

import asyncio
import copy
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from ha_mcp.tool_api import CallContext, ToolFailure

from .test_native_engine_backend import native as native


class BlueprintError(Exception):
    pass


class InUse(BlueprintError):
    pass


class Exists(BlueprintError):
    pass


class CoreBlueprint:
    def __init__(self, data, *, expected_domain=None, schema=None):
        if not isinstance(data, dict) or "blueprint" not in data:
            raise BlueprintError("Missing blueprint metadata")
        self.data = copy.deepcopy(data)
        self.metadata = self.data["blueprint"]
        if expected_domain is not None and self.metadata["domain"] != expected_domain:
            raise BlueprintError("Wrong blueprint domain")

    def yaml(self):
        return yaml.safe_dump(self.data)

    def update_metadata(self, *, source_url):
        self.metadata["source_url"] = source_url

    def validate(self):
        return (
            ["Requires a newer Home Assistant"] if self.metadata.get("future") else None
        )


class CoreDomain:
    def __init__(self, hass, directory):
        self.hass, self.domain, self.blueprint_folder = hass, "automation", directory
        self.calls = []
        self.reads = 0
        self.in_use = []
        self.entered = asyncio.Event()
        self.release = None
        self.fail_after_write = False

    async def async_get_blueprints(self):
        self.reads += 1

        def load():
            return {
                str(path.relative_to(self.blueprint_folder)).replace(
                    "\\", "/"
                ): CoreBlueprint(yaml.safe_load(path.read_text()))
                for path in self.blueprint_folder.rglob("*.yaml")
            }

        return await self.hass.async_add_executor_job(load)

    async def async_inputs_from_config(self, config):
        path = config["use_blueprint"]["path"]
        blueprint = (await self.async_get_blueprints())[path]
        body = copy.deepcopy(blueprint.data)
        body.pop("blueprint")
        return SimpleNamespace(async_substitute=lambda: body)

    async def async_add_blueprint(self, blueprint, path, allow_override=False):
        self.calls.append(("save", path, allow_override))
        self.entered.set()
        if self.release is not None:
            await self.release.wait()

        def write():
            target = self.blueprint_folder / path
            exists = target.exists()
            if exists and not allow_override:
                raise Exists
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(blueprint.yaml(), encoding="utf-8", newline="\n")
            return exists

        exists = await self.hass.async_add_executor_job(write)
        if self.fail_after_write:
            raise RuntimeError("reload failed after persistence")
        return exists

    async def async_remove_blueprint(self, path):
        if self.in_use:
            raise InUse
        self.calls.append(("delete", path))
        await self.hass.async_add_executor_job((self.blueprint_folder / path).unlink)


@pytest.fixture
def blueprints(native, monkeypatch, tmp_path):
    directory = tmp_path / "blueprints" / "automation"
    directory.mkdir(parents=True)
    hass = SimpleNamespace(data={}, async_add_executor_job=asyncio.to_thread)
    owner = CoreDomain(hass, directory)
    hass.data["blueprint"] = {"automation": owner}
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.models",
        SimpleNamespace(DomainBlueprints=CoreDomain, Blueprint=CoreBlueprint),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.errors",
        SimpleNamespace(
            BlueprintException=BlueprintError,
            BlueprintInUse=InUse,
            FileAlreadyExists=Exists,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.schemas",
        SimpleNamespace(BLUEPRINT_SCHEMA=lambda data: data),
    )
    core_yaml = SimpleNamespace(
        parse_yaml=yaml.safe_load, UndefinedSubstitution=BlueprintError
    )
    monkeypatch.setitem(sys.modules, "homeassistant.util.yaml", core_yaml)
    monkeypatch.setattr(
        sys.modules["homeassistant.util"], "yaml", core_yaml, raising=False
    )
    monkeypatch.setattr(
        sys.modules["homeassistant.exceptions"],
        "HomeAssistantError",
        BlueprintError,
        raising=False,
    )
    cv = sys.modules["homeassistant.helpers"].config_validation
    monkeypatch.setattr(cv, "path", lambda value: value, raising=False)
    monkeypatch.setattr(cv, "url", lambda value: value, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.automation",
        SimpleNamespace(automations_with_blueprint=lambda hass, path: owner.in_use),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.script",
        SimpleNamespace(scripts_with_blueprint=lambda hass, path: []),
    )
    for domain in ("automation", "script"):
        monkeypatch.setattr(
            sys.modules["homeassistant.components"],
            domain,
            sys.modules[f"homeassistant.components.{domain}"],
            raising=False,
        )
    active = [True]

    async def authorize(actor):
        if not active[0]:
            raise PermissionError("revoked")

    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.blueprint_management"
    )
    backup = AsyncMock(return_value=None)
    service = module.NativeBlueprints(
        hass, CallContext("owner"), authorize=authorize, backup=backup
    )
    return service, owner, backup, active


def source(name="Original", **metadata):
    return yaml.safe_dump(
        {
            "blueprint": {
                "name": name,
                "domain": "automation",
                "input": {},
                **metadata,
            },
            "triggers": [],
            "actions": [],
        }
    )


async def test_blueprint_round_trip_uses_installed_text_and_Core_callbacks(blueprints):
    service, owner, backup, _active = blueprints
    created = await service.manage("save", path="author/example", yaml=source())
    assert (
        created["data"]["path"] == "author/example.yaml"
        and not created["data"]["overrides_existing"]
    )
    backup.assert_not_awaited()
    target = owner.blueprint_folder / "author/example.yaml"
    target.write_bytes(source().replace("\n", "\r\n").encode())
    installed = target.read_bytes().decode()
    read = await service.manage("get", path="author/example.yaml")
    assert read["data"]["yaml"] == installed and read["data"]["yaml_source"] == "file"
    updated = await service.manage(
        "save", path="author/example", yaml=source("Changed"), overwrite=True
    )
    assert updated["data"]["overrides_existing"]
    assert backup.await_args.args[0]["current_yaml"] == installed
    listed = await service.manage("list")
    assert listed["data"]["count"] == 1
    rendered = await service.manage("substitute", path="author/example.yaml")
    assert rendered["data"]["config"] == {"triggers": [], "actions": []}
    removed = await service.manage("delete", path="author/example.yaml", confirm=True)
    assert removed["success"] and not target.exists()
    assert [call[0] for call in owner.calls] == ["save", "save", "delete"]


@pytest.mark.parametrize("change", ["file", "owner", "revoke"])
async def test_blueprint_change_during_capture_prevents_Core_write(blueprints, change):
    service, owner, backup, active = blueprints
    target = owner.blueprint_folder / "existing.yaml"
    target.write_text(source(), encoding="utf-8")

    async def capture(payload, actor):
        if change == "file":
            target.write_text(source("External"), encoding="utf-8")
        elif change == "owner":
            owner.hass.data["blueprint"]["automation"] = CoreDomain(
                owner.hass, owner.blueprint_folder
            )
        else:
            active[0] = False

    backup.side_effect = capture
    with pytest.raises(ToolFailure) as raised:
        await service.manage(
            "save", path="existing", yaml=source("Requested"), overwrite=True
        )
    assert raised.value.status == "not_started" and not owner.calls
    assert "Requested" not in target.read_text()


async def test_blueprint_reload_failure_keeps_unknown_outcome_without_retry(blueprints):
    service, owner, _backup, _active = blueprints
    owner.fail_after_write = True
    with pytest.raises(ToolFailure) as raised:
        await service.manage("save", path="saved", yaml=source())
    assert raised.value.status == "outcome_unknown" and len(owner.calls) == 1
    assert (owner.blueprint_folder / "saved.yaml").exists()


async def test_blueprint_confirmation_and_Core_compatibility_precede_capture(
    blueprints,
):
    service, owner, backup, _active = blueprints
    with pytest.raises(ToolFailure, match="not confirmed"):
        await service.manage("delete", path="existing.yaml")
    with pytest.raises(ToolFailure, match="newer Home Assistant"):
        await service.manage(
            "save", path="existing", yaml=source(future=True), overwrite=True
        )
    backup.assert_not_awaited()
    assert not owner.calls and not owner.reads


async def test_blueprint_Core_consumer_refusal_retains_file(blueprints):
    service, owner, _backup, _active = blueprints
    target = owner.blueprint_folder / "existing.yaml"
    target.write_text(source())
    owner.in_use = ["automation.consumer"]
    with pytest.raises(ToolFailure) as raised:
        await service.manage("delete", path="existing.yaml", confirm=True)
    assert (
        raised.value.code == "RESOURCE_LOCKED" and raised.value.status == "not_started"
    )
    assert raised.value.data["in_use_by"] == owner.in_use and target.exists()


async def test_blueprint_readonly_denies_even_snapshot_mutation(blueprints):
    service, owner, backup, _active = blueprints
    service.actor = CallContext("owner", read_only=True)
    target = owner.blueprint_folder / "existing.yaml"
    target.write_text(source())
    assert (await service.manage("get", path="existing.yaml"))["success"]
    with pytest.raises(ToolFailure) as raised:
        await service.manage(
            "save", path="existing", yaml=source("Requested"), overwrite=True
        )
    assert raised.value.code == "READ_ONLY" and not owner.calls
    backup.assert_not_awaited()


async def test_two_cancellations_drain_Core_writer_before_releasing_blueprint_lock(
    blueprints,
):
    service, owner, _backup, _active = blueprints
    owner.release = asyncio.Event()
    first = asyncio.create_task(service.manage("save", path="shared", yaml=source()))
    await owner.entered.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(
        service.manage("save", path="shared", yaml=source("Second"), overwrite=True)
    )
    await asyncio.sleep(0)
    assert not first.done() and len(owner.calls) == 1
    owner.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert (await second)["success"] and len(owner.calls) == 2
    assert "Second" in (owner.blueprint_folder / "shared.yaml").read_text()


async def test_blueprint_outside_domain_path_is_never_written(blueprints):
    service, owner, backup, _active = blueprints
    with pytest.raises(ToolFailure, match="escapes"):
        await service.manage("save", path="../outside", yaml=source(), overwrite=True)
    assert not owner.calls
    backup.assert_not_awaited()


async def test_blueprint_import_fetches_once_and_captures_installed_copy(
    blueprints, monkeypatch
):
    service, owner, backup, _active = blueprints
    target = owner.blueprint_folder / "author/imported.yaml"
    target.parent.mkdir()
    installed = source("Installed local copy")
    target.write_text(installed, encoding="utf-8", newline="\n")
    raw = source("Published replacement")
    fetch = AsyncMock(
        return_value=SimpleNamespace(
            blueprint=CoreBlueprint(yaml.safe_load(raw)),
            raw_data=raw,
            suggested_filename="author/imported",
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.components.blueprint.importer",
        SimpleNamespace(fetch_blueprint_from_url=fetch),
    )
    result = await service.manage(
        "import",
        domain="ignored",
        url="https://example.test/blueprint.yaml",
        overwrite=True,
    )
    assert result["data"]["imported_blueprint"]["domain"] == "automation"
    assert result["data"]["overrides_existing"] and fetch.await_count == 1
    assert backup.await_args.args[0]["current_yaml"] == installed
    assert "https://example.test/blueprint.yaml" in target.read_text()


async def test_blueprint_write_revalidates_in_owned_worker_before_Core_dispatch(
    blueprints, monkeypatch
):
    service, owner, _backup, active = blueprints
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.blueprint_owner"
    )
    owned = module.await_owned_io

    async def revoke_before_worker(operation):
        if getattr(getattr(operation, "cr_code", None), "co_name", None) == "_write":
            active[0] = False
        return await owned(operation)

    monkeypatch.setattr(module, "await_owned_io", revoke_before_worker)
    with pytest.raises(ToolFailure) as raised:
        await service.manage("save", path="scheduled", yaml=source())
    assert (
        raised.value.code == "AUTH_FORBIDDEN" and raised.value.status == "not_started"
    )
    assert not owner.calls


async def test_blueprint_revocation_after_dispatch_does_not_return_file_contents(
    blueprints,
):
    service, owner, _backup, active = blueprints
    owner.release = asyncio.Event()
    pending = asyncio.create_task(
        service.manage("save", path="revoked", yaml=source("Private contents"))
    )
    await owner.entered.wait()
    active[0] = False
    owner.release.set()
    with pytest.raises(ToolFailure) as raised:
        await pending
    assert raised.value.status == "outcome_unknown" and "Private contents" not in str(
        raised.value.data
    )
    assert len(owner.calls) == 1 and (owner.blueprint_folder / "revoked.yaml").exists()


async def test_blueprint_restore_recreates_missing_file_via_Core(
    blueprints,
):
    service, owner, backup, _active = blueprints
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.blueprint_restore"
    )
    restore = module.NativeBlueprintRestore(
        service.store("automation"), "restored.yaml", source("Saved")
    )
    assert await restore.current() is None
    result = await restore.apply(None)
    assert result["post_write_verified"] and not result["overrides_existing"]
    backup.assert_not_awaited()


async def test_blueprint_restore_pins_the_file_before_safety_io(blueprints):
    service, owner, _backup, _active = blueprints
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.blueprint_restore"
    )
    target = owner.blueprint_folder / "restored.yaml"
    target.write_text(source("Current"))
    restore = module.NativeBlueprintRestore(
        service.store("automation"), "restored.yaml", source("Saved")
    )
    current = await restore.current()
    target.write_text(source("External"))
    calls = len(owner.calls)
    with pytest.raises(ToolFailure) as raised:
        await restore.apply(current)
    assert raised.value.status == "not_started" and len(owner.calls) == calls


@pytest.mark.parametrize(
    "failure", ["save_cancel", "delete_cancel", "overwrite_exists"]
)
async def test_blueprint_Core_failure_after_mutation_never_claims_not_started(
    blueprints, failure
):
    service, owner, _backup, _active = blueprints
    target = owner.blueprint_folder / "existing.yaml"
    target.write_text(source("Before"))
    method = (
        "async_remove_blueprint"
        if failure == "delete_cancel"
        else "async_add_blueprint"
    )
    original = getattr(owner, method)

    async def changed_then_failed(*args, **kwargs):
        await original(*args, **kwargs)
        if failure == "overwrite_exists":
            raise Exists("Reload failure after overwriting")
        raise asyncio.CancelledError("Core cancelled after persisting")

    setattr(owner, method, changed_then_failed)
    with pytest.raises(ToolFailure) as raised:
        if failure == "delete_cancel":
            await service.manage("delete", path="existing.yaml", confirm=True)
        else:
            await service.manage(
                "save", path="existing", yaml=source("After"), overwrite=True
            )
    assert (
        raised.value.code == "OUTCOME_UNKNOWN"
        and raised.value.status == "outcome_unknown"
    )
    assert len(owner.calls) == 1
    assert (
        (not target.exists())
        if failure == "delete_cancel"
        else "After" in target.read_text()
    )


async def test_blueprint_public_contract_shares_readonly_action_classification(
    blueprints,
):
    from ha_mcp.runtime import Engine

    service, owner, backup, _active = blueprints
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_blueprint_tools"
    )
    backend = SimpleNamespace(authorize=service.authorize)
    manager = SimpleNamespace(
        hass=owner.hass,
        backend=backend,
        snapshots=SimpleNamespace(blueprint_backup=backup),
    )
    definition = module.blueprint_definition(manager)
    engine = Engine(backend, [definition], read_only=True)
    result = await engine.call(
        "ha_manage_blueprints", {"action": "list"}, CallContext("owner")
    )
    assert result.data["data"]["count"] == 0 and not result.is_error
    with pytest.raises(ToolFailure) as raised:
        await engine.call(
            "ha_manage_blueprints",
            {"action": "save", "path": "new", "yaml": source()},
            CallContext("owner"),
        )
    assert raised.value.code == "READ_ONLY" and not owner.calls
    assert definition.annotations["readOnlyHint"] is False


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("Missing input target_entity", "VALIDATION_FAILED"),
        ("Failed to load blueprint missing.yaml", "RESOURCE_NOT_FOUND"),
        ("Cannot substitute this value", "SERVICE_CALL_FAILED"),
    ],
)
async def test_blueprint_substitute_preserves_shared_Core_error_taxonomy(
    blueprints, message, code
):
    service, owner, _backup, _active = blueprints
    owner.async_inputs_from_config = AsyncMock(side_effect=BlueprintError(message))
    with pytest.raises(ToolFailure) as raised:
        await service.manage("substitute", path="missing.yaml")
    assert raised.value.code == code and raised.value.status == "not_started"
