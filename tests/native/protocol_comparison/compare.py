"""Identical reads, alternating AB/BA blocks, bounded observations, no speed gate."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import platform
import statistics
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .contracts import (
    READS,
    actor_contract,
    protocol_contract,
    read_only_contract,
    ready,
    validate_read,
)
from .evidence import scan_log, validate_runtime, write_json
from .wire import VERSION, Client, WireError, loopback_url


class Target:
    def __init__(self, http: httpx.AsyncClient, config: dict[str, Any]) -> None:
        self.http, self.config = http, config
        self.fixture_url = loopback_url(config["fixture_url"])
        fixture_origin = urlsplit(self.fixture_url)
        mcp_origin = urlsplit(loopback_url(config["mcp_url"]))
        if (fixture_origin.hostname, fixture_origin.port) != (
            mcp_origin.hostname,
            mcp_origin.port,
        ):
            raise WireError("mcp_and_fixture_must_share_core_origin")
        self.admin_token = os.environ[config["admin_token_env"]]
        self.actors = [
            {
                "client": Client(
                    http, config["mcp_url"], os.environ[actor["token_env"]]
                ),
                "expected_user_id": os.environ[actor["expected_user_env"]],
            }
            for actor in config["actors"]
        ]
        if len(self.actors) != 2:
            raise ValueError("two_actor_probes_required")
        self.authorized = False

    async def snapshot(self) -> dict[str, Any]:
        response = await self.http.get(
            self.fixture_url, headers={"Authorization": "Bearer " + self.admin_token}
        )
        response.raise_for_status()
        if len(response.content) > 256 * 1024:
            raise WireError("fixture_body_limit")
        result = response.json()
        if result.get("run_nonce") != self.config["expected"]["run_nonce"]:
            raise WireError("fixture_nonce_mismatch")
        return result

    async def policy(self, enabled: bool) -> None:
        if not self.authorized:
            raise WireError("fixture_not_attested")
        async with asyncio.timeout(150):
            response = await self.http.post(
                self.fixture_url,
                headers={"Authorization": "Bearer " + self.admin_token},
                json={"read_only": enabled},
                timeout=145,
            )
            response.raise_for_status()

    async def wait_ready(self) -> None:
        await ready(self)


async def read_once(target: Target, index: int) -> dict[str, Any]:
    name, arguments = READS[index]
    observation = await target.actors[0]["client"].rpc(
        "tools/call", {"name": name, "arguments": arguments}
    )
    valid = True
    validation_failure = None
    try:
        validate_read(index, observation)
    except WireError as error:
        valid = False
        validation_failure = str(error)
    return {
        "read_index": index,
        "valid": valid,
        "validation_failure": validation_failure,
        **observation.public(),
    }


async def sample_block(target: Target, width: int, block: int) -> dict[str, Any]:
    start = time.perf_counter()
    samples = await asyncio.gather(
        *(read_once(target, (block * width + i) % len(READS)) for i in range(width))
    )
    return {
        "wall_ms": round((time.perf_counter() - start) * 1000, 4),
        "requests": samples,
    }


def summaries(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    output = {}
    for width in (1, 4):
        output[str(width)] = {}
        for role in ("baseline", "candidate"):
            selected = [
                b for b in blocks if b["concurrency"] == width and b["role"] == role
            ]
            samples = [r for b in selected for r in b["requests"]]
            durations = sorted(r["duration_ms"] for r in samples if r["valid"])
            output[str(width)][role] = {
                "requests": len(samples),
                "errors": sum(not r["valid"] for r in samples),
                "response_media_counts": {
                    media: sum(r.get("media") == media for r in samples)
                    for media in {r.get("media", "") for r in samples}
                },
                "median_ms": statistics.median(durations) if durations else None,
                "p95_nearest_rank_ms": durations[math.ceil(len(durations) * 0.95) - 1]
                if durations
                else None,
                "valid_requests_per_second": len(durations)
                / (sum(b["wall_ms"] for b in selected) / 1000)
                if selected and sum(b["wall_ms"] for b in selected)
                else None,
            }
    return output


async def measurements(
    targets: dict[str, Target],
    rounds: int,
    warmups: int,
    report: dict[str, Any],
    output: Path,
) -> None:
    for width in (1, 4):
        for block in range(warmups):
            order = (
                ("baseline", "candidate")
                if block % 2 == 0
                else ("candidate", "baseline")
            )
            for role in order:
                result = await sample_block(targets[role], width, block)
                if not all(r["valid"] for r in result["requests"]):
                    report["warmup_failure"] = {
                        "role": role,
                        "concurrency": width,
                        **result,
                    }
                    raise WireError("warmup_read_failed")
        for block in range(rounds):
            order = (
                ("baseline", "candidate")
                if block % 2 == 0
                else ("candidate", "baseline")
            )
            for position, role in enumerate(order):
                result = await sample_block(targets[role], width, block)
                report["blocks"].append(
                    {
                        "role": role,
                        "block": block,
                        "position": position,
                        "concurrency": width,
                        **result,
                    }
                )
            write_json(output, report)


async def attest(targets: dict[str, Target], report: dict[str, Any]) -> None:
    baseline_origin = urlsplit(targets["baseline"].fixture_url)
    candidate_origin = urlsplit(targets["candidate"].fixture_url)
    if baseline_origin.port == candidate_origin.port:
        raise WireError("two_separate_core_listener_ports_required")
    for role, target in targets.items():
        snapshot = await target.snapshot()
        report["runtime"][role] = validate_runtime(snapshot, target.config["expected"])
        if snapshot["state"] != "ready" or snapshot["records"]:
            raise WireError("fixture_not_fresh")
        target.authorized = True
        report["contracts"][role] = await protocol_contract(target.actors[0]["client"])
    for field in (
        "core_image_id",
        "core_version",
        "architecture",
        "python_version",
        "private_versions",
    ):
        if (
            report["runtime"]["baseline"][field]
            != report["runtime"]["candidate"][field]
        ):
            raise WireError("paired_runtime_mismatch")
    if (
        targets["baseline"].config["expected"]["runtime"] != "legacy"
        or targets["candidate"].config["expected"]["runtime"] != "native"
    ):
        raise WireError("legacy_native_pair_required")


def report_start(rounds: int, warmups: int) -> dict[str, Any]:
    return {
        "complete": False,
        "protocol": VERSION,
        "client": "httpx standard HTTP; explicit per-request modern envelope",
        "httpx_version": httpx.__version__,
        "client_python": platform.python_version(),
        "client_architecture": platform.machine(),
        "rounds_per_concurrency": rounds,
        "warmup_blocks_per_concurrency": warmups,
        "warmups_in_measurements": False,
        "read_targets_sha256": hashlib.sha256(
            json.dumps(READS, sort_keys=True).encode()
        ).hexdigest(),
        "ordering": "AB / BA alternating paired blocks; same tools and arguments per pair",
        "performance_gate": None,
        "runtime": {},
        "contracts": {},
        "blocks": [],
        "limitations": [
            "No MRTR, images, continuations, SSE resume or disconnect qualification",
            "No causal attribution to the two independent patches",
            "No equivalence to an old release-versus-native benchmark",
            "Builder attests source origin; observed hashes verify deployed component and staged wheel",
            "Core image identity is supplied by the builder; runtime version and architecture observed",
        ],
    }


async def compare(
    config: dict[str, Any], output: Path, *, rounds: int, warmups: int
) -> bool:
    if not 2 <= rounds <= 100 or rounds % 2 or not 1 <= warmups <= 20:
        raise ValueError("bounded_even_rounds_and_positive_warmups_required")
    report = report_start(rounds, warmups)
    harness = Path(__file__).parent
    report["harness_files_sha256"] = {
        path.relative_to(harness).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(harness.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".lock"}
    }
    targets: dict[str, Target] = {}
    offsets = {
        role: (await asyncio.to_thread(Path(config[role]["server_log"]).stat)).st_size
        for role in ("baseline", "candidate")
    }
    write_json(output, report)
    try:
        async with httpx.AsyncClient(
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
        ) as http:
            targets = {
                role: Target(http, config[role]) for role in ("baseline", "candidate")
            }
            await attest(targets, report)
            await measurements(targets, rounds, warmups, report, output)
            for role, target in targets.items():
                unchanged = await target.snapshot()
                if unchanged["state"] != "ready" or unchanged["records"]:
                    raise WireError("benchmark_reads_changed_fixture")
                report["contracts"][role]["actor_and_reply"] = await actor_contract(
                    target
                )
                report["contracts"][role][
                    "read_only_policy"
                ] = await read_only_contract(target)
            report["functional_complete"] = True
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        httpx.HTTPError,
        TimeoutError,
    ) as error:
        # Payloads, paths, identities and exception messages are deliberately absent.
        report["failure"] = {
            "type": type(error).__name__,
            "category": str(error)
            if isinstance(error, WireError)
            else "harness_or_runtime_failed",
        }
    finally:
        report["last_requests"] = {
            role: [list(actor["client"].observations) for actor in target.actors]
            for role, target in targets.items()
        }
        report["summary"] = summaries(report["blocks"])
        report["server_logs"] = {}
        for role, offset in offsets.items():
            try:
                report["server_logs"][role] = scan_log(
                    Path(config[role]["server_log"]), offset
                )
            except (OSError, ValueError) as error:
                report["server_logs"][role] = {"capture_failed": type(error).__name__}
        samples_ok = bool(report["blocks"]) and all(
            r["valid"] for b in report["blocks"] for r in b["requests"]
        )
        logs_ok = all(
            "counts" in log
            and log.get("capture_markers", 0) >= 2
            and not any(log["counts"].values())
            for log in report["server_logs"].values()
        )
        report["complete"] = bool(
            report.get("functional_complete")
            and samples_ok
            and logs_ok
            and "failure" not in report
        )
        write_json(output, report)
    return report["complete"]
