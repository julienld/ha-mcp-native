# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Runs inside the isolated container; uses the existing native MCP client."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).with_name("native")))
import run_container as bench
from checks import (
    REPOSITORY_ID,
    Control,
    exercise_hacs_owner,
    hacs_call,
    wait_probe,
    ws_cache,
)

bench.CONFIG = Path("/config")
bench.OUT = Path("/qualification-evidence")


async def add_tools_entry(checks: Any) -> None:
    response = await checks.client.post(
        bench.BASE + "/api/config/config_entries/flow",
        headers=checks.headers(),
        json={"handler": "ha_mcp_tools"},
    )
    response.raise_for_status()
    url = bench.BASE + "/api/config/config_entries/flow/" + response.json()["flow_id"]
    response = await checks.client.post(
        url, headers=checks.headers(), json={"next_step_id": "tools"}
    )
    response.raise_for_status()
    if response.json()["type"] == "form":
        response = await checks.client.post(url, headers=checks.headers(), json={})
        response.raise_for_status()
    assert response.json()["type"] == "create_entry", response.json()


async def after_restart(checks: Any, control: Control) -> dict[str, Any]:
    await checks.ready()
    restored = await wait_probe(control, lambda p: p["ready"])
    assert restored["repository"]["data"]["installed_version"] == "2.0.0", restored
    assert not restored["repository"]["pending_restart"], restored
    await control("load_dummy", {})
    active = await control("probe")
    assert active["dummy_running_version"] == "2.0.0", active
    assert not active["install_services"], active
    checks.passed("real_process_restart_loads_installed_dummy_version_without_replay")
    await ws_cache(bench.BASE, checks.credentials["alpha"]["access_token"])
    removed = await hacs_call(
        checks, "ha_manage_hacs", {"action": "remove", "repository_id": REPOSITORY_ID}
    )
    assert removed["repository_id"] == REPOSITORY_ID, removed
    assert removed["restart_requested"] is False, removed
    after = await control("probe")
    assert not after["repository"]["data"]["installed"], after
    assert not any(path.startswith("custom_components/") for path in after["disk"]), (
        after
    )
    assert after["dummy_running_version"] == "2.0.0", after
    assert not after["unexpected_network"], after["unexpected_network"]
    checks.passed(
        "real_owner_uninstall_removes_files_and_keeps_loaded_module_until_restart"
    )
    return after


async def main(phase: str, topology: str) -> None:
    bench.OUT.mkdir(exist_ok=True)
    evidence_path = bench.OUT / "evidence.json"
    previous = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
    evidence: dict[str, Any] = {
        **previous,
        "complete": False,
        "phase": phase,
        "topology": topology,
        "network": "Docker --network none; HTTP content fixtures on container loopback",
        "subject": "real Core/HACS owner with dummy integration; not HA-MCP self-update",
        "candidate": json.loads(
            await asyncio.to_thread(
                Path("/opt/qualification/native-build.json").read_text
            )
        ),
        "architecture": platform.machine(),
        "publication_chain_qualified": False,
        "ha_mcp_self_update_qualified": False,
        "marketplace_qualified": False,
    }
    checks = None
    try:
        async with httpx.AsyncClient(timeout=90, trust_env=False) as client:
            identities = await bench.wait_identities()
            # Existence of retained identities is not HTTP readiness on restart.
            async with asyncio.timeout(180):
                while True:
                    try:
                        response = await client.get(bench.BASE + "/")
                        if response.status_code < 500:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.5)
            credentials = {
                name: await bench.login(client, identity)
                for name, identity in identities.items()
            }
            checks = bench.Checks(client, credentials)
            checks.results = previous.get("checks", [])

            async def control(
                operation: str, body: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                url = bench.BASE + "/api/qualification_hacs/" + operation
                response = (
                    await client.get(url, headers=checks.headers())
                    if body is None
                    else await client.post(url, headers=checks.headers(), json=body)
                )
                response.raise_for_status()
                result = response.json()
                if operation == "probe":
                    evidence["last_owner_probe"] = result
                return result

            if phase == "exercise":
                await bench.create_entry(checks, identities["alpha"]["user_id"])
                if topology == "both":
                    await add_tools_entry(checks)
                evidence["before_process_restart"] = await exercise_hacs_owner(
                    checks,
                    control,
                    base=bench.BASE,
                    admin_id=identities["alpha"]["user_id"],
                )
            else:
                evidence["after_process_restart"] = await after_restart(checks, control)
                evidence["complete"] = True
    finally:
        # Only generated qualification observations, never identities, tokens,
        # config entries or .storage/auth are exported by the host runner.
        evidence["checks"] = checks.results if checks else previous.get("checks", [])
        evidence_path.write_text(
            json.dumps(evidence, indent=2, default=str) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("exercise", "after-restart"))
    parser.add_argument(
        "--topology", choices=("server-only", "both"), default="server-only"
    )
    options = parser.parse_args()
    asyncio.run(main(options.phase, options.topology))
