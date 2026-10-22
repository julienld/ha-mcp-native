# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28.1,<0.29"]
# ///

"""Exercise the actual HACS candidate on a disposable Home Assistant Container."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import time
from pathlib import Path

import httpx
from blueprint_checks import exercise_blueprints
from blueprint_source import blueprint_source
from bulk_checks import exercise_bulk
from calendar_checks import exercise_calendars
from camera_checks import exercise_camera
from configuration_checks import (
    exercise_configuration,
    exercise_flow_helpers,
    exercise_todo,
)
from dashboard_checks import exercise_dashboard_card_types, exercise_dashboards
from device_checks import exercise_devices
from energy_checks import exercise_energy
from entity_checks import exercise_entities
from event_checks import exercise_events
from group_checks import exercise_groups
from health_checks import exercise_health
from history_checks import exercise_history
from interop_checks import exercise_llm
from log_checks import exercise_logs
from overview_checks import exercise_overview
from pipeline_checks import exercise_pipelines
from registry_checks import exercise_registries
from resource_checks import exercise_resources
from search_checks import exercise_search
from system_checks import exercise_system_actions, exercise_system_self_reload
from theme_checks import exercise_themes

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "tests/native/.ha-test/config"
OUT = ROOT / "tests/native/out"
BASE = "http://127.0.0.1:8123"
CONTAINER = "ha-mcp-native-migration"


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *arguments], check=check, capture_output=True, text=True
    )


def unpack(response: httpx.Response) -> dict:
    response.raise_for_status()
    if "text/event-stream" in response.headers.get("content-type", ""):
        messages = [
            json.loads(line[5:].strip())
            for line in response.text.splitlines()
            if line.startswith("data:")
        ]
        return next(
            message
            for message in reversed(messages)
            if "result" in message or "error" in message
        )
    return response.json()


def message(method: str, arguments: dict | None = None, *, modern: bool = True) -> dict:
    params = dict(arguments or {})
    if modern:
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}


async def wait_identities() -> dict:
    target = CONFIG / "test-identities.json"
    deadline = time.monotonic() + 240
    while not target.exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Home Assistant fixture startup")
        await asyncio.sleep(1)
    return json.loads(target.read_text())


async def login(client: httpx.AsyncClient, identity: dict) -> dict:
    client_id = BASE + "/"
    response = await client.post(
        BASE + "/auth/login_flow",
        json={
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": client_id,
        },
    )
    response.raise_for_status()
    response = await client.post(
        BASE + "/auth/login_flow/" + response.json()["flow_id"],
        json={
            "client_id": client_id,
            "username": identity["username"],
            "password": identity["password"],
        },
    )
    response.raise_for_status()
    response = await client.post(
        BASE + "/auth/token",
        data={
            "grant_type": "authorization_code",
            "code": response.json()["result"],
            "client_id": client_id,
        },
    )
    response.raise_for_status()
    return response.json()


class Checks:
    def __init__(self, client: httpx.AsyncClient, credentials: dict) -> None:
        self.client = client
        self.credentials = credentials
        self.path = ""
        self.results: list[str] = []

    def passed(self, name: str) -> None:
        self.results.append(name)
        (OUT / "evidence.json").write_text(
            json.dumps({"complete": False, "checks": self.results}, indent=2) + "\n",
            encoding="utf-8",
        )
        print("PASS", name, flush=True)

    def headers(
        self, user: str = "alpha", *, method: str = "tools/list", name: str = ""
    ) -> dict:
        result = {
            "Authorization": "Bearer " + self.credentials[user]["access_token"],
            "Accept": "application/json, text/event-stream",
            "Mcp-Method": method,
        }
        if name:
            result["Mcp-Name"] = name
        return result

    async def control(self, operation: str, body: dict | None = None) -> dict:
        url = BASE + "/api/native_fixture/" + operation
        response = (
            await self.client.get(url, headers=self.headers())
            if body is None
            else await self.client.post(url, headers=self.headers(), json=body)
        )
        response.raise_for_status()
        return response.json()

    async def ready(self) -> dict:
        deadline = time.monotonic() + 90
        while True:
            probe = await self.control("probe")
            if probe["ready"]:
                self.path = probe["path"]
                return probe
            if time.monotonic() > deadline:
                raise TimeoutError("Native entry startup")
            await asyncio.sleep(0.5)

    async def rpc(
        self, method: str, params: dict | None = None, user: str = "alpha"
    ) -> dict:
        response = await self.client.post(
            BASE + self.path,
            headers=self.headers(
                user, method=method, name=(params or {}).get("name", "")
            ),
            json=message(method, params),
        )
        return unpack(response)

    async def call(self, name: str, arguments: dict | None = None) -> dict:
        body = await self.rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        assert "error" not in body, body
        result = body["result"]
        assert not result.get("isError"), result
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(
            next(
                block["text"] for block in result["content"] if block["type"] == "text"
            )
        )


async def create_entry(checks: Checks, owner: str) -> None:
    url = BASE + "/api/config/config_entries/flow"
    response = await checks.client.post(
        url,
        headers=checks.headers(),
        json={
            "handler": "ha_mcp_tools",
            "show_advanced_options": False,
        },
    )
    response.raise_for_status()
    flow = response.json()
    url += "/" + flow["flow_id"]
    if flow["type"] == "menu":
        response = await checks.client.post(
            url, headers=checks.headers(), json={"next_step_id": "server"}
        )
        response.raise_for_status()
        flow = response.json()
    assert flow["step_id"] == "server", flow
    response = await checks.client.post(
        url,
        headers=checks.headers(),
        json={
            "native_owner_user_id": owner,
            "remote_access": "ha_auth",
        },
    )
    response.raise_for_status()
    assert response.json()["type"] == "create_entry", response.json()
    await checks.ready()


async def exercise_helpers(checks: Checks) -> dict:
    guide = await checks.call("ha_get_skill_guide")
    acknowledgment = re.search(r"Acknowledgment key: ([^\s]+)", guide["content"])
    assert acknowledgment, "The real skill guide must provide the write receipt"
    common = {"BestPracticeKey": acknowledgment[1], "MandatoryBPS": False}
    created = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "input_boolean",
            "name": "Native storage helper",
            "config": {"initial": False},
        },
    )
    assert created["success"] and created["entity_id"], created
    entity_id = created["entity_id"]
    updated = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "input_boolean",
            "helper_id": entity_id,
            "name": "Native storage helper updated",
        },
    )
    assert updated["success"] and updated["data"]["initial"] is False, updated
    assert updated["data"]["id"] == created["data"]["id"], updated
    assert list((CONFIG / ".ha_mcp/backups").glob("helper_input_boolean.*.yaml"))
    checks.passed("native_storage_helper_create_update_with_prewrite_snapshot")
    action = await checks.call(
        "ha_call_service",
        {"domain": "input_boolean", "service": "turn_on", "entity_id": entity_id},
    )
    assert action.get("success"), action
    state = await checks.call("ha_get_state", {"entity_id": entity_id})
    assert '"on"' in json.dumps(state), state
    checks.passed("shared_service_action_changes_real_Core_entity")
    flow = await checks.call(
        "ha_config_set_helper",
        {
            **common,
            "helper_type": "utility_meter",
            "name": "Native utility meter",
            "config": {"source": "sensor.native_energy", "cycle": "daily"},
        },
    )
    assert flow["success"] and flow["entry_id"] and flow["entity_ids"], flow
    checks.passed("native_helper_config_flow_on_real_Core")
    return common


async def exercise_storage_families(checks: Checks, common: dict) -> None:
    configurations = {
        "input_button": {},
        "input_select": {"options": ["one", "two"]},
        "input_number": {"min": 0, "max": 10, "initial": 1},
        "input_text": {"initial": "native"},
        "input_datetime": {
            "has_date": True,
            "has_time": True,
            "initial": "2026-10-22 01:02:00",
        },
        "counter": {"initial": 0},
        "timer": {"duration": "00:05:00"},
        "schedule": {"monday": [{"from": "08:00:00", "to": "09:00:00"}]},
        "zone": {"latitude": 1, "longitude": 1, "radius": 100},
        "person": {},
        "tag": {"tag_id": "native-contract-tag"},
    }
    failures = []
    for helper_type, config in configurations.items():
        try:
            created = await checks.call(
                "ha_config_set_helper",
                {
                    **common,
                    "helper_type": helper_type,
                    "name": f"Native {helper_type}",
                    "config": config,
                },
            )
            assert created["success"] and created["data"]["id"], created
            updated = await checks.call(
                "ha_config_set_helper",
                {
                    **common,
                    "helper_type": helper_type,
                    "helper_id": created["data"]["id"],
                    "name": f"Native {helper_type} updated",
                },
            )
            assert updated["success"], updated
            checks.passed(f"native_{helper_type}_storage_create_update")
        except (AssertionError, httpx.HTTPError) as error:
            failures.append({"helper_type": helper_type, "failure": str(error)[:2000]})
    assert not failures, failures


async def exercise_external_clients(checks: Checks, user_id: str) -> None:
    source = (ROOT / "tests/native/legacy_client_probe.py").read_text(encoding="utf-8")
    # This child is an actual external client. The HA process itself retains
    # the import and loopback guards throughout these network requests.
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "docker",
            "exec",
            "-i",
            CONTAINER,
            "python",
            "-c",
            source,
        ],
        input=json.dumps(
            {
                "url": BASE + checks.path,
                "base_url": BASE,
                "token": checks.credentials["alpha"]["access_token"],
                "user_id": user_id,
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    checks.passed("unchanged_Core_SDK1_client_calls_modern_native_endpoint")
    checks.passed("shared_tool_bodies_use_real_remote_REST_and_WebSocket")


async def exercise_standard_proxy(checks: Checks) -> None:
    source = (ROOT / "tests/native/component_proxy_probe.py").read_text(
        encoding="utf-8"
    )
    result = await asyncio.to_thread(
        subprocess.run,
        ["docker", "exec", "-i", CONTAINER, "python", "-c", source],
        input=json.dumps(
            {
                "url": BASE + checks.path,
                "token": checks.credentials["alpha"]["access_token"],
            }
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    checks.passed(
        "standard_MCP_bridge_calls_canonical_Core_and_relays_catalog_resources"
    )


async def exercise_domain_tools(checks: Checks) -> None:
    receipt = await exercise_helpers(checks)
    await exercise_storage_families(checks, receipt)
    await exercise_configuration(checks, receipt, BASE)
    await exercise_registries(checks, BASE)
    await exercise_flow_helpers(checks, receipt)
    await exercise_todo(checks, BASE)
    await exercise_calendars(checks, BASE)
    await exercise_devices(checks, BASE)
    await exercise_entities(checks, BASE)
    await exercise_bulk(checks)
    await exercise_groups(checks, BASE, receipt)
    container = json.loads(docker("inspect", CONTAINER).stdout)[0]
    gateway = container["NetworkSettings"]["Networks"]["bridge"]["Gateway"]
    with blueprint_source(gateway) as source_url:
        await exercise_blueprints(checks, BASE, source_url)
    await exercise_events(checks)
    await exercise_camera(checks)
    await exercise_dashboards(checks, BASE)
    await exercise_resources(checks, BASE)
    await exercise_dashboard_card_types(checks)
    await exercise_search(checks, receipt, BASE)
    await exercise_history(checks)
    await exercise_energy(checks)
    await exercise_system_actions(checks)
    await exercise_pipelines(checks)
    await exercise_logs(checks)
    await exercise_themes(checks)
    await exercise_health(checks)
    await exercise_overview(checks, BASE)


async def exercise(checks: Checks) -> dict:
    probe = await checks.ready()
    assert probe["manager_type"] == "NativeServerManager", probe
    assert "admin_token" not in probe["entry_data_keys"], probe
    checks.passed("new_native_entry_without_internal_admin_token")
    assert checks.path.startswith("/api/webhook/"), checks.path
    original_path = checks.path
    catalog = (await checks.rpc("tools/list"))["result"]["tools"]
    assert {"ha_get_state", "ha_list_services"} <= {tool["name"] for tool in catalog}
    checks.passed("existing_webhook_URL_and_modern_MCP")
    await checks.control("guard", {})
    state = await checks.call("ha_get_state", {"entity_id": "sensor.native_fixture"})
    assert "ready" in json.dumps(state), state
    services = await checks.call("ha_list_services", {"domain": "input_boolean"})
    assert "input_boolean" in json.dumps(services), services
    checks.passed("shared_state_and_service_tools_on_Core")
    await exercise_domain_tools(checks)
    for headers in ({}, checks.headers("reader")):
        response = await checks.client.post(
            BASE + checks.path, headers=headers, json=message("tools/list")
        )
        # Existing HA-auth webhook deliberately uses the same challenge for an
        # invalid credential and an authenticated user without admin access.
        assert response.status_code in (401, 403), response.status_code
    checks.passed("missing_and_non_admin_identity_rejected")
    legacy_headers = checks.headers()
    legacy_headers.pop("Mcp-Method", None)
    response = await checks.client.post(
        BASE + checks.path,
        headers=legacy_headers,
        json=message(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "native-legacy-ci", "version": "1"},
            },
            modern=False,
        ),
    )
    assert "result" in unpack(response)
    checks.passed("legacy_initialize")
    await exercise_system_self_reload(checks)
    await exercise_llm(checks)
    final = await checks.ready()
    assert checks.path == original_path
    assert final["listeners"] == [8123], final
    assert not final["server_threads"] and not final["remote_imports"], final
    assert final["owned_tasks"] == 0 and final["loopback_attempts"] == 0, final
    assert final["ticks"] > probe["ticks"], final
    checks.passed("reload_single_listener_no_server_thread_remote_import_or_loopback")
    return final


async def main() -> None:
    CONFIG.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    preferences = CONFIG / ".ha_mcp"
    preferences.mkdir(exist_ok=True)
    (preferences / "feature_flags.json").write_text(
        json.dumps(
            {
                "enable_tool_search": False,
                "enable_mandatory_bps": True,
                "enable_strict_mandatory_bps": True,
            }
        ),
        encoding="utf-8",
    )
    (preferences / "backup_settings.json").write_text(
        json.dumps({"enable_auto_backup": True}), encoding="utf-8"
    )
    (CONFIG / "configuration.yaml").write_text(
        "homeassistant:\n  name: Native migration CI\n  latitude: 0\n  longitude: 0\n"
        "  elevation: 0\n  unit_system: metric\n  time_zone: UTC\n"
        "  packages:\n    native_search:\n      script:\n        native_search_yaml:\n"
        "          alias: Native YAML Search\n          sequence:\n"
        "            - variables:\n                native_search_marker: native_yaml_probe\n"
        "                fixture_secret: !secret native_search_secret\n"
        "                fixture_pin: !secret native_search_pin\n  auth_providers:\n"
        "    - type: homeassistant\nhttp:\ninput_boolean:\n  native_light:\n"
        "    name: Native Light\ninput_button:\ninput_select:\ninput_number:\n"
        "input_text:\ninput_datetime:\ncounter:\ntimer:\nschedule:\nzone:\n"
        "person:\ntag:\ngroup: {}\nconfig:\nscript: !include scripts.yaml\n"
        "scene: !include scenes.yaml\nautomation: !include automations.yaml\n"
        "lovelace:\n  mode: storage\n  dashboards:\n    native-yaml-search:\n"
        "      mode: yaml\n      title: Native YAML Search\n      show_in_sidebar: false\n"
        "      filename: native-search-dashboard.yaml\n"
        "recorder:\n  commit_interval: 1\nhistory:\nenergy:\nconversation:\nassist_pipeline:\n"
        "system_log:\nlogger:\nlogbook:\nsystem_health:\ndiagnostics:\n"
        "frontend:\n  themes:\n    native_fixture_blue:\n      primary-color: '#2255aa'\n"
        "    native_fixture_night:\n      primary-color: '#112244'\nnative_fixture:\n"
        "camera:\n  - platform: native_fixture\n",
        encoding="utf-8",
    )
    for filename, empty in (
        ("scripts.yaml", "{}\n"),
        ("scenes.yaml", "[]\n"),
        ("automations.yaml", "[]\n"),
        (
            "secrets.yaml",
            "native_search_secret: native-fixture-secret-probe\nnative_search_pin: 91827364\n",
        ),
        (
            "native-search-dashboard.yaml",
            "views:\n  - title: native_yaml_dashboard_probe\n    cards: []\n",
        ),
    ):
        (CONFIG / filename).write_text(empty, encoding="utf-8")
    docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        "127.0.0.1:8123:8123",
        "-v",
        f"{CONFIG}:/config",
        "--entrypoint",
        "/bin/sh",
        "ha-mcp-native-candidate",
        "-c",
        "mkdir -p /config/custom_components && cp -r /opt/native/custom_components/* "
        "/config/custom_components/ && exec python -m homeassistant --config /config",
    )
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            identities = await wait_identities()
            credentials = {
                name: await login(client, identity)
                for name, identity in identities.items()
            }
            checks = Checks(client, credentials)
            checks.passed("real_HA_authorization_code_flow")
            await create_entry(checks, identities["alpha"]["user_id"])
            await checks.control("guard", {})
            await exercise_external_clients(checks, identities["alpha"]["user_id"])
            await exercise_standard_proxy(checks)
            evidence = await exercise(checks)
            # Do not persist generated identities or access/refresh tokens.
            evidence.pop("entry_id", None)
            evidence.pop("path", None)
            evidence["llm_api_count"] = len(evidence.pop("llm_apis", []))
            evidence["checks"] = checks.results
            evidence["complete"] = True
            (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        logs = docker("logs", CONTAINER, check=False)
        (OUT / "ha.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        docker("rm", "-f", CONTAINER, check=False)


if __name__ == "__main__":
    asyncio.run(main())
