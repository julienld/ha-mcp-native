"""Legacy qualification must prove restart continuity and never run Native."""

import json
import sys
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from tests.native import minimal_install, minimal_qualification

from .test_native_minimal_qualification import core_image, minimal  # noqa: F401


def test_legacy_install_uses_the_paired_wheel_for_real_runtime_startup(
    minimal,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    restarts = Mock(return_value=[{"identity": "same-entry"}])
    monkeypatch.setattr(minimal_install, "legacy_restarts", restarts)
    minimal_install.install(tmp_path, tmp_path, minimal.output, runtime="legacy")
    minimal.probes.assert_not_awaited()
    restarts.assert_called_once()
    manifest = json.loads(
        (tmp_path / "config/custom_components/ha_mcp_tools/manifest.json").read_bytes()
    )
    paired = manifest["requirements"][1]
    assert paired == f"ha-mcp @ {minimal.wheel.resolve().as_uri()}"
    proof = json.loads(minimal.output.read_bytes())
    assert proof["complete"] and proof["runtime_manifest_local_wheel_substitution"]


def test_legacy_selection_survives_the_isolated_worker_boundary(core_image) -> None:  # noqa: F811
    minimal_qualification.qualify(
        Path("assets"),
        core_image.output.parent / "work",
        core_image.output,
        image_receipt=Path("image.json"),
        expected_core=core_image.core,
        expected_arch="amd64",
        python_source="Core image",
        runtime="legacy",
    )
    command = core_image.run.call_args_list[-1].args[0]
    assert command[command.index("--runtime") + 1] == "legacy"


@pytest.mark.parametrize("changed", [False, True])
def test_restart_rejects_changed_entry_identity_and_observes_each_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    calls = []

    def run(command, **kwargs):
        phase = command[command.index("--phase") + 1]
        calls.append(phase)
        receipt = Path(command[command.index("--output") + 1])
        receipt.write_text(json.dumps({"identity": phase if changed else "same"}))
        assert kwargs["timeout"] > 0
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(minimal_install.subprocess, "run", run)
    observe = Mock()
    args = (tmp_path / "config", tmp_path, tmp_path / "proof.json")
    if changed:
        with pytest.raises(RuntimeError, match="changed entry"):
            minimal_install.legacy_restarts(*args, observe=observe)
    else:
        results = minimal_install.legacy_restarts(*args, observe=observe)
        assert results[0]["identity"] == results[1]["identity"]
    assert calls == ["first_start", "full_restart"]
    assert [call.args[0] for call in observe.call_args_list] == [
        "legacy_first_start",
        "legacy_full_restart",
    ]


def test_failed_first_process_never_attempts_or_claims_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = Mock(return_value=CompletedProcess([], 1, "", "fixture failure"))
    monkeypatch.setattr(minimal_install.subprocess, "run", run)
    observe = Mock()
    with pytest.raises(RuntimeError, match="first_start failed"):
        minimal_install.legacy_restarts(
            tmp_path, tmp_path, tmp_path / "proof.json", observe=observe
        )
    run.assert_called_once()
    observe.assert_not_called()


def test_old_core_lane_qualifies_both_architectures_with_legacy_not_native() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github/workflows/legacy-core.yml"
    data = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    contract = data["jobs"]["contract"]
    matrix = contract["strategy"]["matrix"]
    assert {"2026.8.0", "2026.9.0"} <= set(matrix["core"])
    assert set(matrix["arch"]) == {"amd64", "aarch64"}
    command = next(
        step["run"] for step in contract["steps"] if "--runtime" in step.get("run", "")
    )
    assert "--runtime legacy" in command
    assert "--entrypoint python" in command and '"$IMAGE_ID"' in command


async def test_legacy_probe_creates_the_real_long_lived_token_kind(monkeypatch):
    from tests.native.legacy_core_compat import setup_entry

    from .test_legacy_core_compat import const

    monkeypatch.setitem(
        sys.modules,
        "homeassistant.auth.const",
        SimpleNamespace(GROUP_ID_ADMIN="system-admin"),
    )
    monkeypatch.setitem(
        sys.modules,
        "homeassistant.auth.models",
        SimpleNamespace(TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN="long_lived_access_token"),
    )
    entry = SimpleNamespace(entry_id="fixture-entry", options={})
    flow = SimpleNamespace(
        async_init=AsyncMock(return_value={"flow_id": "flow"}),
        async_configure=AsyncMock(
            side_effect=[
                {"type": "form", "step_id": "server"},
                {"type": "create_entry", "result": entry},
            ]
        ),
    )
    user, refresh = object(), object()
    auth = SimpleNamespace(
        async_create_user=AsyncMock(return_value=user),
        async_create_refresh_token=AsyncMock(return_value=refresh),
        async_create_access_token=Mock(return_value="fixture-token"),
    )
    entries = SimpleNamespace(
        flow=flow,
        async_entries=lambda _domain: [],
        async_unload=AsyncMock(return_value=True),
        async_setup=AsyncMock(return_value=True),
        async_update_entry=Mock(),
    )
    hass = SimpleNamespace(
        auth=auth, config_entries=entries, async_block_till_done=AsyncMock()
    )
    assert await setup_entry(hass, "first_start") is entry
    auth.async_create_user.assert_awaited_once()
    assert auth.async_create_user.await_args.kwargs["group_ids"] == ["system-admin"]
    auth.async_create_access_token.assert_called_once_with(refresh)
    create_call = flow.async_configure.await_args_list[1]
    assert create_call.args[1]["admin_token"] == "fixture-token"
    auth.async_create_refresh_token.assert_awaited_once_with(
        user, token_type="long_lived_access_token", client_name="Legacy qualification"
    )
    assert (
        entries.async_update_entry.call_args.kwargs["options"][const.OPT_ENABLE_LLM_API]
        is True
    )


@pytest.mark.parametrize("phase", ["first_start", "full_restart"])
@pytest.mark.parametrize("representation", ["text", "bytes", "mixed", "none"])
def test_legacy_timeout_keeps_sanitized_partial_output_and_stops(
    tmp_path, monkeypatch, phase, representation
):
    out = "Core progress https://operator:PRIVATE@fixture.invalid/probe?token=HIDDEN#fragment\n"
    err = "worker stack detail\n"
    parts = {
        "text": (out, err),
        "bytes": (out.encode(), err.encode() + b"\xff"),
        "mixed": (out, err.encode()),
        "none": (None, None),
    }[representation]
    error = TimeoutExpired("fixture worker", 420, output=parts[0], stderr=parts[1])
    calls = []
    output = tmp_path / "proof.json"
    output.with_suffix(".log").write_text("previous phase log\n")

    def run(command, **kwargs):
        current = command[command.index("--phase") + 1]
        calls.append(current)
        assert kwargs["timeout"] == 420
        if current == phase:
            raise error
        Path(command[command.index("--output") + 1]).write_text('{"identity":"same"}')
        return CompletedProcess(command, 0, "phase completed", "")

    monkeypatch.setattr(minimal_install.subprocess, "run", run)
    observe = Mock()
    with pytest.raises(TimeoutExpired) as expired:
        minimal_install.legacy_restarts(
            tmp_path / "config", tmp_path, output, observe=observe
        )
    assert expired.value is error
    assert calls == (
        ["first_start"] if phase == "first_start" else ["first_start", "full_restart"]
    )
    assert observe.call_count == (0 if phase == "first_start" else 1)
    logged = output.with_suffix(".log").read_text()
    assert logged.startswith("previous phase log")
    if representation != "none":
        assert "Core progress" in logged and "worker stack detail" in logged
        assert "https://fixture.invalid/probe" in logged
        assert (
            "PRIVATE" not in logged
            and "HIDDEN" not in logged
            and "fragment" not in logged
        )
    if representation == "bytes":
        assert "\ufffd" in logged


@pytest.mark.parametrize("as_bytes", [False, True])
def test_outer_minimal_timeout_keeps_sanitized_output(tmp_path, monkeypatch, as_bytes):
    out = "outer progress https://user:PRIVATE@fixture.invalid/task?token=HIDDEN"
    err = "outer stack"
    error = TimeoutExpired(
        "outer worker",
        1200,
        output=out.encode() if as_bytes else out,
        stderr=err.encode() if as_bytes else err,
    )
    run = Mock(side_effect=error)
    monkeypatch.setattr(minimal_qualification.subprocess, "run", run)
    log = tmp_path / "worker.log"
    with pytest.raises(TimeoutExpired) as expired:
        minimal_qualification.run_logged(
            ["fixture"], env={"VIRTUAL_ENV": str(tmp_path / "venv")}, log=log
        )
    assert expired.value is error
    text = log.read_text()
    assert "outer progress" in text and "outer stack" in text
    assert "PRIVATE" not in text and "HIDDEN" not in text
    run.assert_called_once()
