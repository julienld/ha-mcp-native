"""Installation contracts and optional full E2E consume the same Core window."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_core_window import workflow


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
        ("unknown", "", None),
    ],
)
def test_manual_qualification_rejects_ambiguous_modes(
    tmp_path: Path, target: str, tag: str, route: str | None
) -> None:
    jobs = workflow("native-migration.yml")["jobs"]
    step = jobs["select-qualification"]["steps"][0]
    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "TARGET": target,
            "PUBLISHED_TAG": tag,
            "GITHUB_OUTPUT": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is (route is not None), result.stderr
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
