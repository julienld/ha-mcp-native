"""A generated payload index cannot approve unconfined or installer-owned bytes."""

import json
import stat
import zipfile

import pytest

from .test_native_release import release_builder as release_builder

_VERSION = "9.0.0.dev3"
_DIST = "ha_mcp-9.0.0.dev3.dist-info"


def wheel(tmp_path, entries=()):
    path = tmp_path / "candidate.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("ha_mcp/__init__.py", _DIST + "/METADATA", _DIST + "/WHEEL"):
            archive.writestr(name, b"fixture\n")
        archive.writestr(_DIST + "/RECORD", b"rewritten by installer")
        for name, data in entries:
            member = name
            if isinstance(name, str) and "\\" in name:
                unsafe = zipfile.ZipInfo("placeholder")
                unsafe.filename = unsafe.orig_filename = name
                member = unsafe
            archive.writestr(member, data)
    return path


@pytest.mark.parametrize(
    "member",
    [
        "../escape.py",
        "ha_mcp/../escape.py",
        "ha_mcp\\escape.py",
        "other/file.py",
        "ha_mcp-8.7.0.dist-info/METADATA",
        "ha_mcp/module.pyc",
        "ha_mcp/__pycache__/module.cpython-314.pyc",
        _DIST + "/INSTALLER",
        _DIST + "/direct_url.json",
        _DIST + "/REQUESTED",
        _DIST + "/uv_cache.json",
        _DIST + "/uv_build.json",
    ],
)
def test_invalid_payload_member_prevents_sidecar_creation(
    tmp_path, release_builder, member
):
    path = wheel(tmp_path, [(member, b"invalid")])
    sidecar = tmp_path / "native-wheel.json"
    with pytest.raises(ValueError):
        release_builder.write_wheel_index(path, sidecar, _VERSION)
    assert not sidecar.exists()


def test_symbolic_link_is_not_approved_as_regular_package_content(
    tmp_path, release_builder
):
    link = zipfile.ZipInfo("ha_mcp/redirect.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = wheel(tmp_path, [(link, b"outside")])
    with pytest.raises(ValueError, match="symbolic"):
        release_builder.write_wheel_index(path, tmp_path / "index.json", _VERSION)


@pytest.mark.parametrize(
    "limit,value",
    [
        ("MAX_WHEEL_INDEX_FILES", 2),
        ("MAX_WHEEL_PAYLOAD_BYTES", 4),
        ("MAX_WHEEL_INDEX_BYTES", 16),
    ],
)
def test_consumer_bounds_are_enforced_before_emitting_sidecar(
    tmp_path, release_builder, monkeypatch, limit, value
):
    path = wheel(tmp_path)
    monkeypatch.setattr(release_builder, limit, value)
    with pytest.raises(ValueError, match="bound"):
        release_builder.write_wheel_index(path, tmp_path / "index.json", _VERSION)


def test_directory_entries_and_installed_RECORD_are_not_payload_members(
    tmp_path, release_builder
):
    path = wheel(tmp_path, [("ha_mcp/resources/", b"")])
    target = tmp_path / "index.json"
    release_builder.write_wheel_index(path, target, _VERSION)
    index = json.loads(target.read_text())
    assert set(index["files"]) == {
        "ha_mcp/__init__.py",
        _DIST + "/METADATA",
        _DIST + "/WHEEL",
    }
