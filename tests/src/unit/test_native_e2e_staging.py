"""Qualification must boot the selected runtime under a real fixture identity."""

import importlib.util
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests.src.e2e import _conftest_testcontainer as container_harness
from tests.src.e2e._conftest_native_runtime import (
    attest_native_runtime,
    configure_native_entry,
    native_dependency_command,
)


@pytest.fixture(autouse=True)
def isolated_attestation_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("E2E_ATTESTATION_DIRECTORY", raising=False)


@pytest.fixture
def container_build(tmp_path, monkeypatch):
    container = Mock()
    for method in (
        "with_exposed_ports",
        "with_volume_mapping",
        "with_env",
        "with_kwargs",
    ):
        getattr(container, method).return_value = container
    constructor = Mock(return_value=container)
    monkeypatch.setattr(container_harness, "DockerContainer", constructor)
    monkeypatch.setattr(
        container_harness,
        "_collect_manifest_requirements",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(container_harness, "native_runtime_selected", lambda: True)
    monkeypatch.delenv("HA_TEST_PORT", raising=False)
    monkeypatch.delenv("HA_TEST_IMAGE", raising=False)
    monkeypatch.delenv("HA_IMAGE_GHCR", raising=False)

    def build() -> Mock:
        spec = importlib.util.spec_from_file_location(
            "selected_test_image", Path(__file__).parents[2] / "test_constants.py"
        )
        constants = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(constants)
        monkeypatch.setattr(container_harness, "HA_TEST_IMAGE", constants.HA_TEST_IMAGE)
        return container_harness._build_ha_testcontainer(
            tmp_path, True, "candidate.whl"
        )

    return SimpleNamespace(build=build, constructor=constructor)


def test_embedded_boot_uses_the_matrix_image_instead_of_the_stale_imported_pin(
    monkeypatch, container_build
):
    image = "ghcr.io/home-assistant/home-assistant:2026.8.0"
    monkeypatch.setenv("HA_IMAGE_GHCR", image)
    container_build.build()
    container_build.constructor.assert_called_once_with(image)


def test_explicit_test_image_override_still_wins_over_the_workflow_image(
    monkeypatch, container_build
):
    image = "registry.test/custom-core:candidate"
    monkeypatch.setenv("HA_TEST_IMAGE", image)
    monkeypatch.setenv(
        "HA_IMAGE_GHCR", "ghcr.io/home-assistant/home-assistant:2026.10.3"
    )
    container_build.build()
    container_build.constructor.assert_called_once_with(image)


def test_native_entrypoint_brackets_the_wheel_before_core_starts(container_build):
    container = container_build.build()
    entrypoint = container.with_kwargs.call_args.kwargs["entrypoint"]
    assert entrypoint[:2] == ["sh", "-c"]
    preparation, startup = entrypoint[2].split(" && ")
    assert shlex.split(preparation) == native_dependency_command("prepare")
    assert startup == "exec /init"


def _seed(config: Path, user: dict) -> Path:
    storage = config / ".storage"
    storage.mkdir()
    (storage / "auth").write_text(json.dumps({"data": {"users": [user]}}))
    path = storage / "core.config_entries"
    path.write_text(
        json.dumps(
            {
                "data": {
                    "entries": [
                        {
                            "entry_id": "server",
                            "data": {"admin_token": "old-fixture-token"},
                            "options": {"webhook_auth": "none"},
                        }
                    ]
                }
            }
        )
    )
    return path


@pytest.fixture
def owner() -> dict:
    return {
        "id": "owner",
        "is_owner": True,
        "is_active": True,
        "system_generated": False,
        "group_ids": ["system-admin"],
    }


def test_native_staging_uses_human_owner_without_retaining_a_token(tmp_path, owner):
    path = _seed(tmp_path, owner)
    configure_native_entry(tmp_path, "server")
    entry = json.loads(path.read_text())["data"]["entries"][0]
    assert entry["data"] == {"native_owner_user_id": "owner"}
    assert entry["options"]["runtime_mode"] == "native"
    assert entry["options"]["webhook_auth"] == "none"
    assert not entry["options"]["enable_llm_api"]
    assert not entry["options"]["enable_native_llm_import"]


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"system_generated": True},
        {"group_ids": ["system-users"]},
        {"is_owner": False},
    ],
)
def test_missing_human_owner_refuses_before_rewriting_the_entry(
    tmp_path, owner, change
):
    path = _seed(tmp_path, {**owner, **change})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="human fixture owner"):
        configure_native_entry(tmp_path, "server")
    assert path.read_bytes() == before


def test_a_responding_legacy_server_cannot_qualify_as_native(monkeypatch, tmp_path):
    directory = tmp_path / "receipts"
    monkeypatch.setenv("E2E_ATTESTATION_DIRECTORY", str(directory))
    response = SimpleNamespace(
        raise_for_status=Mock(),
        json=lambda: {
            "manager": "EmbeddedServerManager",
            "runtime": "legacy",
            "running": True,
        },
    )
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: response,
    )
    with pytest.raises(AssertionError, match="did not attest"):
        attest_native_runtime("http://fixture.test", {"Authorization": "fixture"})
    assert not directory.exists()


@pytest.fixture
def native_observation():
    return {
        "manager": "NativeServerManager",
        "runtime": "native",
        "running": True,
        "on_core_loop": True,
        "legacy_listener": False,
        "legacy_thread": False,
        "admin_token_present": False,
        "revision": "candidate",
        "library_version": "candidate-version",
        "core_version": "2026.8.0",
        "python_version": "3.14.2 (Core image interpreter)",
        "architecture": "aarch64",
    }


@pytest.mark.parametrize("observed_core", [None, "2026.10.3"])
def test_a_native_server_on_the_wrong_core_cannot_qualify_the_minimum(
    monkeypatch, observed_core, native_observation, tmp_path
):
    directory = tmp_path / "receipts"
    monkeypatch.setenv("E2E_ATTESTATION_DIRECTORY", str(directory))
    observed = {**native_observation, "core_version": observed_core}
    monkeypatch.setenv("E2E_EXPECTED_CORE_VERSION", "2026.8.0")
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=Mock(), json=lambda: observed
        ),
    )
    with pytest.raises(AssertionError, match="Core version"):
        attest_native_runtime("http://fixture.test", {})
    assert not directory.exists()


def test_an_x64_container_cannot_qualify_the_arm64_lane(
    monkeypatch, native_observation, tmp_path
):
    directory = tmp_path / "receipts"
    monkeypatch.setenv("E2E_ATTESTATION_DIRECTORY", str(directory))
    monkeypatch.setenv("E2E_EXPECTED_ARCH", "ARM64")
    observed = {**native_observation, "architecture": "x86_64"}
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=Mock(), json=lambda: observed
        ),
    )
    with pytest.raises(AssertionError, match="architecture"):
        attest_native_runtime("http://fixture.test", {})
    assert not directory.exists()


@pytest.fixture
def receipt_world(
    monkeypatch: pytest.MonkeyPatch, native_observation: dict[str, Any], tmp_path: Path
) -> Path:
    directory = tmp_path / "receipts"
    native_observation.update(
        owner_id="private-owner",
        token="private-token",
        endpoint="http://private-endpoint",
        private_config={"password": "private-password"},
    )
    monkeypatch.setenv("E2E_ATTESTATION_DIRECTORY", str(directory))
    monkeypatch.setenv("E2E_EXPECTED_CORE_VERSION", native_observation["core_version"])
    monkeypatch.setenv("E2E_EXPECTED_ARCH", native_observation["architecture"])
    monkeypatch.setattr(
        "tests.src.e2e._conftest_native_runtime.requests.get",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=Mock(), json=lambda: native_observation
        ),
    )
    return directory


def test_attested_workers_preserve_actual_runtime_values_without_private_data(
    monkeypatch: pytest.MonkeyPatch,
    native_observation: dict[str, Any],
    receipt_world: Path,
) -> None:
    monkeypatch.setenv("GITHUB_SHA", "public-source-revision")
    monkeypatch.setenv("GITHUB_RUN_ID", "public-run-id")
    monkeypatch.setenv("GITHUB_TOKEN", "private-github-token")
    for worker in ("gw0", "gw1", None):
        if worker is None:
            monkeypatch.delenv("PYTEST_XDIST_WORKER")
        else:
            monkeypatch.setenv("PYTEST_XDIST_WORKER", worker)
        assert (
            attest_native_runtime("http://private-endpoint", {}) is native_observation
        )
    receipts = list(receipt_world.glob("*.json"))
    assert len(receipts) == 3
    observed_fields = {
        "core_version",
        "python_version",
        "architecture",
        "runtime",
        "on_core_loop",
        "revision",
        "library_version",
    }
    workers = set()
    for path in receipts:
        raw = path.read_text()
        payload = json.loads(raw)
        assert set(payload) == observed_fields | {"worker", "github"}
        assert {key: payload[key] for key in observed_fields} == {
            key: native_observation[key] for key in observed_fields
        }
        assert payload["github"]["sha"] == "public-source-revision"
        assert payload["github"]["run_id"] == "public-run-id"
        assert "private-" not in raw
        workers.add(payload["worker"])
    assert workers == {"gw0", "gw1", "master"}


def test_attestation_worker_cannot_escape_its_explicit_output_directory(
    monkeypatch: pytest.MonkeyPatch, receipt_world: Path
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "../escape")
    with pytest.raises(ValueError, match="worker"):
        attest_native_runtime("http://fixture.test", {})
    assert not receipt_world.exists()
    assert not list(receipt_world.parent.rglob("*.json"))


def test_attestation_cannot_save_a_receipt_without_the_actual_core_python(
    native_observation: dict[str, Any], receipt_world: Path
) -> None:
    native_observation.pop("python_version")
    with pytest.raises(AssertionError, match="python_version"):
        attest_native_runtime("http://fixture.test", {})
    assert not receipt_world.exists()
