"""Boundary tests for HACS owners; stand-ins are not a live HACS qualification."""

import asyncio
import copy
import importlib
import json
import sys
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from ha_mcp.runtime import Engine
from ha_mcp.tool_api import CallContext, ToolFailure

from ._fixture_effects import notify_effect
from .test_component_ws_search import _REAL_VOL
from .test_native_engine_backend import native as native


class RepositoryData(SimpleNamespace):
    def to_json(self):
        return copy.deepcopy(
            {key: value for key, value in vars(self).items() if key != "last_fetched"}
        )


class Repository:
    def __init__(
        self, owner, identifier="1", name="example/fixture", category="integration"
    ):
        self.hacs = owner
        self.data = RepositoryData(
            id=identifier,
            full_name=name,
            category=category,
            installed=True,
            installed_version="1",
            installed_commit=None,
            last_version="2",
            last_fetched=datetime(2026, 1, 1, tzinfo=UTC),
            authors=["Fixture"],
            description="Fixture repository",
            domain="fixture",
            downloads=5,
            stargazers_count=1,
            published_tags=["2", "1"],
            new=True,
            topics=["fixture"],
            default_branch="main",
        )
        self.repository_manifest = SimpleNamespace(
            country=[], homeassistant=None, hide_default_branch=False
        )
        self.content = SimpleNamespace(
            path=SimpleNamespace(local="/config/custom_components/fixture")
        )
        self.ref = "main"
        self.pending_restart = False
        self.updated_info = False
        self.additional_info = "Cached README"
        self.ignored_by_country_configuration = False
        self.state = None
        self.async_download_repository = AsyncMock(side_effect=self._download)
        self.uninstall = AsyncMock(side_effect=self._remove)
        self.update_repository = AsyncMock(side_effect=self._refresh)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.after = None

    @property
    def display_name(self):
        return self.data.full_name.split("/")[-1]

    @property
    def display_installed_version(self):
        return self.data.installed_version or ""

    @property
    def display_available_version(self):
        return self.data.last_version

    @property
    def pending_update(self):
        return (
            self.data.installed
            and self.data.installed_version != self.data.last_version
        )

    @property
    def can_download(self):
        return True

    @property
    def display_status(self):
        return "installed" if self.data.installed else "default"

    @property
    def display_version_or_commit(self):
        return "version"

    _done = notify_effect

    async def _download(self, *, ref=None):
        self.data.installed = True
        self.data.installed_version = ref or self.data.last_version
        self.pending_restart = True
        await self._done()

    async def _remove(self):
        self.data.installed = False
        self.data.installed_version = None
        await self._done()

    async def _refresh(self, **_kwargs):
        self.data.last_version = "3"
        await self._done()


@pytest.fixture
def hacs(native, monkeypatch):
    marker = object()
    entry = SimpleNamespace(
        entry_id="hacs-entry", state=marker, data={"country": "ALL"}, options={}
    )
    owner = SimpleNamespace(
        version="2.0.5",
        configuration=SimpleNamespace(config_entry=entry),
        status=SimpleNamespace(startup=False),
        system=SimpleNamespace(running=True, disabled=False, disabled_reason=None),
        data=SimpleNamespace(async_write=AsyncMock()),
        github=object(),
        githubapi=object(),
        common=SimpleNamespace(
            categories={"integration", "plugin", "theme"},
            skip=set(),
            renamed_repositories={},
        ),
        coordinators={"integration": SimpleNamespace(async_update_listeners=Mock())},
        async_dispatch=Mock(),
        async_recreate_entities=AsyncMock(),
    )
    repo = Repository(owner)
    repositories = {"1": repo}
    owner.repositories = SimpleNamespace(
        get_by_id=lambda key: repositories.get(str(key)),
        get_by_full_name=lambda name: next(
            (
                r
                for r in repositories.values()
                if r.data.full_name.lower() == name.lower()
            ),
            None,
        ),
        list_all=list(repositories.values()),
        is_default=lambda _identifier: False,
    )

    async def register(*, repository_full_name, category):
        created = Repository(owner, "2", repository_full_name, category)
        created.data.installed = False
        repositories["2"] = created
        owner.repositories.list_all = list(repositories.values())

    owner.async_register_repository = AsyncMock(side_effect=register)
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"hacs", "ha_mcp_tools"}, time_zone="UTC"),
        config_entries=SimpleNamespace(
            async_get_entry=lambda key: entry if key == entry.entry_id else None
        ),
        data={"hacs": owner, "websocket_api": {}},
    )
    for command in (
        "hacs/repositories/list",
        "hacs/repository/info",
        "hacs/repository/download",
        "hacs/repository/remove",
        "hacs/repository/refresh",
        "hacs/repositories/add",
    ):
        hass.data["websocket_api"][command] = (object(), copy.deepcopy)
    state = SimpleNamespace(active=True)

    async def authorize(actor):
        if actor.credential:
            await actor.credential()
        if not state.active:
            raise PermissionError("Actor revoked")

    access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.hacs_access"
    )
    monkeypatch.setattr(access, "ConfigEntryState", SimpleNamespace(LOADED=marker))
    registry_access = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.registry_access"
    )
    monkeypatch.setattr(
        registry_access, "json_bytes", lambda value: json.dumps(value).encode()
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.hacs.enums",
        SimpleNamespace(HacsDispatchEvent=SimpleNamespace(RELOAD="reload")),
    )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.hacs.utils.regex",
        SimpleNamespace(extract_repository_from_url=lambda value: value),
    )
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_operations.hacs_writes"
    )
    binder = importlib.import_module("custom_components.ha_mcp_tools.native_hacs_tools")
    manager = SimpleNamespace(hass=hass, backend=SimpleNamespace(authorize=authorize))
    manager.visibility = SimpleNamespace(before=AsyncMock())
    actor = CallContext("operator")
    engine = Engine(manager.backend, binder.hacs_definitions(manager))
    return SimpleNamespace(
        hass=hass,
        owner=owner,
        repo=repo,
        repositories=repositories,
        entry=entry,
        state=state,
        access=access,
        module=module,
        manager=manager,
        actor=actor,
        engine=engine,
    )


async def call(hacs, action, **kwargs):
    name = "ha_get_hacs_info" if action in {"search", "info"} else "ha_manage_hacs"
    return await hacs.engine.call(name, {"action": action, **kwargs}, hacs.actor)


async def test_info_never_refreshes_clears_new_or_saves_even_on_unfetched_repository(
    hacs,
):
    hacs.repo.data.last_fetched = None
    hacs.repo.additional_info = None
    hacs.engine.read_only = True
    result = await call(hacs, "info", repository_id="EXAMPLE/FIXTURE")
    assert not result.is_error, result
    data = result.data["data"]
    assert (
        data["cache"]["refreshed_from_github"] is False
        and not data["cache"]["readme_available"]
    )
    assert hacs.repo.data.new and hacs.repo.updated_info is False
    hacs.repo.update_repository.assert_not_awaited()
    hacs.owner.data.async_write.assert_not_awaited()


async def test_search_keeps_scoring_pagination_and_metadata_without_network(hacs):
    result = await call(hacs, "search", query="fixture", max_results=1)
    assert not result.is_error, result
    data = result.data["data"]
    assert data["count"] == 1 and data["results"][0]["score"] > 0
    assert (
        data["results"][0]["pending_update"] and data["cache"]["source"] == "hacs_cache"
    )
    assert result.data["metadata"]["home_assistant_timezone"] == "UTC"


@pytest.mark.parametrize(
    "action",
    ["search", "info", "download", "remove", "update_information", "add_repository"],
)
async def test_absent_hacs_fails_before_any_optional_import(hacs, monkeypatch, action):
    import builtins

    original = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("custom_components.hacs"):
            raise AssertionError("Absent HACS imported")
        return original(name, *args, **kwargs)

    hacs.hass.config.components.remove("hacs")
    monkeypatch.setattr(builtins, "__import__", blocked)
    arguments = (
        {"repository": "example/new", "category": "integration"}
        if action == "add_repository"
        else {"repository_id": "1"}
    )
    result = await call(hacs, action, **arguments)
    assert (
        result.is_error and result.data["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    ), result


async def test_initializing_hacs_future_is_never_awaited_or_cancelled(hacs):
    pending = asyncio.get_running_loop().create_future()
    hacs.hass.data["hacs"] = pending
    result = await call(hacs, "search")
    assert result.is_error and result.status == "not_started" and not pending.done()
    pending.cancel()


@pytest.mark.parametrize("action", ["download", "remove", "update_information"])
async def test_writes_use_real_repository_methods_and_persist_before_reporting_success(
    hacs, action
):
    result = await call(hacs, action, repository_id="1")
    assert not result.is_error, result
    hacs.owner.data.async_write.assert_awaited_once()
    if action == "download":
        hacs.repo.async_download_repository.assert_awaited_once_with(ref=None)
        assert result.data["data"]["pending_restart"]
    elif action == "remove":
        hacs.repo.uninstall.assert_awaited_once()
        assert not hacs.repo.data.installed
        hacs.repo.update_repository.assert_not_awaited()
    else:
        hacs.repo.update_repository.assert_awaited_once_with(
            ignore_issues=True, force=True
        )
        hacs.owner.coordinators[
            "integration"
        ].async_update_listeners.assert_called_once()


@pytest.mark.parametrize(
    "race",
    [
        "actor",
        "credential",
        "policy",
        "owner",
        "repository",
        "typed_image",
        "github_client",
        "configuration",
        "method",
    ],
)
async def test_worker_rechecks_authority_and_frozen_hacs_targets(
    hacs, monkeypatch, race
):
    original = hacs.module.NativeHacs._commit

    async def credential():
        raise PermissionError("expired")

    async def commit(self, access, repo, *args):
        changes = {
            "actor": lambda: setattr(hacs.state, "active", False),
            "credential": lambda: setattr(
                access, "actor", CallContext("operator", credential=credential)
            ),
            "policy": lambda: setattr(hacs.engine, "read_only", True),
            "owner": lambda: hacs.hass.data.__setitem__("hacs", copy.copy(hacs.owner)),
            "repository": lambda: hacs.repositories.__setitem__(
                "1", Repository(hacs.owner)
            ),
            "typed_image": lambda: setattr(repo.data, "stargazers_count", True),
            "github_client": lambda: setattr(hacs.owner, "githubapi", object()),
            "configuration": lambda: hacs.entry.data.__setitem__("country", "changed"),
            "method": lambda: setattr(repo, "async_download_repository", AsyncMock()),
        }
        changes[race]()
        return await original(self, access, repo, *args)

    original_download = hacs.repo.async_download_repository
    monkeypatch.setattr(hacs.module.NativeHacs, "_commit", commit)
    result = await call(hacs, "download", repository_id="1")
    assert result.is_error and result.status == "not_started", result
    original_download.assert_not_awaited()
    hacs.owner.data.async_write.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [RuntimeError("after file write"), asyncio.CancelledError()]
)
async def test_provider_failure_after_effect_is_unknown_and_never_replayed(hacs, error):
    def fail():
        raise error

    hacs.repo.after = fail
    result = await call(hacs, "download", repository_id="1")
    assert result.is_error and result.status == "outcome_unknown", result
    assert hacs.repo.async_download_repository.await_count == 1


async def test_revocation_after_install_blocks_persistence_and_reports_uncertainty(
    hacs,
):
    hacs.repo.after = lambda: setattr(hacs.state, "active", False)
    result = await call(hacs, "download", repository_id="1")
    assert result.is_error and result.status == "outcome_unknown"
    assert hacs.repo.data.installed_version == "2"
    hacs.owner.data.async_write.assert_not_awaited()


async def test_double_cancel_drains_hacs_write_before_releasing_mutex(hacs):
    hacs.repo.block = True

    async def write(version):
        return await hacs.module.NativeHacs(hacs.manager, hacs.actor)._hacs_download(
            "1", version
        )

    first = asyncio.create_task(write("2"))
    await hacs.repo.started.wait()
    first.cancel()
    await asyncio.sleep(0)
    first.cancel()
    second = asyncio.create_task(write("3"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    held = (
        not first.done()
        and not second.done()
        and hacs.repo.async_download_repository.await_count == 1
    )
    hacs.repo.release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await second
    assert (
        held
        and result["success"]
        and hacs.repo.async_download_repository.await_count == 2
    )


async def test_core_validator_rejection_has_no_hacs_effect(hacs):
    def reject(_message):
        raise _REAL_VOL.Invalid("HACS rejects the requested version")

    hacs.hass.data["websocket_api"]["hacs/repository/download"] = (object(), reject)
    result = await call(hacs, "download", repository_id="1", version="bad")
    assert result.is_error and result.status == "not_started"
    hacs.repo.async_download_repository.assert_not_awaited()


@pytest.mark.parametrize("action", ["download", "remove"])
async def test_reviewed_self_file_change_does_not_claim_new_runtime_or_schedule_restart(
    hacs, action
):
    hacs.repo.data.domain = "ha_mcp_tools"
    result = await call(hacs, action, repository_id="1")
    assert not result.is_error, result
    data = result.data["data"]
    assert data["self_affecting"] and data["runtime_still_loaded"]
    assert (
        not data["active_version_verified"]
        and not data["restart_requested"]
        and not data["config_entries_removed"]
    )


async def test_unreviewed_hacs_version_refuses_self_mutation_before_dispatch(hacs):
    hacs.owner.version = "2.1.0"
    hacs.repo.data.domain = "ha_mcp_tools"
    result = await call(hacs, "download", repository_id="1")
    assert result.is_error and result.status == "not_started"
    hacs.repo.async_download_repository.assert_not_awaited()


async def test_add_waits_for_the_owner_and_confirms_actual_registration(hacs):
    result = await call(
        hacs, "add_repository", repository="example/new", category="lovelace"
    )
    assert not result.is_error and result.data["data"]["repository_id"] == "2", (
        result.data
    )
    hacs.owner.async_register_repository.assert_awaited_once_with(
        repository_full_name="example/new", category="plugin"
    )
    hacs.owner.data.async_write.assert_awaited_once()


async def test_silent_registration_refusal_cannot_be_reported_as_success(hacs):
    hacs.owner.async_register_repository.side_effect = None
    result = await call(
        hacs, "add_repository", repository="example/new", category="integration"
    )
    assert result.is_error and result.status == "outcome_unknown"
    assert hacs.owner.async_register_repository.await_count == 1
    hacs.owner.data.async_write.assert_not_awaited()


async def test_download_cannot_claim_requested_version_when_owner_returns_without_installing_it(
    hacs,
):
    hacs.repo.async_download_repository.side_effect = None
    result = await call(hacs, "download", repository_id="1", version="2")
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert hacs.repo.data.installed_version == "1"
    hacs.owner.data.async_write.assert_not_awaited()


async def test_first_install_recreates_hacs_entities_before_persistence(hacs):
    hacs.repo.data.installed = False
    result = await call(hacs, "download", repository_id="1", version="2")
    assert not result.is_error, result.data
    hacs.owner.async_dispatch.assert_called_once_with("reload", {"force": True})
    hacs.owner.async_recreate_entities.assert_awaited_once()
    hacs.owner.data.async_write.assert_awaited_once()


async def test_changed_repository_during_persistence_is_not_a_confirmed_install(hacs):
    async def save():
        hacs.repo.data.installed_version = "external"

    hacs.owner.data.async_write.side_effect = save
    result = await call(hacs, "download", repository_id="1", version="2")
    assert result.is_error and result.status == "outcome_unknown", result.data
    assert hacs.repo.async_download_repository.await_count == 1


async def test_disabled_hacs_can_explain_cached_data_but_cannot_start_a_write(hacs):
    hacs.owner.system.disabled = True
    hacs.owner.system.disabled_reason = "rate_limit"
    cached = await call(hacs, "info", repository_id="1")
    assert (
        not cached.is_error
        and cached.data["data"]["cache"]["hacs_disabled_reason"] == "rate_limit"
    )
    write = await call(hacs, "update_information", repository_id="1")
    assert write.is_error and write.status == "not_started"
    hacs.repo.update_repository.assert_not_awaited()


async def test_authorization_revoked_by_progress_withholds_search_results(hacs):
    async def progress(**kwargs):
        if kwargs["progress"] == 2:
            hacs.state.active = False

    hacs.actor = CallContext("operator", progress=progress)
    result = await call(hacs, "search")
    assert result.is_error and result.status == "not_started", result.data
    assert "results" not in str(result.data)


async def test_repository_appearing_before_registration_is_never_overwritten(
    hacs, monkeypatch
):
    original = hacs.module.NativeHacs._register

    async def register(self, access, name, *args):
        hacs.repositories["2"] = Repository(hacs.owner, "2", name)
        return await original(self, access, name, *args)

    monkeypatch.setattr(hacs.module.NativeHacs, "_register", register)
    result = await call(
        hacs, "add_repository", repository="example/new", category="integration"
    )
    assert result.is_error and result.status == "not_started", result.data
    hacs.owner.async_register_repository.assert_not_awaited()
    hacs.owner.data.async_write.assert_not_awaited()


def test_hacs_public_signatures_metadata_and_ctx_are_shared(hacs):
    import inspect

    from ha_mcp.runtime.hacs_tools import HacsFacade
    from ha_mcp.tools.tools_hacs import HacsTools

    for name in ("ha_get_hacs_info", "ha_manage_hacs"):
        shared = inspect.signature(getattr(HacsFacade, name))
        legacy = inspect.signature(getattr(HacsTools, name))
        assert set(shared.parameters) == set(legacy.parameters)
        assert {key: value.default for key, value in shared.parameters.items()} == {
            key: value.default for key, value in legacy.parameters.items()
        }
        definition = hacs.engine.tools[name]
        assert (
            definition.tags == frozenset({"HACS"})
            and definition.annotations["openWorldHint"]
        )
        assert "ctx" not in definition.input_schema["properties"]
    assert hacs.engine.tools["ha_get_hacs_info"].annotations["readOnlyHint"]
    assert hacs.engine.tools["ha_manage_hacs"].annotations["destructiveHint"]


@pytest.mark.parametrize("race", ["visibility", "actor", "repository", "method"])
async def test_final_visibility_await_precedes_the_last_frozen_dispatch_guards(
    hacs, race
):
    original = hacs.repo.async_download_repository

    async def before(name, arguments, actor):
        assert name == "ha_manage_hacs" and arguments["action"] == "download"
        if race == "visibility":
            raise ToolFailure(
                "ENTITY_VISIBILITY_ENFORCED",
                "Opaque HACS file operation refused",
                status="not_started",
            )
        if race == "actor":
            hacs.state.active = False
        elif race == "repository":
            hacs.repo.data.stargazers_count = True
        elif race == "method":
            hacs.repo.async_download_repository = AsyncMock()

    hacs.manager.visibility.before.side_effect = before
    result = await call(hacs, "download", repository_id="1", version="2")
    assert result.is_error and result.status == "not_started", result.data
    original.assert_not_awaited()
    hacs.owner.data.async_write.assert_not_awaited()
