"""Only exact installed wheel receipts can avoid a paired URL reinstall."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

_URL = "https://github.com/julienld/ha-mcp-native/releases/download/9.0.0.dev1/ha_mcp-9.0.0.dev1-py3-none-any.whl"
_DIGEST = "abc01234" * 8
_SPEC = "ha-mcp @ " + _URL + "#sha256=" + _DIGEST


@pytest.fixture
def paired(monkeypatch):
    path = (
        Path(__file__).resolve().parents[3]
        / "custom_components/ha_mcp_tools/paired_wheel.py"
    )
    spec = importlib.util.spec_from_file_location("paired_wheel_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    receipt = {"url": _URL, "archive_info": {"hashes": {"sha256": _DIGEST}}}
    distribution = SimpleNamespace(
        version="9.0.0.dev1",
        metadata={"Name": "ha-mcp"},
        read_text=lambda _name: json.dumps(receipt),
    )
    monkeypatch.setattr(
        module.importlib.metadata, "distribution", lambda _name: distribution
    )
    return module, receipt, distribution


def test_exact_paired_wheel_receipt_is_read_without_importing_runtime(paired):
    module, _receipt, _distribution = paired
    assert module.paired_wheel_matches(_SPEC, "9.0.0.dev1", "9.0.0.dev1")


@pytest.mark.parametrize(
    "change",
    [
        "url",
        "hash",
        "loaded_version",
        "distribution_version",
        "vcs",
        "missing",
        "conflicting_legacy_hash",
    ],
)
def test_changed_or_unproven_installation_never_qualifies(paired, change):
    module, receipt, distribution = paired
    loaded = "9.0.0.dev1"
    if change == "url":
        receipt["url"] = _URL.replace("ha-mcp-native", "other-repository")
    elif change == "hash":
        receipt["archive_info"]["hashes"]["sha256"] = "0" * 64
    elif change == "loaded_version":
        loaded = "9.0.0.dev2"
    elif change == "distribution_version":
        distribution.version = "9.0.0.dev2"
    elif change == "vcs":
        receipt["vcs_info"] = {"vcs": "git", "commit_id": "a" * 40}
    elif change == "missing":
        receipt.clear()
    else:
        receipt["archive_info"]["hash"] = "sha256=" + "0" * 64
    assert not module.paired_wheel_matches(_SPEC, loaded, "9.0.0.dev1")


@pytest.mark.parametrize(
    "spec",
    [
        _URL,
        "ha-mcp==9.0.0.dev1",
        "ha-mcp @ " + _URL,
        _SPEC.replace("https:", "http:"),
        _SPEC.replace("github.com/", "credential@github.com/"),
        _SPEC.replace("#sha256=", "?signature=expires#sha256="),
    ],
)
def test_mutable_unhashed_or_credential_bearing_references_do_not_qualify(paired, spec):
    module, *_ = paired
    assert not module.paired_wheel_matches(spec, "9.0.0.dev1", "9.0.0.dev1")


def test_legacy_pep610_hash_field_is_accepted_only_when_no_hashes_map_exists(paired):
    module, receipt, _distribution = paired
    receipt["archive_info"] = {"hash": "sha256=" + _DIGEST}
    assert module.paired_wheel_matches(_SPEC, "9.0.0.dev1", "9.0.0.dev1")
