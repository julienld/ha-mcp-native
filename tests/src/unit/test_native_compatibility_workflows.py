"""Installation contracts and optional full E2E consume the same Core window."""

import re
from pathlib import Path
from typing import Any

import pytest

from .test_core_window import workflow


def _run_preflight(step: dict[str, Any]) -> None:
    """Run the actual workflow's checkout-free Python preflight on Windows too."""
    script = step["run"].split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    exec(compile(script, "<workflow-preflight>", "exec"), {})


def test_full_e2e_uses_the_installation_matrix_and_each_core_python():
    definition = workflow("native-e2e.yml")
    assert (
        definition["jobs"]["core-window"]["uses"]
        == "./.github/workflows/core-window.yml"
    )
    for name in ["native-e2e", "native-server-only"]:
        job = definition["jobs"][name]
        assert job["needs"] == "core-window"
        assert (
            job["strategy"]["matrix"]
            == "${{ fromJSON(needs.core-window.outputs.matrix) }}"
        )
        assert job["runs-on"] == "${{ matrix.os }}"
        assert "${{ matrix.core }}" in job["env"]["HA_IMAGE_GHCR"]
        setup = next(
            step for step in job["steps"] if "setup-uv@" in step.get("uses", "")
        )
        assert setup["with"]["python-version"] == "${{ matrix.python_source }}"


def test_http_bench_and_units_are_not_multiplied_by_the_core_matrix():
    jobs = workflow("native-migration.yml")["jobs"]
    assert jobs["core-window"]["uses"] == "./.github/workflows/core-window.yml"
    for name in ["unit", "package"]:
        assert "strategy" not in jobs[name]
    assert "core-window" in jobs["package"]["needs"]
    assert (
        "${{ needs.core-window.outputs.latest }}"
        in jobs["package"]["env"]["HA_IMAGE_GHCR"]
    )


@pytest.mark.parametrize(
    "target,tag,route",
    [
        ("native", "", "native"),
        ("native", "native-v1.2.3", "published"),
        ("hacs_owners", "", "hacs_owners"),
        ("hacs_owners", "native-v1.2.3", None),
        ("protocol", "", "protocol"),
        ("protocol", "native-v1.2.3", None),
        ("live", "", "live"),
        ("live", "native-v1.2.3", None),
        ("unknown", "", None),
    ],
)
def test_manual_qualification_rejects_ambiguous_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    tag: str,
    route: str | None,
) -> None:
    jobs = workflow("native-migration.yml")["jobs"]
    step = jobs["select-qualification"]["steps"][0]
    output = tmp_path / "outputs"
    for key, value in {
        "TARGET": target,
        "PUBLISHED_TAG": tag,
        "TRACK_REF": "candidate-branch" if target == "live" else "",
        "PUBLIC_KEY": "PUBLIC_KEY_SENTINEL" if target == "live" else "",
        "MINUTES": "60",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "example/ha-mcp-fork",
        "GITHUB_OUTPUT": str(output),
    }.items():
        monkeypatch.setenv(key, value)
    if route is None:
        with pytest.raises(SystemExit):
            _run_preflight(step)
        assert not output.exists(), "Invalid requests must not admit any bench"
        return
    _run_preflight(step)
    if route:
        assert output.read_text().strip() == f"route={route}"
        selected = [
            name
            for name, job in jobs.items()
            if job.get("if")
            == "${{ needs.select-qualification.outputs.route == '" + route + "' }}"
        ]
        assert set(selected) == (
            {"unit", "core-window", "package"} if route == "native" else {route}
        )
        for name in selected:
            assert "select-qualification" in jobs[name]["needs"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"TRACK_REF": ""},
        {"TRACK_REF": " \n"},
        {"PUBLIC_KEY": ""},
        {"PUBLIC_KEY": " \n"},
        {"MINUTES": "0"},
        {"MINUTES": "331"},
        {"MINUTES": "-1"},
        {"MINUTES": "1.5"},
        {"MINUTES": "invalid"},
        {"GITHUB_REPOSITORY": "homeassistant-ai/ha-mcp"},
        {"GITHUB_EVENT_NAME": "pull_request"},
        {"TARGET": "native"},
        {"TARGET": "protocol"},
    ],
)
def test_invalid_live_request_cannot_start_another_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, str],
) -> None:
    step = workflow("native-migration.yml")["jobs"]["select-qualification"]["steps"][0]
    output = tmp_path / "outputs"
    values = {
        "TARGET": "live",
        "PUBLISHED_TAG": "",
        "TRACK_REF": "candidate-branch",
        "PUBLIC_KEY": "PUBLIC_KEY_SENTINEL",
        "MINUTES": "60",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "example/ha-mcp-fork",
        "GITHUB_OUTPUT": str(output),
        **overrides,
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit) as error:
        _run_preflight(step)
    assert not output.exists()
    captured = capsys.readouterr()
    assert "PUBLIC_KEY_SENTINEL" not in f"{error.value}{captured.out}{captured.err}"


@pytest.mark.parametrize(
    "filename", ["native-protocol-comparison.yml", "dev-ha-env.yml"]
)
def test_new_routes_reuse_the_same_commit_without_extra_authority(
    filename: str,
) -> None:
    caller = workflow("native-migration.yml")
    called = workflow(filename)
    assert "workflow_call" in called.get("on", called.get(True))
    assert caller["permissions"] == called["permissions"] == {"contents": "read"}
    calls = [
        job
        for job in caller["jobs"].values()
        if job.get("uses") == f"./.github/workflows/{filename}"
    ]
    assert len(calls) == 1
    assert "secrets" not in calls[0]
    assert "steps" not in calls[0]
    assert "permissions" not in calls[0]
    assert "inputs.target" in caller["concurrency"]["group"]


def test_live_verification_of_other_heads_or_modes_cannot_cancel_the_running_one() -> (
    None
):
    template = workflow("native-migration.yml")["concurrency"]["group"]
    groups = set()
    for revision, server in (
        ("a" * 40, "embedded"),
        ("b" * 40, "embedded"),
        ("a" * 40, "standalone"),
    ):
        inputs = {
            "target": "live",
            "published_tag": "",
            "track_ref": revision,
            "platform": "docker",
            "server": server,
        }
        groups.add(
            re.sub(
                r"\$\{\{ inputs\.(\w+)(?: \|\| '[^']*')? \}\}",
                lambda match, values=inputs: values[match[1]],
                template,
            )
        )
    assert len(groups) == 3


def test_live_call_maps_install_revision_and_keeps_dev_defaults() -> None:
    caller = workflow("native-migration.yml")
    called = workflow("dev-ha-env.yml")
    triggers = called.get("on", called.get(True))
    dispatch_inputs = triggers["workflow_dispatch"]["inputs"]
    call_inputs = triggers["workflow_call"]["inputs"]
    assert dispatch_inputs.keys() == call_inputs.keys()
    for name, config in dispatch_inputs.items():
        assert call_inputs[name]["type"] == (
            "string"
            if config.get("type", "string") == "choice"
            else config.get("type", "string")
        )
        assert call_inputs[name].get("default") == config.get("default")
        assert call_inputs[name].get("required", False) == config.get("required", False)
    forwarded = caller["jobs"]["live"]["with"]
    for name in ("track_ref", "public_key", "minutes", "platform", "server"):
        assert forwarded[name] == "${{ inputs." + name + " }}"
    assert set(forwarded) <= set(call_inputs)
    for name in set(call_inputs) - set(forwarded):
        assert not call_inputs[name].get("required", False)
    assert called["env"]["TRACK_REF"] == "${{ inputs.track_ref }}"
    checkouts = [
        s
        for s in called["jobs"]["env"]["steps"]
        if "actions/checkout@" in s.get("uses", "")
    ]
    assert "ref" not in checkouts[0]["with"]
    assert checkouts[1]["with"]["ref"] == "${{ env.TRACK_REF }}"
    assert all(s["with"].get("persist-credentials") is False for s in checkouts)
    assert all("repository" not in s["with"] for s in checkouts)
    for step in called["jobs"]["env"]["steps"]:
        assert "${{ inputs.track_ref }}" not in step.get("run", "")
        assert "${{ env.TRACK_REF }}" not in step.get("run", "")


@pytest.mark.parametrize(
    "repository,event,allowed",
    [
        ("example/fork", "workflow_dispatch", True),
        ("homeassistant-ai/ha-mcp", "workflow_dispatch", False),
        ("example/fork", "pull_request", False),
        ("example/fork", "pull_request_target", False),
        ("example/fork", "push", False),
    ],
)
def test_reusable_live_job_only_admits_manual_fork_runs(
    repository: str, event: str, allowed: bool
) -> None:
    condition = workflow("dev-ha-env.yml")["jobs"]["env"]["if"]
    expression = condition.removeprefix("${{").removesuffix("}}")
    expression = expression.replace("github.repository", repr(repository))
    expression = expression.replace("github.event_name", repr(event))
    assert (
        eval(expression.replace("&&", " and ").strip(), {"__builtins__": {}}) is allowed
    )


@pytest.mark.parametrize(
    "platform,server,minutes,valid",
    [
        ("docker", "embedded", "1", True),
        ("haos", "app", "330", True),
        ("docker", "oauth", "60", True),
        ("docker", "app", "60", False),
        ("haos", "oauth", "60", False),
        ("typo", "embedded", "60", False),
        ("docker", "typo", "60", False),
        ("docker", "embedded", "0", False),
        ("docker", "embedded", "331", False),
        ("docker", "embedded", "bad", False),
    ],
)
def test_callable_dev_inputs_fail_before_checkout_or_boot(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    server: str,
    minutes: str,
    valid: bool,
) -> None:
    step = workflow("dev-ha-env.yml")["jobs"]["env"]["steps"][0]
    for key, value in {
        "PLATFORM": platform,
        "SERVER": server,
        "DEVENV_MINUTES": minutes,
        "TRACK_REF": "candidate-branch",
        "PUBLIC_KEY": "PUBLIC_KEY_SENTINEL",
    }.items():
        monkeypatch.setenv(key, value)
    if valid:
        _run_preflight(step)
    else:
        with pytest.raises(SystemExit):
            _run_preflight(step)


def test_live_preflight_wiring_and_encrypted_artifacts_preserve_the_boundary() -> None:
    caller = workflow("native-migration.yml")
    selector = caller["jobs"]["select-qualification"]["steps"][0]
    for name in ("target", "published_tag", "track_ref", "public_key", "minutes"):
        assert "inputs." + name in selector["env"][name.upper()]
    called = workflow("dev-ha-env.yml")
    job = called["jobs"]["env"]
    check = job["steps"][0]
    for env, name in (
        ("PUBLIC_KEY", "public_key"),
        ("PLATFORM", "platform"),
        ("SERVER", "server"),
        ("DEVENV_MINUTES", "minutes"),
    ):
        assert check["env"][env] == "${{ inputs." + name + " }}"
    # RSA-4096 OAEP admits the 470-byte trial; RSA-2048 cannot.
    assert "head -c 470" in check["run"]
    assert "pkeyutl -encrypt -pubin" in check["run"]
    assert "rsa_padding_mode:oaep" in check["run"]
    uploads = [
        s for s in job["steps"] if "actions/upload-artifact@" in s.get("uses", "")
    ]
    assert uploads
    assert all(s["with"]["path"].endswith(".enc") for s in uploads)
    boot = next(s for s in job["steps"] if "hold.pid" in s.get("run", ""))
    assert boot["env"]["DEVENV_MINUTES"] == "${{ inputs.minutes }}"
    assert "publish_urls" in boot["run"]
    assert "::add-mask::" in boot["run"]
    assert job["timeout-minutes"] > int(
        called[True]["workflow_call"]["inputs"]["minutes"]["default"]
    )
    assert "dev-ha-env-" in called["concurrency"]["group"]
    assert called["concurrency"]["group"] != caller["concurrency"]["group"]


def test_hacs_owner_workflow_is_callable_without_publishing_authority() -> None:
    definition = workflow("native-hacs-owner-qualification.yml")
    assert "workflow_call" in definition.get("on", definition.get(True))
    assert definition["permissions"] == {"contents": "read"}
    caller = workflow("native-migration.yml")["jobs"]["hacs_owners"]
    assert caller["uses"] == "./.github/workflows/native-hacs-owner-qualification.yml"
    assert "secrets" not in caller
    steps = definition["jobs"]["qualify"]["steps"]
    build = next(
        step for step in steps if "--qualification-only" in step.get("run", "")
    )
    assert build["env"]["CANDIDATE_TAG"].startswith("native-ci-")


def test_installation_runs_the_inspected_image_id_and_preserves_receipt() -> None:
    job = workflow("core-window.yml")["jobs"]["contract"]
    assert "matrix.python" not in job["name"]
    steps = job["steps"]
    inspect = next(step for step in steps if step.get("id") == "image")
    assert 'docker image inspect "$image_id"' in inspect["run"]
    install = next(
        step for step in steps if "minimal_qualification" in step.get("run", "")
    )
    assert install["env"]["IMAGE_ID"] == "${{ steps.image.outputs.id }}"
    assert '--entrypoint python "$IMAGE_ID"' in install["run"]
    assert "--image-receipt" in install["run"]
