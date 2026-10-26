# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx>=0.28.1,<0.29", "requests>=2.32,<3", "pytest>=8,<10", "testcontainers>=4.13,<5", "PyJWT>=2.10,<3"]
# ///

"""Launch the continuity legacy/native pair and run the modern protocol CLI.

Run only against two disposable clean checkouts, on a local Docker daemon.
Application files are never overlaid. Worker stdout and container logs are private.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol_comparison.evidence import PROTECTED, build_receipt, git, write_json
from protocol_comparison.protocol_fixture.provenance import file_digest

BASELINE = "3690af6e0f62ccec6c7fa70f748494ba2605f419"
DEFAULT_IMAGE = "ghcr.io/home-assistant/home-assistant:2026.10.4"
LABEL = "ha-mcp.protocol-comparison"
ROLES = {"baseline": "legacy", "candidate": "native"}
OVERLAYS = (
    "tests/src/e2e/_conftest_embedded.py",
    "tests/src/e2e/_conftest_native_runtime.py",
    "tests/src/e2e/_conftest_testcontainer.py",
    "tests/src/e2e/_conftest_seed.py",
    "tests/qualification_artifacts.py",
)


def private_environment() -> dict[str, str]:
    """Do not pass household tokens, proxy settings or CI credentials to children."""
    allowed = {
        "PATH",
        "HOME",
        "USERPROFILE",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LOCALAPPDATA",
        "APPDATA",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_EXCLUDE_NEWER",
    }
    result = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    result.update(
        PYTHONUNBUFFERED="1",
        PYTHONDONTWRITEBYTECODE="1",
        TESTCONTAINERS_RYUK_DISABLED="true",
    )
    return result


def command(*args: str, timeout: int = 120) -> str:
    return subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=private_environment(),
    ).stdout.strip()


def source_proof(source: Path, revision: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("full_source_sha_required")
    if git(source, "rev-parse", "HEAD") != revision:
        raise ValueError("source_sha_mismatch")
    if git(source, "status", "--porcelain", "--untracked-files=all", "--", *PROTECTED):
        raise ValueError("dirty_application_source")
    return {name: git(source, "rev-parse", f"HEAD:{name}") for name in PROTECTED}


def overlay_continuity(harness: Path, source: Path) -> dict[str, str]:
    """Reuse continuity's exact helpers in each role's separate Python process."""
    hashes = {}
    paths = [harness / name for name in OVERLAYS]
    paths.extend((harness / "tests/fixtures/native_qualification").rglob("*.py"))
    paths.extend((harness / "tests/fixtures/native_qualification").rglob("*.json"))
    for origin in paths:
        relative = origin.relative_to(harness)
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
        hashes[relative.as_posix()] = file_digest(destination)
    return hashes


def seed_auth(config: Path) -> list[dict[str, str]]:
    """Seed actual Core auth records, with two fresh human administrators.

    No imported production credential and no auth bypass. Core verifies these
    signed, run-local LLATs; the component still enforces its own actor admission.
    """
    import jwt

    now = datetime.now(UTC)
    users, refresh_tokens, actors = [], [], []
    for index in range(2):
        user_id, token_id, key = (
            secrets.token_hex(16),
            secrets.token_hex(16),
            secrets.token_hex(64),
        )
        users.append(
            {
                "id": user_id,
                "group_ids": ["system-admin"],
                "is_owner": index == 0,
                "is_active": True,
                "name": f"Protocol actor {index + 1}",
                "system_generated": False,
                "local_only": False,
            }
        )
        refresh_tokens.append(
            {
                "id": token_id,
                "user_id": user_id,
                "client_id": None,
                "client_name": f"Disposable protocol actor {index + 1}",
                "client_icon": None,
                "token_type": "long_lived_access_token",
                "created_at": now.isoformat(),
                "access_token_expiration": 7200.0,
                "token": secrets.token_hex(64),
                "jwt_key": key,
                "last_used_at": None,
                "last_used_ip": None,
                "expire_at": None,
                "credential_id": None,
                "version": "2026.10.0",
            }
        )
        token = jwt.encode(
            {
                "iss": token_id,
                "iat": int(now.timestamp()),
                "exp": int(now.timestamp()) + 7200,
            },
            key,
            algorithm="HS256",
        )
        actors.append({"user_id": user_id, "token": token})
    store(
        config,
        "auth",
        {
            "users": users,
            "refresh_tokens": refresh_tokens,
            "credentials": [],
            "groups": [
                {"id": "system-admin", "name": "Administrators"},
                {"id": "system-users", "name": "Users"},
                {"id": "system-read-only", "name": "Read Only"},
            ],
        },
    )
    store(
        config,
        "onboarding",
        {"done": ["user", "core_config", "integration", "analytics"]},
    )
    return actors


def store(config: Path, key: str, data: dict[str, Any]) -> None:
    write_json(
        config / ".storage" / key,
        {"version": 1, "minor_version": 1, "key": key, "data": data},
    )


def configure_entry(config: Path, runtime: str, actors: list[dict[str, str]]) -> str:
    path = config / ".storage/core.config_entries"
    entries = json.loads(path.read_bytes())
    entry = next(
        e for e in entries["data"]["entries"] if e["data"].get("entry_type") == "server"
    )
    webhook = "mcp_protocol_" + secrets.token_hex(32)
    entry["data"].update(
        webhook_id=webhook,
        secret_path="/private_" + secrets.token_hex(32),
        access_enabled=True,
    )
    entry["options"].update(
        runtime_mode=runtime,
        enable_webhook=True,
        webhook_auth="ha_auth" if runtime == "native" else "none",
        enable_llm_api=False,
        enable_native_llm_import=False,
        enable_sidebar_panel=False,
        access_off_after_restart=False,
    )
    if runtime == "native":
        entry["data"].pop("admin_token", None)
        entry["data"]["native_owner_user_id"] = actors[0]["user_id"]
    else:
        entry["data"]["admin_token"] = actors[0]["token"]
    write_json(path, entries)
    write_json(
        config / ".ha_mcp/feature_flags.json",
        {
            "read_only_mode": False,
            "enable_dev_mode": False,
            "enable_mandatory_bps": False,
            "enable_strict_mandatory_bps": False,
            "enable_tool_search": False,
            "enabled_tool_modules": "all",
            "enable_tool_security_policies": False,
        },
    )
    write_json(config / ".ha_mcp/backup_settings.json", {"enable_auto_backup": False})
    return webhook


def stage_role(
    source: Path, config: Path, runtime: str
) -> tuple[Path, list[dict[str, str]], str]:
    # The modules' __file__ resolves inside that role's source checkout, so the
    # legacy wheel builder cannot accidentally build the harness/candidate tree.
    sys.path[:0] = [str(source / "tests/src"), str(source / "tests")]
    from e2e._conftest_embedded import (
        _build_embedded_server_wheel,
        _install_embedded_server,
    )
    from e2e._conftest_native_runtime import stage_native_runtime
    from e2e._conftest_seed import _install_custom_component, pair_component_library

    config.mkdir(parents=True)
    store(config, "core.config_entries", {"entries": []})
    actors = seed_auth(config)
    (config / "configuration.yaml").write_text(
        "homeassistant:\n  name: Disposable protocol comparison\n  latitude: 0\n"
        "  longitude: 0\n  elevation: 0\n  unit_system: metric\n  time_zone: UTC\n"
        "  auth_providers:\n    - type: homeassistant\n"
        "http:\napi:\nwebsocket_api:\nconfig:\nlogger:\n  default: warning\n"
        "protocol_fixture:\n",
        encoding="utf-8",
    )
    if runtime == "native":
        wheel = stage_native_runtime(source, config)
    else:
        if not _install_custom_component(
            config,
            source / "custom_components/ha_mcp_tools",
            "ha_mcp_tools",
            "HA-MCP",
            seed_entry=False,
        ):
            raise ValueError("baseline_component_absent")
        wheel = _build_embedded_server_wheel(config)
        pair_component_library(config, wheel.name)
    _install_embedded_server(config, wheel.name)
    webhook = configure_entry(config, runtime, actors)
    shutil.copytree(
        Path(__file__).parent / "protocol_fixture",
        config / "custom_components/protocol_fixture",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return wheel, actors, webhook


def constrain_container(container: Any, network: str, name: str, nonce: str) -> None:
    """Retain continuity's installer, but never its privileged/wildcard defaults."""
    # testcontainers.with_bind_ports accepts only an integer host port. Docker's
    # supported (host-IP, dynamic-port) binding must be supplied through ports.
    container.ports = {"8123/tcp": ("127.0.0.1", None)}
    container.with_name(name)
    container.with_kwargs(
        **{
            **container._kwargs,
            "privileged": False,
            "network": network,
            "nano_cpus": 2_000_000_000,
            "mem_limit": "2g",
            "memswap_limit": "2g",
            "security_opt": ["no-new-privileges:true"],
            "labels": {LABEL: nonce},
        }
    )


def observed_container(attrs: dict[str, Any], image: str) -> int:
    bindings = attrs["NetworkSettings"]["Ports"]
    published = bindings.get("8123/tcp") or []
    if (
        attrs["Image"] != image
        or attrs["HostConfig"]["Privileged"]
        or len(published) != 1
        or published[0]["HostIp"] != "127.0.0.1"
        or any(value for port, value in bindings.items() if port != "8123/tcp")
    ):
        raise ValueError("container_image_or_isolation_mismatch")
    port = int(published[0]["HostPort"])
    if not 0 < port < 65536:
        raise ValueError("invalid_container_port")
    return port


def wait_fixture(
    url: str, token: str, expected: dict[str, Any], process: subprocess.Popen[Any]
) -> None:
    import httpx

    from protocol_comparison.evidence import validate_runtime

    deadline = time.monotonic() + 900
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as http:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("continuous_log_capture_stopped")
            try:
                response = http.get(url, headers={"Authorization": "Bearer " + token})
                if response.status_code == 200:
                    observed = response.json()
                    if observed.get("running"):
                        validate_runtime(observed, expected)
                        return
            except httpx.TransportError:
                pass
            time.sleep(1)
    raise TimeoutError("fixture_not_ready")


def worker(args: argparse.Namespace) -> int:
    os.environ["E2E_EMBEDDED_RUNTIME"] = ROLES[args.role]
    os.environ["HA_TEST_IMAGE"] = args.image
    source, directory = args.source, args.directory
    write_json(directory / "status.json", {"phase": "building_artifacts"})
    wheel, actors, webhook = stage_role(source, directory / "config", ROLES[args.role])
    expected = build_receipt(
        source,
        args.revision,
        directory / "config/custom_components/ha_mcp_tools",
        wheel,
        args.image,
        args.nonce,
    )
    write_json(directory / "config/protocol-build.json", expected)
    expected["runtime"] = ROLES[args.role]
    from e2e._conftest_testcontainer import _build_ha_testcontainer

    name = f"ha-mcp-protocol-{args.nonce}-{args.role}"
    network = name + "-net"
    command("docker", "network", "create", "--label", f"{LABEL}={args.nonce}", network)
    container = _build_ha_testcontainer(directory / "config", True, wheel.name)
    constrain_container(container, network, name, args.nonce)
    capture = None
    try:
        write_json(directory / "status.json", {"phase": "starting_container"})
        container.start()
        wrapped = container.get_wrapped_container()
        wrapped.reload()
        port = observed_container(wrapped.attrs, args.image)
        with (directory / "server.log").open("wb", buffering=0) as log:
            capture = subprocess.Popen(
                ["docker", "logs", "--follow", name],
                stdout=log,
                stderr=subprocess.STDOUT,
                env=private_environment(),
            )
            origin = f"http://127.0.0.1:{port}"
            fixture_url = origin + "/api/protocol_fixture"
            write_json(
                directory / "status.json", {"phase": "waiting_for_runtime_attestation"}
            )
            wait_fixture(fixture_url, actors[0]["token"], expected, capture)
            ready = {
                "mcp_url": origin + "/api/webhook/" + webhook,
                "fixture_url": fixture_url,
                "expected": expected,
                "server_log": str(directory / "server.log"),
                "actors": actors,
            }
            temporary = directory / "ready.tmp"
            write_json(temporary, ready)
            temporary.replace(directory / "ready.json")
            write_json(directory / "status.json", {"phase": "ready"})
            # stdin is the controller's lifetime lease, not a public service.
            sys.stdin.buffer.read(1)
            if capture.poll() is not None:
                raise RuntimeError("continuous_log_capture_stopped")
        return 0
    finally:
        if capture is not None:
            stop_process(capture)
        container.stop()


def run_worker(args: argparse.Namespace) -> int:
    try:
        result = worker(args)
        write_json(args.directory / "status.json", {"phase": "stopped"})
        return result
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- private worker boundary
        path = args.directory / "status.json"
        status = (
            json.loads(path.read_bytes())
            if path.exists()
            else {"phase": "worker_start"}
        )
        write_json(
            path, {"phase": status["phase"], "failure_type": type(error).__name__}
        )
        return 1


def stop_process(process: subprocess.Popen[Any], *, group: bool = False) -> None:
    if process.poll() is None:
        if group and os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if group and os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=15)


def wait_workers(
    workers: dict[str, subprocess.Popen[Any]], private: Path, timeout: float = 1200
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(p.poll() is not None for p in workers.values()):
            raise RuntimeError("fixture_worker_failed")
        if all((private / role / "ready.json").exists() for role in ROLES):
            return {
                role: json.loads((private / role / "ready.json").read_bytes())
                for role in ROLES
            }
        time.sleep(0.25)
    raise TimeoutError("paired_bringup_timeout")


def comparison_config(ready: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    config, environment = {}, private_environment()
    for role, value in ready.items():
        prefix = "PROTOCOL_" + role.upper()
        actors = value["actors"]
        if (
            len(actors) != 2
            or actors[0]["user_id"] == actors[1]["user_id"]
            or actors[0]["token"] == actors[1]["token"]
        ):
            raise ValueError("distinct_disposable_actors_required")
        actor_config = []
        for index, actor in enumerate(actors):
            token_key, owner_key = f"{prefix}_TOKEN_{index}", f"{prefix}_USER_{index}"
            environment[token_key] = actor["token"]
            environment[owner_key] = (
                actors[0]["user_id"] if role == "baseline" else actor["user_id"]
            )
            actor_config.append(
                {"token_env": token_key, "expected_user_env": owner_key}
            )
        config[role] = {
            key: value[key]
            for key in ("mcp_url", "fixture_url", "expected", "server_log")
        }
        config[role].update(admin_token_env=f"{prefix}_TOKEN_0", actors=actor_config)
    return config, environment


def remove_resources(nonce: str) -> None:
    failed = False
    for kind, listing in (
        ("container", ["ps", "-aq"]),
        ("network", ["network", "ls", "-q"]),
    ):
        try:
            identifiers = command(
                "docker", *listing, "--filter", f"label={LABEL}={nonce}"
            ).splitlines()
        except (OSError, subprocess.SubprocessError):
            failed = True
            continue
        for identifier in identifiers:
            try:
                # IDs come from a label-scoped daemon query, never shell input.
                if kind == "container":
                    command("docker", "rm", "--force", "--volumes", identifier)
                else:
                    command("docker", "network", "rm", identifier)
            except (OSError, subprocess.SubprocessError):
                failed = True
    if failed:
        raise RuntimeError("cleanup_incomplete")


def cleanup(state_path: Path) -> bool:
    """Idempotent fallback also used by Actions after cancellation/worker failure."""
    if not state_path.exists():
        return True
    state = json.loads(state_path.read_bytes())
    nonce, private = state["nonce"], Path(state["private"])
    if (
        not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or private.name != "ha-mcp-protocol-" + nonce
        or not private.is_absolute()
        or private.is_symlink()
    ):
        raise ValueError("invalid_cleanup_scope")
    remove_resources(nonce)
    if private.exists():
        if os.name != "nt" and state.get("image"):
            # HA runs as root. Repair ownership only within this run's private
            # mount, without mounting the repository or the Docker socket.
            program = (
                "import os; from pathlib import Path; root=Path('/cleanup'); "
                f"uid,gid={os.getuid()},{os.getgid()}; "
                "[os.chown(p,uid,gid,follow_symlinks=False) for p in [root,*root.rglob('*')]]"
            )
            command(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--label",
                f"{LABEL}={nonce}",
                "--entrypoint",
                "python3",
                "--mount",
                f"type=bind,src={private},dst=/cleanup",
                state["image"],
                "-c",
                program,
            )
        shutil.rmtree(private)
    state_path.unlink()
    return True


def run_pair(
    args: argparse.Namespace,
    sources: dict[str, Path],
    revisions: dict[str, str],
    private: Path,
    image: str,
    nonce: str,
    workers: dict[str, subprocess.Popen[Any]],
    report: dict[str, Any],
) -> None:
    with ExitStack() as stack:
        for role, source in sources.items():
            directory = private / role
            directory.mkdir(mode=0o700)
            log = stack.enter_context((directory / "worker.log").open("wb"))
            workers[role] = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "worker",
                    "--role",
                    role,
                    "--source",
                    str(source),
                    "--revision",
                    revisions[role],
                    "--directory",
                    str(directory),
                    "--image",
                    image,
                    "--nonce",
                    nonce,
                ],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=private_environment(),
                start_new_session=os.name != "nt",
            )
        report["phase"] = "waiting_for_both_runtimes"
        ready = wait_workers(workers, private)
        config, environment = comparison_config(ready)
        write_json(private / "comparison-config.json", config)
        report["source_trees_after_build"] = {
            role: source_proof(path, revisions[role]) for role, path in sources.items()
        }
        with (private / "comparison-cli.log").open("wb") as log:
            report["phase"] = "protocol_comparison"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("run.py")),
                    "compare",
                    "--config",
                    str(private / "comparison-config.json"),
                    "--output",
                    str(args.output / "comparison.json"),
                    "--rounds",
                    "60",
                    "--warmups",
                    "2",
                ],
                check=False,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=1200,
            )
        report["comparison_exit_code"] = result.returncode
        report["source_trees_after_run"] = {
            role: source_proof(path, revisions[role]) for role, path in sources.items()
        }
        for process in workers.values():
            assert process.stdin is not None
            process.stdin.close()
        report["workers_stopped_cleanly"] = all(
            p.wait(timeout=45) == 0 for p in workers.values()
        )
        comparison = json.loads((args.output / "comparison.json").read_bytes())
        report["complete"] = bool(
            result.returncode == 0
            and comparison["complete"]
            and report["workers_stopped_cleanly"]
        )


def finish_run(
    workers: dict[str, subprocess.Popen[Any]],
    state: Path,
    report: dict[str, Any],
) -> None:
    for process in workers.values():
        try:
            stop_process(process, group=True)
        except (OSError, subprocess.SubprocessError):
            report["worker_cleanup_failed"] = True
    try:
        if state.exists():
            private = Path(json.loads(state.read_bytes())["private"])
            report["worker_status"] = {}
            for role in ROLES:
                path = private / role / "status.json"
                if path.exists():
                    status = json.loads(path.read_bytes())
                    report["worker_status"][role] = {
                        k: status[k] for k in ("phase", "failure_type") if k in status
                    }
    except (OSError, ValueError, KeyError):
        report["worker_diagnostics_incomplete"] = True
    try:
        report["cleanup_complete"] = cleanup(state)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        report["cleanup_complete"] = False
    report["complete"] = bool(
        report["complete"]
        and report["cleanup_complete"]
        and not report.get("worker_cleanup_failed")
    )


def launch(args: argparse.Namespace) -> int:
    harness = Path(__file__).resolve().parents[3]
    sources = {
        "baseline": args.baseline_source.resolve(),
        "candidate": args.candidate_source.resolve(),
    }
    revisions = {"baseline": BASELINE, "candidate": args.candidate_revision}
    if sources["baseline"] == sources["candidate"] or harness in sources.values():
        raise ValueError("separate_disposable_source_checkouts_required")
    if args.state.exists():
        raise ValueError("cleanup_previous_run_before_reusing_state")
    report: dict[str, Any] = {
        "complete": False,
        "cleanup_complete": False,
        "baseline_revision": BASELINE,
        "candidate_revision": args.candidate_revision,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("launch.json", "comparison.json"):
        if (args.output / name).exists():
            raise ValueError("fresh_report_directory_required")
    workers: dict[str, subprocess.Popen[Any]] = {}
    try:
        report["phase"] = "source_admission"
        report["source_trees_before"] = {
            role: source_proof(path, revisions[role]) for role, path in sources.items()
        }
        report["phase"] = "core_image"
        host = json.loads(
            command(
                "docker",
                "context",
                "inspect",
                "--format",
                "{{json .Endpoints.docker.Host}}",
            )
        )
        if not host.startswith(("unix://", "npipe://")):
            raise ValueError("local_docker_daemon_required")
        command("docker", "pull", args.core_image, timeout=600)
        image = command(
            "docker", "image", "inspect", args.core_image, "--format", "{{.Id}}"
        )
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("immutable_image_required")
        report["core_image_id"] = image
        report["phase"] = "continuity_staging"
        report["continuity_helpers_sha256"] = {
            role: overlay_continuity(harness, path) for role, path in sources.items()
        }
        nonce = secrets.token_hex(16)
        private = Path(tempfile.gettempdir()).resolve() / ("ha-mcp-protocol-" + nonce)
        write_json(
            args.state, {"nonce": nonce, "private": str(private), "image": image}
        )
        args.state.chmod(0o600)
        private.mkdir(mode=0o700)
        run_pair(args, sources, revisions, private, image, nonce, workers, report)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 -- redact all CLI failures
        # Arbitrary exceptions can contain tokens, URLs and entire HTTP bodies.
        report["failure"] = {
            "type": type(error).__name__,
            "category": "launch_or_runtime_failed",
        }
    finally:
        finish_run(workers, args.state, report)
        write_json(args.output / "launch.json", report)
    print(
        "Protocol qualification completed"
        if report["complete"]
        else "Protocol qualification failed; see sanitized JSON"
    )
    return 0 if report["complete"] else 1


def cancel_run(*_: Any) -> None:
    raise KeyboardInterrupt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    for name in ("baseline-source", "candidate-source", "output", "state"):
        run.add_argument("--" + name, type=Path, required=True)
    run.add_argument(
        "--candidate-revision",
        default=os.environ.get("GITHUB_SHA"),
        required=not bool(os.environ.get("GITHUB_SHA")),
    )
    run.add_argument("--core-image", default=DEFAULT_IMAGE)
    clean = commands.add_parser("cleanup")
    clean.add_argument("--state", type=Path, required=True)
    child = commands.add_parser("worker", help="internal disposable worker")
    child.add_argument("--role", choices=list(ROLES), required=True)
    for name in ("source", "directory"):
        child.add_argument("--" + name, type=Path, required=True)
    for name in ("revision", "image", "nonce"):
        child.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if args.command == "worker":
        return run_worker(args)
    if args.command == "cleanup":
        try:
            return 0 if cleanup(args.state) else 1
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            print("Protocol cleanup failed; private state retained for retry")
            return 1
    # Cancellation uses the same cleanup path as a failing comparison.
    signal.signal(signal.SIGTERM, cancel_run)
    return launch(args)


if __name__ == "__main__":
    raise SystemExit(main())
