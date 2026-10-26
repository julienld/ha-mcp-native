"""Container rebuilds may change Python patches without changing the Core tag."""

import json
from pathlib import Path
from typing import Any

import pytest

from tests.native import runtime_attestation


@pytest.fixture
def image_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    actual = {
        "core": "2026.10.0",
        "core_requires_python": ">=3.14.2",
        "python": "3.14.6",
        "implementation": "cpython",
        "cache_tag": "cpython-314",
        "soabi": "cpython-314-x86_64-linux-musl",
        "arch": "amd64",
    }
    image = {
        "Id": "sha256:" + "a" * 64,
        "RepoDigests": ["ghcr.io/home-assistant/home-assistant@sha256:" + "b" * 64],
        "Architecture": "amd64",
        "Os": "linux",
    }
    receipt = tmp_path / "image.json"
    receipt.write_text(json.dumps([image]))
    monkeypatch.setattr(runtime_attestation, "inspect_runtime", lambda: actual)

    def attest(expected_arch: str = "amd64") -> dict[str, Any]:
        return runtime_attestation.attest_runtime(
            receipt,
            expected_core="2026.10.0",
            expected_arch=expected_arch,
            python_source="3.14.5",
            requires_python=">=3.13,<3.15",
        )

    return actual, image, receipt, attest


def test_rebuilt_image_patch_is_recorded_with_immutable_image_receipt(
    image_runtime: Any,
) -> None:
    _, image, _, attest = image_runtime
    proof = attest()
    assert proof["runtime"]["python"] == "3.14.6"
    assert proof["python_source_hint"] == "3.14.5"
    assert proof["image_id"] == image["Id"]
    assert proof["repo_digests"] == image["RepoDigests"]
    assert proof["verified"] is True


def test_arm_image_receipt_and_runtime_use_the_same_architecture(
    image_runtime: Any,
) -> None:
    actual, image, receipt, attest = image_runtime
    actual.update(arch="aarch64", soabi="cpython-314-aarch64-linux-musl")
    image["Architecture"] = "arm64"
    receipt.write_text(json.dumps([image]))
    assert attest("aarch64")["verified"]
    image["Architecture"] = "amd64"
    receipt.write_text(json.dumps([image]))
    with pytest.raises(ValueError, match="platform"):
        attest("aarch64")


@pytest.mark.parametrize(
    "key,value",
    [
        ("core", "2026.10.4"),
        ("arch", "aarch64"),
        ("python", "3.13.9"),
        ("python", "3.15.0"),
        ("implementation", "pypy"),
        ("soabi", "cpython-314t-x86_64-linux-musl"),
        ("core_requires_python", ""),
    ],
)
def test_wrong_core_arch_or_incompatible_python_cannot_be_attested(
    image_runtime: Any, key: str, value: str
) -> None:
    actual, _, _, attest = image_runtime
    actual[key] = value
    with pytest.raises(ValueError):
        attest()


@pytest.mark.parametrize("missing", ["file", "Id", "RepoDigests"])
def test_runtime_without_image_identity_cannot_qualify(
    image_runtime: Any, missing: str
) -> None:
    _, image, receipt, attest = image_runtime
    if missing == "file":
        receipt.unlink()
    else:
        image.pop(missing)
        receipt.write_text(json.dumps([image]))
    with pytest.raises((ValueError, OSError)):
        attest()
