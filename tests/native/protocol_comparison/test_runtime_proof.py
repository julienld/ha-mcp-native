"""Execute the real proof with isolated providers/files, without booting Core."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from .evidence import validate_runtime
from .protocol_fixture.provenance import (
    file_digest,
    tree_digest,
    wheel_package_digest,
)


@pytest.fixture
def proof_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    component = tmp_path / "custom_components/ha_mcp_tools"
    package = tmp_path / "ha_mcp"
    modules = {}
    for name, directory in (
        ("custom_components.ha_mcp_tools", component),
        ("ha_mcp", package),
    ):
        directory.mkdir(parents=True)
        source = directory / "__init__.py"
        source.write_text('"""Isolated proof provider."""\n', encoding="utf-8")
        module = ModuleType(name)
        module.__file__ = str(source)
        modules[name] = module
    for name, version in {
        "fastmcp": "4.1.2",
        "mcp": "2.3.0",
        "mcp_types": "2.3.0",
    }.items():
        marker = package / "_vendor" / name / "VENDORED"
        marker.parent.mkdir(parents=True)
        marker.write_text(f"{name}=={version}\n", encoding="utf-8")
    wheel = tmp_path / "paired.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path in package.rglob("*"):
            if path.is_file():
                archive.write(path, "ha_mcp/" + path.relative_to(package).as_posix())
    receipt: dict[str, Any] = {
        "run_nonce": "a" * 32,
        "revision": "b" * 40,
        "core_image_id": "sha256:" + "c" * 64,
        "source_clean_at_receipt": True,
        "component_sha256": tree_digest(component),
        "package_sha256": wheel_package_digest(wheel),
        "wheel_sha256": file_digest(wheel),
        "wheel_name": wheel.name,
    }
    (tmp_path / "protocol-build.json").write_text(json.dumps(receipt), encoding="utf-8")
    for name, module in {
        **modules,
        "custom_components": SimpleNamespace(
            ha_mcp_tools=modules["custom_components.ha_mcp_tools"]
        ),
        "ha_mcp._vendor.mcp": ModuleType("ha_mcp._vendor.mcp"),
        "homeassistant.components.http": SimpleNamespace(HomeAssistantView=object),
        "homeassistant.const": SimpleNamespace(__version__="2026.10.4"),
        "homeassistant.core": SimpleNamespace(SupportsResponse=object),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location(
        f"{__package__}.protocol_fixture._runtime_proof_test",
        Path(__file__).parent / "protocol_fixture/runtime.py",
    )
    assert spec and spec.loader
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    monkeypatch.setattr(runtime.importlib.metadata, "version", lambda _: "test-package")
    entries: dict[str, Any] = {}
    lookup = Mock(side_effect=entries.get)
    hass = SimpleNamespace(
        config=SimpleNamespace(path=lambda name: str(tmp_path / name)),
        config_entries=SimpleNamespace(async_get_entry=lookup),
        data={"ha_mcp_tools": {}},
    )
    return SimpleNamespace(
        runtime=runtime, hass=hass, entries=entries, lookup=lookup, receipt=receipt
    )


@pytest.fixture(params=["legacy", "native"])
def owned_manager(
    proof_world: SimpleNamespace, request: pytest.FixtureRequest
) -> SimpleNamespace:
    entry = SimpleNamespace(
        entry_id="server-entry", options={"runtime_mode": request.param}
    )
    # U2 EmbeddedServerManager owns _entry; NativeServerManager exposes entry.
    manager = SimpleNamespace(is_running=True)
    attribute = "_entry" if request.param == "legacy" else "entry"
    setattr(manager, attribute, entry)
    if request.param == "native":
        manager.engine = object()
    proof_world.hass.data["ha_mcp_tools"]["manager"] = manager
    proof_world.entries[entry.entry_id] = entry
    return SimpleNamespace(
        entry=entry,
        manager=manager,
        attribute=attribute,
        expected={**proof_world.receipt, "runtime": request.param},
    )


def test_owned_manager_entry_passes_real_runtime_attestation(
    proof_world: SimpleNamespace,
    owned_manager: SimpleNamespace,
) -> None:
    observed = proof_world.runtime.runtime_proof(proof_world.hass)
    assert observed["entry_runtime"] == owned_manager.entry.options["runtime_mode"]
    assert (
        validate_runtime(observed, owned_manager.expected)["runtime"]
        == owned_manager.expected["runtime"]
    )


@pytest.mark.parametrize(
    "defect", ["manager_entry_absent", "core_entry_absent", "same_id_replacement"]
)
def test_missing_or_replaced_entry_cannot_attest_ownership(
    proof_world: SimpleNamespace,
    owned_manager: SimpleNamespace,
    defect: str,
) -> None:
    if defect == "manager_entry_absent":
        delattr(owned_manager.manager, owned_manager.attribute)
    elif defect == "core_entry_absent":
        proof_world.entries.clear()
    else:
        replacement = deepcopy(owned_manager.entry)
        assert (
            replacement == owned_manager.entry
            and replacement is not owned_manager.entry
        )
        proof_world.entries[replacement.entry_id] = replacement
    with pytest.raises(ValueError, match="runtime_entry_owner_mismatch"):
        proof_world.runtime.runtime_proof(proof_world.hass)
    if defect != "manager_entry_absent":
        proof_world.lookup.assert_called_once_with(owned_manager.entry.entry_id)


def test_entry_options_are_observed_instead_of_inferred_from_manager_kind(
    proof_world: SimpleNamespace,
    owned_manager: SimpleNamespace,
) -> None:
    contrary = "native" if owned_manager.expected["runtime"] == "legacy" else "legacy"
    owned_manager.entry.options["runtime_mode"] = contrary
    observed = proof_world.runtime.runtime_proof(proof_world.hass)
    assert observed["entry_runtime"] == contrary
    with pytest.raises(ValueError, match="entry_runtime_mismatch"):
        validate_runtime(observed, owned_manager.expected)


def test_absent_public_entry_is_not_replaced_by_private_fallback(
    proof_world: SimpleNamespace,
) -> None:
    entry = SimpleNamespace(entry_id="server-entry", options={"runtime_mode": "legacy"})
    proof_world.entries[entry.entry_id] = entry
    proof_world.hass.data["ha_mcp_tools"]["manager"] = SimpleNamespace(
        entry=None, _entry=entry, is_running=True
    )
    with pytest.raises(ValueError, match="runtime_entry_owner_mismatch"):
        proof_world.runtime.runtime_proof(proof_world.hass)


def test_entry_without_runtime_option_retains_legacy_interpretation(
    proof_world: SimpleNamespace,
    owned_manager: SimpleNamespace,
) -> None:
    owned_manager.entry.options.clear()
    observed = proof_world.runtime.runtime_proof(proof_world.hass)
    assert observed["entry_runtime"] == "legacy"
    if owned_manager.expected["runtime"] == "native":
        with pytest.raises(ValueError, match="entry_runtime_mismatch"):
            validate_runtime(observed, owned_manager.expected)
    else:
        assert validate_runtime(observed, owned_manager.expected)["runtime"] == "legacy"


async def test_read_only_probe_reloads_the_same_owned_entry_for_both_runtimes(
    proof_world: SimpleNamespace,
    owned_manager: SimpleNamespace,
    tmp_path: Path,
) -> None:
    hass = proof_world.hass
    hass.config_entries.async_unload = AsyncMock(return_value=True)
    hass.config_entries.async_setup = AsyncMock(return_value=True)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn: fn())
    await proof_world.runtime.set_read_only(hass, True)
    flags = json.loads((tmp_path / ".ha_mcp/feature_flags.json").read_bytes())
    assert flags["read_only_mode"] is True
    hass.config_entries.async_unload.assert_awaited_once_with(
        owned_manager.entry.entry_id
    )
    hass.config_entries.async_setup.assert_awaited_once_with(
        owned_manager.entry.entry_id
    )
