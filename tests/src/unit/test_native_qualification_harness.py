"""Qualification must handle prerelease Core and stop stalled child processes."""

import importlib
import importlib.util
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.fixture
def harness(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "native"))
    return importlib.import_module("run_container")


@pytest.mark.parametrize(
    ("version", "modern"),
    [
        ("2026.9.9", False),
        ("2026.10.0", True),
        ("2026.10.4", True),
        ("2026.11.0b2", True),
        ("2026.11.0.dev202610310001", True),
    ],
)
async def test_oauth_qualification_selects_login_for_prerelease_core(
    harness, version, modern
):
    oauth = importlib.import_module("oauth_checks")
    checks = SimpleNamespace(
        control=AsyncMock(return_value={"versions": {"homeassistant": version}})
    )
    assert await oauth._supports_new_login(checks) is modern


def test_short_docker_operations_have_a_finite_deadline(harness, monkeypatch):
    def run(command, **kwargs):
        assert 0 < kwargs.get("timeout", float("inf")) < float("inf")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(harness.subprocess, "run", run)
    harness.docker("inspect", "fixture")


@pytest.mark.parametrize(
    "probe", ["exercise_external_clients", "exercise_standard_proxy"]
)
async def test_external_probe_timeout_propagates_without_retry(
    harness, monkeypatch, probe
):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        timeout = kwargs.get("timeout", float("inf"))
        assert 0 < timeout < float("inf")
        raise subprocess.TimeoutExpired("disposable probe", timeout)

    monkeypatch.setattr(harness.subprocess, "run", run)
    checks = SimpleNamespace(
        path="/fixture",
        credentials={"alpha": {"access_token": "disposable"}},
        passed=Mock(),
    )
    args = (
        (checks, "fixture-user") if probe == "exercise_external_clients" else (checks,)
    )
    with pytest.raises(subprocess.TimeoutExpired):
        await getattr(harness, probe)(*args)
    assert len(calls) == 1
    checks.passed.assert_not_called()


@pytest.mark.parametrize("phase", ["run", "readiness", "logs"])
async def test_container_is_removed_even_when_startup_or_log_capture_times_out(
    harness, monkeypatch, tmp_path, phase
):
    calls = []

    def docker(command, *args, **kwargs):
        calls.append(command)
        if command == phase:
            raise subprocess.TimeoutExpired("disposable container", 1)
        return subprocess.CompletedProcess([command], 0, "", "")

    monkeypatch.setattr(harness, "CONFIG", tmp_path / "config")
    monkeypatch.setattr(harness, "OUT", tmp_path / "out")
    monkeypatch.setattr(harness, "docker", docker)
    monkeypatch.setattr(
        harness,
        "wait_identities",
        AsyncMock(side_effect=subprocess.TimeoutExpired("readiness", 1)),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        await harness.main()
    assert calls.count("run") == 1
    assert calls[-1] == "rm"


@pytest.mark.parametrize(
    "method,params",
    [
        ("server/discover", {}),
        ("tools/call", {"name": "framing_read", "arguments": {}}),
        ("resources/read", {"uri": "probe://frame/value"}),
    ],
)
async def test_raw_modern_bench_framing_reaches_the_real_sdk_router(
    harness, method, params
):
    import json

    import httpx

    from ha_mcp._vendor.fastmcp import FastMCP

    server = FastMCP("Framing probe", instructions="Exact framing instructions")

    @server.tool()
    async def framing_read() -> dict:
        return {"ready": True}

    @server.resource("probe://frame/value")
    async def framing_resource() -> str:
        return "ready"

    app = server.http_app(
        path="/mcp", stateless_http=True, host_origin_protection=False
    )
    sent = []

    async def record(request):
        sent.append((request.headers, json.loads(request.content)))

    async with (
        app.lifespan(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), event_hooks={"request": [record]}
        ) as client,
    ):
        checks = harness.Checks(client, {"alpha": {"access_token": "fixture-token"}})
        checks.path = "/mcp"
        result = await checks.rpc(method, params)
    assert "error" not in result, result
    headers, body = sent[-1]
    assert (
        headers.get("mcp-protocol-version")
        == body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
    )
    assert headers["mcp-method"] == method
    if method == "server/discover":
        assert result["result"]["instructions"] == "Exact framing instructions"
    else:
        assert (
            headers["mcp-name"]
            == params["uri" if method == "resources/read" else "name"]
        )


async def test_raw_legacy_bench_initialize_omits_the_modern_header(harness):
    import httpx

    from ha_mcp._vendor.fastmcp import FastMCP

    server = FastMCP("Legacy framing probe")
    app = server.http_app(
        path="/mcp", stateless_http=True, host_origin_protection=False
    )
    async with (
        app.lifespan(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app)) as client,
    ):
        checks = harness.Checks(client, {"alpha": {"access_token": "fixture-token"}})
        headers = checks.headers(method="initialize", modern=False)
        assert "Mcp-Protocol-Version" not in headers
        response = await client.post(
            "http://fixture/mcp",
            headers=headers,
            json=harness.message(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "legacy-fixture", "version": "1"},
                },
                modern=False,
            ),
        )
        body = harness.unpack(response)
    assert body["result"]["protocolVersion"] == "2025-11-25", body


async def test_entity_bench_finds_pre_rename_history_by_public_entity_id(
    harness, monkeypatch, tmp_path
):
    import httpx

    from ha_mcp.runtime.snapshot_store import SnapshotStore

    entities = importlib.import_module("entity_checks")
    backups = importlib.import_module("backup_checks")
    explicit_capture = AsyncMock()
    monkeypatch.setattr(backups, "_exercise_edit_backup", explicit_capture)
    entity_id = "sensor.original"
    registry_id = "stable-registry-uuid"
    before = {
        "id": registry_id,
        "entity_id": entity_id,
        "platform": "native_fixture",
        "device_id": "fixture-device",
    }
    renamed = {
        **before,
        "entity_id": entity_id + "_renamed",
        "name": "Native entity settings",
        "icon": "mdi:flash",
        "hidden_by": "user",
        "aliases": ["Native entity alias"],
        "options": {"sensor": {"display_precision": 2}},
    }
    store = SnapshotStore(tmp_path)
    snapshot = store.capture("entity", entity_id, before, "ha_set_entity")
    store.capture("entity", "sensor.unrelated", before, "ha_set_entity")
    assert snapshot is not None

    def respond(request):
        if request.url.path.endswith("/diff"):
            assert request.url.path.endswith("/" + snapshot.name + "/diff")
            return httpx.Response(200, text="Native entity settings")
        return httpx.Response(
            200, json={"backups": store.list_snapshots(**dict(request.url.params))}
        )

    class SnapshotSelected(Exception):
        pass

    async def stop_before_restore(checks, panel, name):
        assert name == snapshot.name
        raise SnapshotSelected

    monkeypatch.setattr(entities, "_restore", stop_before_restore)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://fixture"
    ) as client:
        checks = SimpleNamespace(
            client=client,
            call=AsyncMock(
                side_effect=[
                    {"success": True},
                    {"success": True, "entity_id": renamed["entity_id"]},
                ]
            ),
            control=AsyncMock(
                side_effect=[
                    {
                        "registry_entry": row,
                        "registry_id": registry_id,
                        "entity_id": row["entity_id"],
                        "state": {"state": "on"},
                    }
                    for row in (before, renamed)
                ]
            ),
            passed=Mock(),
        )
        with pytest.raises(SnapshotSelected):
            await entities.exercise_entities(checks, "http://fixture")
        explicit_capture.assert_awaited_once_with(checks, entity_id)


async def test_cold_resource_qualification_precedes_clients_that_can_load_core(
    harness, monkeypatch
):
    domain_tools = harness.exercise_domain_tools
    for name in vars(harness):
        if name.startswith("exercise_") and name not in {
            "exercise_runtime",
            "exercise_domain_tools",
        }:
            monkeypatch.setattr(harness, name, AsyncMock(return_value={}))

    loaded = False
    cold_checks = []

    async def dashboards(*args):
        assert loaded is False, "Cold Lovelace checks ran after unrelated readers"
        cold_checks.append("dashboards")

    async def resources(*args):
        nonlocal loaded
        assert loaded is False
        loaded = True
        cold_checks.append("resources")

    async def cards(*args):
        assert loaded is True
        cold_checks.append("cards")

    async def external(*args):
        nonlocal loaded
        loaded = True

    async def exercise(checks, **kwargs):
        await domain_tools(checks)
        return {"ready": True}

    monkeypatch.setattr(harness, "exercise_dashboards", dashboards)
    monkeypatch.setattr(harness, "exercise_resources", resources)
    monkeypatch.setattr(harness, "exercise_dashboard_card_types", cards)
    monkeypatch.setattr(harness, "exercise_external_clients", external)
    monkeypatch.setattr(harness, "exercise", exercise)
    monkeypatch.setattr(harness, "blueprint_source", lambda _: nullcontext("fixture"))
    monkeypatch.setattr(
        harness,
        "docker",
        Mock(
            return_value=SimpleNamespace(
                stdout='[{"NetworkSettings":{"Networks":{"bridge":{"Gateway":"fixture"}}}}]'
            )
        ),
    )
    result = await harness.exercise_runtime(SimpleNamespace(), "fixture-user")
    assert result == {"ready": True}
    assert cold_checks == ["dashboards", "resources", "cards"]


@pytest.mark.parametrize("bundled_marketplace", [False, True])
async def test_store_absence_probe_respects_cores_ignored_marketplace(
    harness, bundled_marketplace
):
    stores = importlib.import_module("hacs_checks")
    code = "CONFIG_CONFLICT" if bundled_marketplace else "COMPONENT_NOT_INSTALLED"
    message = (
        "Expected one loaded Marketplace owner"
        if bundled_marketplace
        else "HACS is not loaded"
    )
    checks = SimpleNamespace(
        control=AsyncMock(
            return_value={
                "hacs_loaded": False,
                "marketplace_component": bundled_marketplace,
                "marketplace_loaded": 0,
                "marketplace_entries": [{"source": "ignore"}]
                if bundled_marketplace
                else [],
            }
        ),
        rpc=AsyncMock(
            return_value={
                "result": {
                    "isError": True,
                    "structuredContent": {"error": {"code": code, "message": message}},
                }
            }
        ),
        passed=Mock(),
    )
    await stores.exercise_hacs_absence(checks)
    assert checks.rpc.await_count == 6


async def test_store_absence_probe_rejects_an_active_marketplace_before_tool_calls(
    harness,
):
    stores = importlib.import_module("hacs_checks")
    checks = SimpleNamespace(
        control=AsyncMock(
            return_value={
                "hacs_loaded": False,
                "marketplace_component": True,
                "marketplace_loaded": 1,
                "marketplace_entries": [{"source": "system"}],
            }
        ),
        rpc=AsyncMock(
            return_value={
                "result": {
                    "isError": True,
                    "structuredContent": {"error": {"code": "COMPONENT_NOT_INSTALLED"}},
                }
            }
        ),
        passed=Mock(),
    )
    with pytest.raises(AssertionError):
        await stores.exercise_hacs_absence(checks)
    checks.rpc.assert_not_awaited()


@pytest.fixture
def optional_store(harness, monkeypatch):
    path = Path(harness.__file__).parent / "fixtures/native_fixture/optional_store.py"
    spec = importlib.util.spec_from_file_location("_optional_store_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entries = []

    class MissingIntegration(Exception):
        pass

    integration = AsyncMock()
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.loader",
        SimpleNamespace(
            IntegrationNotFound=MissingIntegration,
            async_get_integration=integration,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.config_entries",
        SimpleNamespace(SOURCE_IGNORE="ignore"),
    )

    async def init(domain, *, context, data):
        entry = SimpleNamespace(source=context["source"], unique_id=data["unique_id"])
        entries.append(entry)
        return {"type": "create_entry", "result": entry}

    flow = SimpleNamespace(async_init=AsyncMock(side_effect=init))
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            flow=flow,
            async_entries=lambda _: list(entries),
            async_loaded_entries=lambda _: [],
        )
    )
    return SimpleNamespace(
        module=module,
        hass=hass,
        entries=entries,
        flow=flow,
        integration=integration,
        missing=MissingIntegration,
    )


@pytest.mark.parametrize("available", [False, True])
async def test_fixture_ignores_core_store_via_its_flow_without_recreating_on_restart(
    optional_store, available
):
    fixture = optional_store
    if not available:
        fixture.integration.side_effect = fixture.missing
    await fixture.module.prepare_optional_store_absence(fixture.hass)
    if available:
        entry = fixture.entries[0]
        assert entry.source == "ignore"
        await fixture.module.prepare_optional_store_absence(fixture.hass)
        assert fixture.entries == [entry]
        fixture.flow.async_init.assert_awaited_once()
        assert fixture.flow.async_init.await_args.args == ("marketplace",)
    else:
        assert fixture.entries == []
        fixture.flow.async_init.assert_not_awaited()


async def test_optional_store_fixture_preserves_unowned_existing_entries(
    optional_store,
):
    fixture = optional_store
    entry = SimpleNamespace(source="user", unique_id="unowned")
    fixture.entries.append(entry)
    with pytest.raises(AssertionError):
        await fixture.module.prepare_optional_store_absence(fixture.hass)
    assert fixture.entries == [entry]
    fixture.flow.async_init.assert_not_awaited()


async def test_optional_store_fixture_does_not_hide_flow_or_loader_failures(
    optional_store,
):
    fixture = optional_store
    fixture.flow.async_init.side_effect = None
    fixture.flow.async_init.return_value = {"type": "abort", "reason": "refused"}
    with pytest.raises(AssertionError):
        await fixture.module.prepare_optional_store_absence(fixture.hass)
    assert fixture.entries == []
    failure = RuntimeError("Core loader unavailable")
    fixture.integration.side_effect = failure
    with pytest.raises(RuntimeError) as raised:
        await fixture.module.prepare_optional_store_absence(fixture.hass)
    assert raised.value is failure
    fixture.flow.async_init.assert_awaited_once()
