"""A public install is qualified only with its actual receipt and Core footprint."""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def install_receipt():
    spec = importlib.util.spec_from_file_location(
        "published_install_test",
        ROOT / "tests/native/fixtures/native_fixture/dependency_receipt.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installed_public_wheel(tmp_path, monkeypatch, install_receipt):
    component = tmp_path / "component"
    component.mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    version = "9.0.0.dev123"
    url = "https://github.com/julienld/ha-mcp-native/releases/download/v9.0.0.dev123/ha_mcp-9.0.0.dev123-py3-none-any.whl"
    sha256 = "b" * 64
    dependency = f"ha-mcp @ {url}#sha256={sha256}"
    manifest = json.dumps({"requirements": [dependency], "version": version}).encode()
    (component / "manifest.json").write_bytes(manifest)
    expected = {
        "wheel_url": url,
        "wheel_sha256": sha256,
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "tag": "v9.0.0.dev123",
    }
    (assets / "published-verification.json").write_text(json.dumps(expected))
    (assets / "native-build.json").write_text(
        json.dumps({"dependency": dependency, "library_version": version})
    )
    proof_path = tmp_path / "dependencies.json"
    versions = {"homeassistant": "2026.10.2", "rich": "10.0.0", "pydantic": "2.12.0"}
    issues = ["known Core image conflict"]
    monkeypatch.setattr(install_receipt, "installed_versions", versions.copy)
    monkeypatch.setattr(install_receipt, "dependency_issues", issues.copy)
    install_receipt.capture_baseline(proof_path)
    versions["ha-mcp"] = version
    direct = {"url": url, "archive_info": {"hashes": {"sha256": sha256}}}
    distribution = SimpleNamespace(
        metadata={"Name": "ha-mcp"},
        version=version,
        read_text=lambda _: json.dumps(direct),
    )
    monkeypatch.setattr(
        install_receipt.metadata, "distribution", lambda _: distribution
    )
    return SimpleNamespace(
        component=component,
        assets=assets,
        proof=proof_path,
        versions=versions,
        issues=issues,
        direct=direct,
        distribution=distribution,
    )


def observe(install_receipt, world):
    return install_receipt.observe_dependencies(
        world.component, world.proof, world.assets
    )


def test_managed_receipt_retains_first_and_post_restart_observations(
    install_receipt, installed_public_wheel
):
    world = installed_public_wheel
    first = observe(install_receipt, world)
    assert first["observations"][0]["verified"]
    # Core may add packages for exercised integrations, but cannot replace originals.
    world.versions["new-integration-dependency"] = "1.0"
    last = observe(install_receipt, world)
    assert len(last["observations"]) == 2
    assert all(item["verified"] for item in last["observations"])
    assert "ha-mcp" not in last["before"]
    assert last["observations"][-1]["wheel"]["url"] == world.direct["url"]


@pytest.mark.parametrize(
    "change",
    ["dependency", "conflict", "receipt_url", "receipt_hash", "version", "manifest"],
)
def test_runtime_evidence_rejects_changes_hidden_by_successful_tool_calls(
    install_receipt, installed_public_wheel, change
):
    world = installed_public_wheel
    match change:
        case "dependency":
            world.versions["rich"] = "15.0.0"
        case "conflict":
            world.issues.append("new incompatible dependency")
        case "receipt_url":
            world.direct["url"] = "file:///opt/preinstalled-wheel.whl"
        case "receipt_hash":
            world.direct["archive_info"]["hashes"]["sha256"] = "c" * 64
        case "version":
            world.distribution.version = "9.0.0.dev999"
        case "manifest":
            (world.component / "manifest.json").write_text(
                json.dumps({"requirements": ["ha-mcp==9.0.0.dev123"]})
            )
    proof = observe(install_receipt, world)
    assert not proof["observations"][-1]["verified"]
    # Failures remain in the evidence artifact, not only an HTTP assertion string.
    assert json.loads(world.proof.read_text()) == proof


def test_receipt_cannot_mix_conflicting_old_and_new_hash_fields(
    install_receipt, installed_public_wheel
):
    world = installed_public_wheel
    world.direct["archive_info"]["hash"] = "sha256=" + "c" * 64
    assert not observe(install_receipt, world)["observations"][-1]["verified"]


def test_missing_uv_archive_hash_is_retained_as_missing_not_mismatch(
    install_receipt, installed_public_wheel
):
    world = installed_public_wheel
    world.direct["archive_info"] = {}
    observation = observe(install_receipt, world)["observations"][-1]
    assert not observation["verified"]
    assert observation["receipt"]["archive_info"] == {}
    assert observation["receipt"]["sha256_status"] == "missing"
    assert observation["receipt"]["url_matches"] is True
    assert "no SHA-256" in observation["errors"][0]


def test_mismatched_hash_remains_a_refusal_with_observed_digest(
    install_receipt, installed_public_wheel
):
    world = installed_public_wheel
    world.direct["archive_info"]["hashes"]["sha256"] = "c" * 64
    observation = observe(install_receipt, world)["observations"][-1]
    assert not observation["verified"]
    assert observation["receipt"]["sha256_status"] == "mismatch"
    assert observation["receipt"]["archive_info"]["hashes"]["sha256"] == "c" * 64


def test_failed_receipt_evidence_never_persists_signed_url_or_arbitrary_fields(
    install_receipt, installed_public_wheel
):
    world = installed_public_wheel
    world.direct["url"] = (
        "https://release-assets.githubusercontent.com/file?sig=PRIVATE"
    )
    world.direct["archive_info"] = {"hashes": {"sha256": "PRIVATE"}, "token": "PRIVATE"}
    observation = observe(install_receipt, world)["observations"][-1]
    assert not observation["verified"]
    assert observation["receipt"]["url_matches"] is False
    assert "PRIVATE" not in json.dumps(observation)


def test_baseline_refuses_preinstalled_distribution(
    tmp_path, monkeypatch, install_receipt
):
    monkeypatch.setattr(
        install_receipt, "installed_versions", lambda: {"ha-mcp": "9.0.0.dev1"}
    )
    with pytest.raises(ValueError, match="without either"):
        install_receipt.capture_baseline(tmp_path / "proof.json")
    assert not (tmp_path / "proof.json").exists()


def test_pip_check_execution_failure_is_not_an_empty_conflict_set(
    monkeypatch, install_receipt
):
    monkeypatch.setattr(
        install_receipt.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="", stderr="No module named pip"
        ),
    )
    with pytest.raises(RuntimeError, match="could not inspect"):
        install_receipt.dependency_issues()


def test_published_lane_uses_same_full_bench_without_preinstall_or_manifest_rewrite():
    dockerfile = (ROOT / "tests/native/Dockerfile").read_text()
    published, candidate = dockerfile.split("FROM ${HA_IMAGE} AS candidate")
    published_commands = "\n".join(
        line for line in published.splitlines() if not line.startswith("#")
    )
    assert "pip install" not in published_commands
    assert "install_package(" not in published_commands
    assert "COPY dist/native/*.whl" not in published
    assert "manifest['requirements'] =" not in published
    assert "capture_baseline()" in published
    # Core upgrades dependencies; plain pip reused satisfied packages and hid
    # conflicts that appeared only during the real manifest installation.
    assert "from homeassistant.util.package import install_package" in candidate
    assert "install_package(str(wheel), upgrade=True" in candidate
    assert "manifest['requirements'] =" in candidate
    assert candidate.index("capture_baseline(Path(") < candidate.index(
        "assert install_package("
    )

    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/native-migration.yml").read_text()
    )
    jobs = workflow["jobs"]
    assert "!inputs.published_tag" in jobs["unit"]["if"]
    assert "!inputs.published_tag" in jobs["package"]["if"]
    assert "workflow_dispatch" in jobs["published"]["if"]
    assert "inputs.published_tag != ''" in jobs["published"]["if"]
    commands = [step.get("run", "") for step in jobs["published"]["steps"]]
    assert any("run_container.py --published-tag" in command for command in commands)
    assert any("--target published" in command for command in commands)
    assert not any(
        "native_release.py" in command or "gh release" in command
        for command in commands
    )
    # Inputs are passed as data, never interpolated into executable shell text.
    assert not any("${{ inputs." in command for command in commands)
