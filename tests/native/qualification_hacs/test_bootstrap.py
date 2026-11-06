"""Disposable bootstrap regressions; these do not run Core or Docker."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


def test_loopback_selection_prevents_core_default_interface_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Core must receive its network preference before its DNS resolver starts."""
    root = Path(__file__).parent
    spec = importlib.util.spec_from_file_location(
        "hacs_bootstrap", root / "bootstrap.py"
    )
    assert spec and spec.loader
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    components = tmp_path / "image"
    components.mkdir()
    monkeypatch.setattr(
        bootstrap,
        "shutil",
        SimpleNamespace(
            copytree=lambda _source, target: shutil.copytree(components, target)
        ),
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "content", SimpleNamespace(seed_repositories=dict))

    config = tmp_path / "config"
    bootstrap.prepare(config)
    network_path = config / ".storage" / "core.network"
    network = json.loads(network_path.read_text(encoding="utf-8"))
    # Core 2026.10.4's network store, not a fixture-only setting. With no
    # selected adapter Core requests InterfaceChoice.Default and mDNS fails
    # in Docker's network-none namespace. Its real loopback is named lo.
    assert network == {
        "version": 1,
        "minor_version": 1,
        "key": "core.network",
        "data": {"configured_adapters": ["lo"]},
    }
    bootstrap.prepare(config)
    assert json.loads(network_path.read_text(encoding="utf-8")) == network
