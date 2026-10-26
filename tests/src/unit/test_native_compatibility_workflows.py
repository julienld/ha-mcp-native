"""Installation contracts and optional full E2E consume the same Core window."""

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
        assert setup["with"]["python-version"] == "${{ matrix.python }}"


def test_http_bench_and_units_are_not_multiplied_by_the_core_matrix():
    jobs = workflow("native-migration.yml")["jobs"]
    assert jobs["core-window"]["uses"] == "./.github/workflows/core-window.yml"
    for name in ["unit", "package"]:
        assert "strategy" not in jobs[name]
    assert jobs["package"]["needs"] == "core-window"
    assert (
        "${{ needs.core-window.outputs.latest }}"
        in jobs["package"]["env"]["HA_IMAGE_GHCR"]
    )
