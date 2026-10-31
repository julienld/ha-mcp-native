"""Fork origin is proved by existing paired-artifact evidence, never a label."""

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from .test_native_engine_backend import native as native


@pytest.fixture
def provenance(native: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    module = importlib.import_module(
        "custom_components.ha_mcp_tools.native_self_update_provenance"
    )
    monkeypatch.setattr(
        module, "__file__", str(tmp_path / "native_self_update_provenance.py")
    )
    monkeypatch.setattr(module, "_owns_ha_mcp", lambda name: name == "ha-mcp")
    monkeypatch.setattr(
        module.importlib.metadata,
        "distribution",
        lambda name: SimpleNamespace(version="7.0.0"),
    )
    paired = Mock(return_value=True)
    monkeypatch.setattr(module, "paired_wheel_matches", paired)
    spec = (
        "ha-mcp @ https://github.com/example/my-fork/releases/download/v7.0.0/ha_mcp-7.0.0-py3-none-any.whl#sha256="
        + "a" * 64
    )
    manifest = {
        "domain": "ha_mcp_tools",
        "version": "7.0.0",
        "documentation": "https://github.com/example/my-fork",
        "requirements": [spec],
    }
    return SimpleNamespace(
        module=module,
        manifest=manifest,
        path=tmp_path / "manifest.json",
        paired=paired,
        spec=spec,
    )


def test_installed_fork_requires_the_startup_wheel_proof(provenance: Any) -> None:
    provenance.path.write_text(json.dumps(provenance.manifest), encoding="utf-8")
    assert provenance.module.paired_fork_origin("example/my-fork", "v7.0.0")
    provenance.paired.assert_called_once_with(provenance.spec, "7.0.0", "7.0.0")
    provenance.paired.return_value = False
    assert not provenance.module.paired_fork_origin("example/my-fork", "v7.0.0")


@pytest.mark.parametrize(
    "defect",
    ["wheel_origin", "component_domain", "documentation_origin", "installed_version"],
)
def test_unpaired_or_cross_repository_metadata_cannot_authorize_a_fork(
    provenance: Any, defect: str
) -> None:
    if defect == "wheel_origin":
        provenance.manifest["requirements"] = [
            provenance.spec.replace("example/my-fork", "homeassistant-ai/ha-mcp")
        ]
    elif defect == "component_domain":
        provenance.manifest["domain"] = "another_component"
    elif defect == "documentation_origin":
        provenance.manifest["documentation"] = (
            "https://github.com/homeassistant-ai/ha-mcp"
        )
    else:
        provenance.manifest["version"] = "7.0.1"
    provenance.path.write_text(json.dumps(provenance.manifest), encoding="utf-8")
    assert not provenance.module.paired_fork_origin("example/my-fork", "7.0.0")
    provenance.paired.assert_not_called()
