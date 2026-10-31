"""The local qualification verifier receives Core's exact hashed file URL."""

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.src.e2e.workflows.embedded import test_embedded_no_stomp as guard


@pytest.fixture
def installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    version = "9.0.0.dev1"
    wheel = f"ha_mcp-{version}-py3-none-any.whl"
    digest = hashlib.sha256(b"qualification-wheel").hexdigest()
    url = f"file:///config/{wheel}#sha256={digest}"
    component = tmp_path / "custom_components/ha_mcp_tools"
    component.mkdir(parents=True)
    manifest = component / "manifest.json"
    manifest.write_text(json.dumps({"requirements": ["ha-mcp @ " + url]}))
    (tmp_path / "native-build.json").write_text(
        json.dumps({"library_version": version, "files": {wheel: digest}})
    )
    receipt_file = (
        Path(__file__).resolve().parents[2]
        / "native/fixtures/native_fixture/dependency_receipt.py"
    )
    spec = importlib.util.spec_from_file_location(
        "local_receipt_verifier", receipt_file
    )
    assert spec and spec.loader
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    # Local uv file requirements retain their hash fragment in the receipt URL.
    direct = {"url": url, "archive_info": {"hashes": {"sha256": digest}}}
    distribution = SimpleNamespace(
        metadata={"Name": "ha-mcp"},
        version=version,
        read_text=lambda _: json.dumps(direct),
    )
    monkeypatch.setattr(
        verifier, "metadata", SimpleNamespace(distribution=lambda _: distribution)
    )

    def execute(command):
        tree = ast.parse(command[-1])
        call = next(
            node
            for node in ast.walk(tree.body[-1])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "wheel_receipt"
        )
        expected = ast.literal_eval(call.args[0])
        try:
            receipt = verifier.wheel_receipt(expected)
        except ValueError as error:
            return SimpleNamespace(exit_code=1, output=str(error).encode())
        return SimpleNamespace(exit_code=0, output=json.dumps(receipt).encode())

    machine = SimpleNamespace(exec_run=Mock(side_effect=execute))
    info = {
        "config_path": str(tmp_path),
        "container": SimpleNamespace(get_wrapped_container=lambda: machine),
    }
    return info, {"websockets": "15.0.1"}, {"ha-mcp": version}, manifest, machine


def test_local_core_receipt_retains_the_manifest_hash_fragment(installation):
    info, before, runtime, _manifest, machine = installation
    guard._assert_native_managed_install(info, before, runtime)
    machine.exec_run.assert_called_once()


def test_local_receipt_does_not_accept_a_manifest_pointing_at_other_bytes(installation):
    info, before, runtime, manifest, machine = installation
    manifest.write_text(
        json.dumps({"requirements": ["ha-mcp @ file:///config/other.whl"]})
    )
    with pytest.raises(AssertionError, match="manifest"):
        guard._assert_native_managed_install(info, before, runtime)
    machine.exec_run.assert_not_called()
