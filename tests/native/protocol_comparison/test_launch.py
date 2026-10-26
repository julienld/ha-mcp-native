"""Launcher boundaries and real child-process lifecycle; not live Core evidence."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import jwt
import pytest

from . import launch


def test_ephemeral_seed_keeps_two_real_admins_and_never_reuses_keys(
    tmp_path: Path,
) -> None:
    actors = launch.seed_auth(tmp_path / "first")
    other = launch.seed_auth(tmp_path / "second")
    auth = json.loads((tmp_path / "first/.storage/auth").read_bytes())["data"]
    assert len({a["user_id"] for a in actors + other}) == 4
    assert len({a["token"] for a in actors + other}) == 4
    assert all(u["is_active"] and not u["system_generated"] for u in auth["users"])
    assert all("system-admin" in u["group_ids"] for u in auth["users"])
    assert sum(u["is_owner"] for u in auth["users"]) == 1
    for actor, record in zip(actors, auth["refresh_tokens"], strict=True):
        claims = jwt.decode(actor["token"], record["jwt_key"], algorithms=["HS256"])
        assert claims["iss"] == record["id"]
        assert record["user_id"] == actor["user_id"]
        assert claims["exp"] - claims["iat"] <= 7200


@pytest.mark.parametrize("runtime", ["legacy", "native"])
def test_real_continuity_entry_is_configured_without_replacing_its_owner_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
) -> None:
    repository = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(repository / "tests"))
    monkeypatch.syspath_prepend(str(repository / "tests/src"))
    monkeypatch.setenv("E2E_EMBEDDED_RUNTIME", runtime)
    from e2e._conftest_embedded import _install_embedded_server

    launch.store(tmp_path, "core.config_entries", {"entries": []})
    actors = launch.seed_auth(tmp_path)
    _install_embedded_server(tmp_path, "paired.whl")
    webhook = launch.configure_entry(tmp_path, runtime, actors)
    entry = json.loads((tmp_path / ".storage/core.config_entries").read_bytes())[
        "data"
    ]["entries"][0]
    assert entry["data"]["webhook_id"] == webhook
    assert entry["options"]["pip_spec"].endswith("/paired.whl")
    assert entry["options"]["runtime_mode"] == runtime
    if runtime == "native":
        assert "admin_token" not in entry["data"]
        assert entry["data"]["native_owner_user_id"] == actors[0]["user_id"]
        assert entry["options"]["webhook_auth"] == "ha_auth"
    else:
        assert entry["data"]["admin_token"] == actors[0]["token"]
    flags = json.loads((tmp_path / ".ha_mcp/feature_flags.json").read_bytes())
    assert not flags["read_only_mode"] and not flags["enable_dev_mode"]


def test_container_reuses_installer_without_exposing_the_legacy_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from testcontainers.core import container as docker_container

    monkeypatch.setattr(docker_container, "DockerClient", lambda **_: object())
    container = docker_container.DockerContainer("sha256:" + "1" * 64)
    container.with_exposed_ports(8123, 9584)
    container.with_kwargs(
        privileged=True, entrypoint=["sh", "-c", "install && exec /init"]
    )
    launch.constrain_container(container, "isolated-network", "isolated-name", "nonce")
    assert container.ports == {"8123/tcp": ("127.0.0.1", None)}
    assert not container._kwargs["privileged"]
    assert container._kwargs["entrypoint"][-1] == "install && exec /init"
    assert container._kwargs["network"] == "isolated-network"


@pytest.mark.parametrize(
    "defect", ["different_image", "wildcard", "extra_port", "privileged"]
)
def test_actual_docker_inspection_rejects_unisolated_or_substituted_container(
    defect: str,
) -> None:
    image = "sha256:" + "a" * 64
    attrs = {
        "Image": image,
        "HostConfig": {"Privileged": False},
        "NetworkSettings": {
            "Ports": {
                "8123/tcp": [{"HostIp": "127.0.0.1", "HostPort": "30000"}],
            }
        },
    }
    assert launch.observed_container(attrs, image) == 30000
    if defect == "different_image":
        attrs["Image"] = "sha256:" + "b" * 64
    elif defect == "wildcard":
        attrs["NetworkSettings"]["Ports"]["8123/tcp"][0]["HostIp"] = "0.0.0.0"
    elif defect == "extra_port":
        attrs["NetworkSettings"]["Ports"]["9584/tcp"] = [
            {"HostIp": "127.0.0.1", "HostPort": "30001"}
        ]
    else:
        attrs["HostConfig"]["Privileged"] = True
    with pytest.raises(ValueError, match="isolation"):
        launch.observed_container(attrs, image)


def test_private_config_uses_baseline_owner_and_native_callers_without_exporting_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "household-must-not-escape")
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy")
    monkeypatch.setenv("GITHUB_TOKEN", "github-must-not-escape")
    ready = {
        role: {
            "mcp_url": "http://127.0.0.1:30000/private",
            "fixture_url": "http://127.0.0.1:30000/api/protocol_fixture",
            "server_log": "private.log",
            "expected": {},
            "actors": [
                {"user_id": role + "-one", "token": role + "-token-one"},
                {"user_id": role + "-two", "token": role + "-token-two"},
            ],
        }
        for role in launch.ROLES
    }
    config, env = launch.comparison_config(ready)
    for forbidden in ("HOMEASSISTANT_TOKEN", "HTTPS_PROXY", "GITHUB_TOKEN"):
        assert forbidden not in env
    assert env[config["baseline"]["actors"][1]["expected_user_env"]] == "baseline-one"
    assert env[config["candidate"]["actors"][1]["expected_user_env"]] == "candidate-two"
    assert "token-one" not in json.dumps(config)
    bad = copy.deepcopy(ready)
    bad["candidate"]["actors"][1] = bad["candidate"]["actors"][0]
    with pytest.raises(ValueError, match="distinct"):
        launch.comparison_config(bad)


def test_source_admission_does_not_accept_a_dirty_application_or_short_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    def git(_path: Path, *args: str) -> str:
        calls.append(args)
        return " M src/ha_mcp/server.py" if args[0] == "status" else launch.BASELINE

    monkeypatch.setattr(launch, "git", git)
    with pytest.raises(ValueError, match="dirty"):
        launch.source_proof(tmp_path, launch.BASELINE)
    assert set(launch.PROTECTED).issubset(calls[-1])
    calls.clear()
    with pytest.raises(ValueError, match="full_source"):
        launch.source_proof(tmp_path, launch.BASELINE[:8])
    assert not calls


def test_overlay_cannot_copy_application_files(tmp_path: Path) -> None:
    harness, source = tmp_path / "harness", tmp_path / "source"
    for relative in launch.OVERLAYS:
        path = harness / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative)
    forbidden = source / "custom_components/ha_mcp_tools/manifest.json"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("keep-own-component")
    hashes = launch.overlay_continuity(harness, source)
    assert forbidden.read_text() == "keep-own-component"
    assert hashes and all(path.startswith("tests/") for path in hashes)
    assert all(
        (source / path).read_bytes() == (harness / path).read_bytes() for path in hashes
    )


@pytest.fixture
def launcher_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[argparse.Namespace, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    args = argparse.Namespace(
        baseline_source=tmp_path / "baseline",
        candidate_source=tmp_path / "candidate",
        candidate_revision="c" * 40,
        core_image=launch.DEFAULT_IMAGE,
        output=tmp_path / "public",
        state=tmp_path / "state.json",
    )
    monkeypatch.setattr(launch.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(
        launch, "source_proof", lambda path, revision: {"src": revision}
    )
    monkeypatch.setattr(
        launch, "overlay_continuity", lambda *a: {"tests/helper.py": "a" * 64}
    )

    def docker(*cmd: str, **_: Any) -> str:
        calls.append(cmd)
        if cmd[1:3] == ("context", "inspect"):
            return json.dumps("unix:///var/run/docker.sock")
        if cmd[1:3] == ("image", "inspect"):
            return "sha256:" + "1" * 64
        if cmd[1:3] == ("ps", "-aq"):
            return "owned-container"
        if cmd[1:4] == ("network", "ls", "-q"):
            return "owned-network"
        return ""

    monkeypatch.setattr(launch, "command", docker)
    return args, calls


@pytest.mark.parametrize("fail", [False, True])
def test_two_real_worker_processes_are_joined_and_private_data_removed(
    launcher_world: tuple[argparse.Namespace, list[tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fail: bool,
) -> None:
    args, calls = launcher_world
    peer = tmp_path / "peer.py"
    peer.write_text(
        textwrap.dedent("""
        import json, os, sys
        from pathlib import Path
        command, *arguments = sys.argv[1:]
        options = dict(zip(arguments[::2], arguments[1::2], strict=True))
        if command == 'worker':
            role = options['--role']
            directory = Path(options['--directory'])
            record = {'mcp_url': 'http://127.0.0.1:30000/private-secret',
                      'fixture_url': 'http://127.0.0.1:30000/api/protocol_fixture',
                      'server_log': str(directory / 'server.log'), 'expected': {},
                      'actors': [{'user_id': role + str(i), 'token': role + '-private-token-' + str(i)} for i in range(2)]}
            (directory / 'server.log').write_text('PRIVATE LOG MUST DISAPPEAR')
            (directory / 'ready.tmp').write_text(json.dumps(record))
            (directory / 'ready.tmp').replace(directory / 'ready.json')
            sys.stdin.buffer.read(1)
        else:
            config = json.loads(Path(options['--config']).read_text())
            assert options['--rounds'] == '60' and options['--warmups'] == '2'
            assert set(config) == {'baseline', 'candidate'}
            assert all(os.environ[a['token_env']] for c in config.values() for a in c['actors'])
            assert 'HOMEASSISTANT_TOKEN' not in os.environ
            Path(options['--output']).write_text(json.dumps({'complete': FAIL_VALUE is False}))
            raise SystemExit(1 if FAIL_VALUE else 0)
    """).replace("FAIL_VALUE", repr(fail))
    )
    real_popen = subprocess.Popen
    children = []

    def process(argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        child = real_popen([sys.executable, str(peer), *argv[2:]], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(launch.subprocess, "Popen", process)
    assert launch.launch(args) == int(fail)
    assert all(p.poll() is not None for p in children)
    assert len(children) == 3
    assert not list(tmp_path.glob("ha-mcp-protocol-*"))
    assert not args.state.exists()
    report = json.loads((args.output / "launch.json").read_bytes())
    assert report["cleanup_complete"] and report["workers_stopped_cleanly"]
    assert report["source_trees_before"]["baseline"]["src"] == launch.BASELINE
    assert report["source_trees_before"]["candidate"]["src"] == args.candidate_revision
    exported = "".join(p.read_text() for p in args.output.iterdir())
    assert "private-token" not in exported and "private-secret" not in exported
    assert any(call[1:4] == ("rm", "--force", "--volumes") for call in calls)


def test_failed_bringup_terminates_other_worker_and_redacts_exception(
    launcher_world: tuple[argparse.Namespace, list[tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _ = launcher_world
    children = []

    def broken_pair(
        args: Any,
        sources: Any,
        revisions: Any,
        private: Path,
        image: str,
        nonce: str,
        workers: dict[str, subprocess.Popen[Any]],
        report: Any,
    ) -> None:
        for role in launch.ROLES:
            workers[role] = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                start_new_session=os.name != "nt",
            )
            children.append(workers[role])
        (private / "credentials.json").write_text("private-token")
        raise RuntimeError("private-token http://secret/path unexpected body")

    monkeypatch.setattr(launch, "run_pair", broken_pair)
    assert launch.launch(args) == 1
    assert all(p.poll() is not None for p in children)
    raw = (args.output / "launch.json").read_text()
    assert "private-token" not in raw and "http://" not in raw
    assert json.loads(raw)["cleanup_complete"]


def test_cleanup_failure_cannot_leave_a_successful_report(
    launcher_world: tuple[argparse.Namespace, list[tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _ = launcher_world
    monkeypatch.setattr(launch, "run_pair", lambda *a: a[-1].update(complete=True))

    def fail_cleanup(state: Path) -> bool:
        raise OSError("private path")

    monkeypatch.setattr(launch, "cleanup", fail_cleanup)
    assert launch.launch(args) == 1
    assert args.state.exists()  # Actions can retry this exact run, not a global prune.
    report = json.loads((args.output / "launch.json").read_bytes())
    assert not report["complete"] and not report["cleanup_complete"]


def test_cleanup_rejects_an_unscoped_directory_without_running_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state.json"
    launch.write_json(
        state, {"nonce": "a" * 32, "private": str(tmp_path), "image": "image"}
    )
    calls = []
    monkeypatch.setattr(launch, "command", lambda *a, **kw: calls.append(a))
    with pytest.raises(ValueError, match="cleanup_scope"):
        launch.cleanup(state)
    assert not calls and tmp_path.exists()


def test_one_failed_container_removal_does_not_abandon_the_other_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nonce = "a" * 32
    private = tmp_path / ("ha-mcp-protocol-" + nonce)
    private.mkdir()
    state = tmp_path / "cleanup.json"
    launch.write_json(state, {"nonce": nonce, "private": str(private)})
    calls = []

    def docker(*args: str, **kwargs: Any) -> str:
        calls.append(args)
        if args[1:3] == ("ps", "-aq"):
            return "stuck-container\nother-container"
        if args[1:4] == ("network", "ls", "-q"):
            return "owned-network"
        if args[-1] == "stuck-container":
            raise subprocess.CalledProcessError(1, args)
        return ""

    monkeypatch.setattr(launch, "command", docker)
    with pytest.raises(RuntimeError, match="cleanup_incomplete"):
        launch.cleanup(state)
    assert any(c[-1] == "other-container" for c in calls)
    assert any(c[1:3] == ("network", "rm") for c in calls)
    assert state.exists() and private.exists()


def test_partial_worker_diagnostic_never_prevents_cleanup(
    launcher_world: tuple[argparse.Namespace, list[tuple[str, ...]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _ = launcher_world

    def interrupted_status(*values: Any) -> None:
        private = values[3]
        (private / "baseline").mkdir()
        (private / "baseline/status.json").write_text('{"phase":')
        raise RuntimeError("worker stopped during a diagnostic write")

    monkeypatch.setattr(launch, "run_pair", interrupted_status)
    assert launch.launch(args) == 1
    assert json.loads((args.output / "launch.json").read_bytes())["cleanup_complete"]
    assert not args.state.exists()
