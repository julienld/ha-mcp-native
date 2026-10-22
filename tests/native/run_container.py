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
from configuration_checks import (
    exercise_configuration,
    exercise_flow_helpers,
    exercise_todo,
)

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
    receipt = await exercise_helpers(checks)
    await exercise_storage_families(checks, receipt)
    await exercise_configuration(checks, receipt, BASE)
    await exercise_flow_helpers(checks, receipt)
    await exercise_todo(checks, BASE)
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
    await checks.control("configure", {})
    await checks.ready()
    result = await checks.control(
        "llm",
        {
            "tool_name": "ha_get_state",
            "arguments": {
                "entity_id": "sensor.native_fixture",
            },
        },
    )
    assert not result["error"] and "ready" in json.dumps(result), result
    checks.passed("shared_tool_exported_to_Assist")
    imported = await checks.call("ha_native_llm_tools", {"api_id": "native_fixture"})
    assert "native_fixture/NativeEcho" in json.dumps(imported), imported
    reply = await checks.call(
        "ha_native_llm_call",
        {
            "name": "native_fixture/NativeEcho",
            "arguments": {"text": "native"},
        },
    )
    assert "NATIVE" in json.dumps(reply), reply
    checks.passed("native_LLM_API_import_with_original_validator")
    await checks.control("reload", {})
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
        "  elevation: 0\n  unit_system: metric\n  time_zone: UTC\n  auth_providers:\n"
        "    - type: homeassistant\nhttp:\ninput_boolean:\n  native_light:\n"
        "    name: Native Light\ninput_button:\ninput_select:\ninput_number:\n"
        "input_text:\ninput_datetime:\ncounter:\ntimer:\nschedule:\nzone:\n"
        "person:\ntag:\nconfig:\nscript: !include scripts.yaml\n"
        "scene: !include scenes.yaml\nautomation: !include automations.yaml\n"
        "native_fixture:\n",
        encoding="utf-8",
    )
    for filename, empty in (
        ("scripts.yaml", "{}\n"),
        ("scenes.yaml", "[]\n"),
        ("automations.yaml", "[]\n"),
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
            evidence = await exercise(checks)
            # Do not persist generated identities or access/refresh tokens.
            evidence.pop("entry_id", None)
            evidence.pop("path", None)
            evidence["llm_api_count"] = len(evidence.pop("llm_apis", []))
            evidence["checks"] = checks.results
            (OUT / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    finally:
        logs = docker("logs", CONTAINER, check=False)
        (OUT / "ha.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
        docker("rm", "-f", CONTAINER, check=False)


if __name__ == "__main__":
    asyncio.run(main())
