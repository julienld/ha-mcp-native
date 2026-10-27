"""Stable release rollover must not silently drop the promised Core minimum."""

import importlib.util
import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def resolver():
    spec = importlib.util.spec_from_file_location(
        "core_window", ROOT / "scripts/core_window.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "latest,releases,expected",
    [
        (
            "2026.10.4",
            ["2026.9.4", "2026.10.0", "2026.10.4", "2026.11.0b1", "2026.11.0.dev1"],
            ["2026.10.0", "2026.10.4"],
        ),
        (
            "2026.11.0",
            ["2026.10.0", "2026.10.4", "2026.11.0"],
            ["2026.10.0", "2026.10.4", "2026.11.0"],
        ),
        (
            "2026.12.1",
            ["2026.10.4", "2026.11.0", "2026.11.3", "2026.12.0", "2026.12.1"],
            ["2026.11.0", "2026.11.3", "2026.12.1"],
        ),
        (
            "2027.1.2",
            ["2026.12.0", "2026.12.4", "2027.1.2"],
            ["2026.12.0", "2026.12.4", "2027.1.2"],
        ),
    ],
)
def test_rollover_uses_released_generations_and_keeps_exact_floor(
    resolver, latest, releases, expected
):
    assert resolver.select_versions(latest, releases) == expected


@pytest.mark.parametrize(
    "latest", ["2026.11.0b1", "2026.11.0rc1", "2026.11.0.dev0", "2026.13.0", "2026.9.4"]
)
def test_prerelease_or_invalid_stable_channel_fails_closed(resolver, latest):
    with pytest.raises(ValueError):
        resolver.select_versions(latest, [latest])


def test_missing_exact_floor_cannot_be_replaced_with_latest_patch(resolver):
    with pytest.raises(ValueError, match="exact minimum"):
        resolver.select_versions("2026.11.2", ["2026.10.4", "2026.11.2"])


def test_one_channel_snapshot_excludes_future_tags_and_records_each_python(resolver):
    fixtures = {
        resolver.STABLE_URL: {
            "channel": "stable",
            "homeassistant": {"default": "2026.11.0"},
        },
        resolver.RELEASES_URL: {
            "releases": {
                version: [{"yanked": False}]
                for version in [
                    "2026.10.0",
                    "2026.10.4",
                    "2026.11.0",
                    "2026.12.0",
                    "2026.12.0b1",
                ]
            }
        },
    }
    calls = []

    def fetch(url):
        calls.append(url)
        if url in fixtures:
            return json.dumps(fixtures[url])
        return "3.15.1\n" if "/2026.11.0/" in url else "3.14.9\n"

    window = resolver.resolve_window(fetch)
    assert calls.count(resolver.STABLE_URL) == 1
    assert window["minimum"] == "2026.10.0"
    assert window["generations"] == ["2026.10", "2026.11"]
    assert len(window["matrix"]["include"]) == 6
    for row in window["matrix"]["include"]:
        assert row["python_source"] == window["versions"][row["core"]]
        assert row["os"] == resolver.ARCHITECTURES[row["arch"]]


def test_yanked_stable_is_not_an_installable_window(resolver):
    def fetch(url):
        return json.dumps(
            {"channel": "stable", "homeassistant": {"default": "2026.10.4"}}
            if url == resolver.STABLE_URL
            else {
                "releases": {
                    "2026.10.0": [{"yanked": False}],
                    "2026.10.4": [{"yanked": True}],
                }
            }
        )

    with pytest.raises(ValueError, match="missing"):
        resolver.resolve_window(fetch)


@pytest.mark.parametrize(
    "error,attempts",
    [(URLError("offline"), 3), (HTTPError("url", 404, "missing", {}, None), 1)],
)
def test_network_failure_is_bounded_and_never_returns_a_frozen_matrix(
    resolver, monkeypatch, error, attempts
):
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs["timeout"])
        raise error

    monkeypatch.setattr(resolver, "urlopen", fail)
    monkeypatch.setattr(resolver.time, "sleep", lambda _: None)
    with pytest.raises(RuntimeError):
        resolver.fetch_text(resolver.STABLE_URL)
    assert len(calls) == attempts and all(timeout <= 15 for timeout in calls)


def test_release_floor_guard_checks_hacs_and_both_runtime_modes(resolver, tmp_path):
    component = tmp_path / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    window = resolver.make_window(
        "2026.12.1", {"2026.11.0": "3.14.9", "2026.12.1": "3.15.1"}
    )
    for directory in [tmp_path, component]:
        (directory / "hacs.json").write_text(json.dumps({"homeassistant": "2026.11.0"}))
    constants = component / "const.py"
    constants.write_text("\n".join(f'{name} = "2026.11.0"' for name in resolver.FLOORS))
    resolver.check_floor(tmp_path, window)
    constants.write_text(
        constants.read_text().replace(
            'MIN_NATIVE_HOME_ASSISTANT_VERSION = "2026.11.0"',
            'MIN_NATIVE_HOME_ASSISTANT_VERSION = "2026.10.0"',
        )
    )
    with pytest.raises(ValueError, match="raise the HACS and runtime floors"):
        resolver.check_floor(tmp_path, window)


def test_corrupted_matrix_cannot_skip_an_architecture(resolver, tmp_path):
    window = resolver.make_window(
        "2026.10.4", {"2026.10.0": "3.14.9", "2026.10.4": "3.14.9"}
    )
    window["matrix"]["include"].pop()
    path = tmp_path / "window.json"
    path.write_text(json.dumps(window))
    with pytest.raises(ValueError, match="matrix"):
        resolver.load_window(path)


def workflow(name):
    return yaml.safe_load(
        (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
    )


def test_every_pr_and_main_push_runs_the_monthly_contract_even_for_docs():
    for name in ["pr.yml", "e2e-tests.yml"]:
        definition = workflow(name)
        triggers = definition.get("on", definition.get(True))
        event = triggers["pull_request" if name == "pr.yml" else "push"]
        assert not {"paths", "paths-ignore"} & event.keys()
        call = definition["jobs"]["core-window"]
        assert call["uses"] == "./.github/workflows/core-window.yml"
        assert "if" not in call
    assert "fast-checks" in workflow("pr.yml")["jobs"]["core-window"]["needs"]


def test_qualification_continues_when_source_floor_needs_raising():
    jobs = workflow("core-window.yml")["jobs"]
    assert "floor" not in jobs["package"]["needs"]
    assert "floor" not in jobs["contract"]["needs"]
    assert "qualification-only" in str(jobs["package"]["steps"])
    assert set(jobs["gate"]["needs"]) == {"resolve", "floor", "package", "contract"}
    assert jobs["gate"]["if"] == "always()"


def test_pr_code_jobs_wait_for_clean_tree_validation():
    jobs = workflow("pr.yml")["jobs"]
    for name in [
        "component-version-gate",
        "site-checks",
        "unit-tests",
        "docker-validation",
    ]:
        assert "fast-checks" in jobs[name]["needs"], name
    for name in jobs:
        if name.startswith("e2e-validation") and name != "e2e-validation-gate":
            assert "fast-checks" in jobs[name]["needs"], name


@pytest.mark.parametrize(
    "name,job,guarded_step",
    [
        ("semver-release.yml", "semantic-release", "Python Semantic Release"),
        ("publish-dev.yml", "prepare", "Generate dev version"),
        ("sync-integration-mirror.yml", "sync", "Commit and push"),
    ],
)
def test_release_paths_check_actual_source_floors_before_publication(
    name, job, guarded_step
):
    steps = workflow(name)["jobs"][job]["steps"]
    guards = [
        (index, step)
        for index, step in enumerate(steps)
        if "core_window.py" in step.get("run", "")
    ]
    assert len(guards) == 1
    index, guard = guards[0]
    assert index < next(
        index for index, step in enumerate(steps) if step.get("name") == guarded_step
    )
    assert "--check-floor" in guard["run"] and "--from-file" not in guard["run"]
    assert "continue-on-error" not in guard
    if name == "sync-integration-mirror.yml":
        assert "--repository" in guard["run"]
        assert guard["env"]["RELEASE_SOURCE"] == "${{ steps.release.outputs.src }}"
    if name == "publish-dev.yml":
        for publisher in [
            "publish-pypi-dev",
            "publish-docker-dev",
            "create-prerelease",
        ]:
            assert "prepare" in workflow(name)["jobs"][publisher]["needs"]


def test_required_pr_gate_cannot_let_docs_skip_the_core_window():
    gate = workflow("pr.yml")["jobs"]["e2e-validation-gate"]
    assert "core-window" in gate["needs"]
    script = gate["steps"][0]["run"]
    assert script.index("needs.core-window.result") < script.index(
        "needs.changes.outputs.run"
    )
