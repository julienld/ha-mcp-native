"""An empty uv archive receipt requires independently verified installed bytes."""

import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from .test_paired_wheel import _DIGEST, _SPEC
from .test_paired_wheel import paired as paired
from .test_paired_wheel_managed_install import managed as managed


def _prepare_payload(site, index_path):
    contents = {
        "ha_mcp/__init__.py": b"# original package\n",
        "ha_mcp/tool.py": b"VALUE = 1\n",
        "ha_mcp-9.0.0.dev1.dist-info/METADATA": b"Name: ha-mcp\nVersion: 9.0.0.dev1\n",
        "ha_mcp-9.0.0.dev1.dist-info/WHEEL": b"Wheel-Version: 1.0\n",
    }
    for name, data in contents.items():
        path = site / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    index = {
        "format_version": 1,
        "distribution": "ha-mcp",
        "version": "9.0.0.dev1",
        "wheel_sha256": _DIGEST,
        "files": {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in contents.items()
        },
    }
    index_path.write_text(json.dumps(index))
    return index


@pytest.fixture
def wheel_world(paired, tmp_path, monkeypatch):
    module, receipt, distribution = paired
    payload = __import__(
        module.__package__ + ".wheel_payload", fromlist=["wheel_payload"]
    )
    site = Path(distribution.locate_file(""))
    index_path = tmp_path / "native-wheel.json"
    index = _prepare_payload(site, index_path)
    monkeypatch.setattr(payload, "INDEX_PATH", index_path)
    receipt["archive_info"] = {}
    modules = {
        "ha_mcp": SimpleNamespace(
            __file__=str(site / "ha_mcp/__init__.py"), __path__=[str(site / "ha_mcp")]
        ),
    }
    monkeypatch.setattr(payload, "sys", SimpleNamespace(modules=modules))
    original_read = distribution.read_text

    def read_metadata(name):
        assert name != "RECORD", (
            "installed RECORD cannot establish independent evidence"
        )
        return original_read(name)

    distribution.read_text = read_metadata
    return SimpleNamespace(
        module=module,
        payload=payload,
        receipt=receipt,
        distribution=distribution,
        site=site,
        index=index,
        index_path=index_path,
        modules=modules,
    )


def qualifies(world):
    return world.module.paired_wheel_matches(_SPEC, "9.0.0.dev1", "9.0.0.dev1")


def test_empty_uv_receipt_qualifies_only_with_real_matching_payload(wheel_world):
    proof = wheel_world.payload.verify_wheel_payload(
        wheel_world.distribution, "ha-mcp", "9.0.0.dev1", _DIGEST
    )
    assert proof["files_checked"] == len(wheel_world.index["files"])
    assert qualifies(wheel_world)
    assert wheel_world.receipt["archive_info"] == {}
    assert set(wheel_world.modules) == {"ha_mcp"}


@pytest.mark.parametrize(
    "change",
    [
        "changed_bytes",
        "missing_file",
        "extra_module",
        "missing_index",
        "wrong_index_digest",
        "wrong_index_version",
        "traversal",
        "oversized",
        "duplicate_key",
    ],
)
def test_unproven_or_changed_payload_never_qualifies(wheel_world, change):
    world = wheel_world
    target = world.site / "ha_mcp/tool.py"
    if change == "changed_bytes":
        target.write_bytes(b"VALUE = 2\n")
    elif change == "missing_file":
        target.unlink()
    elif change == "extra_module":
        (world.site / "ha_mcp/stale.py").write_text("# old version")
    elif change == "missing_index":
        world.index_path.unlink()
    elif change == "duplicate_key":
        raw = world.index_path.read_text()
        world.index_path.write_text(raw[:-1] + ',"format_version":1}')
    else:
        if change == "wrong_index_digest":
            world.index["wheel_sha256"] = "0" * 64
        elif change == "wrong_index_version":
            world.index["version"] = "9.0.0.dev2"
        elif change == "traversal":
            world.index["files"]["ha_mcp/../../outside"] = world.index["files"].pop(
                "ha_mcp/tool.py"
            )
        else:
            world.index["files"]["ha_mcp/tool.py"]["size"] = (
                world.payload.MAX_PAYLOAD_BYTES + 1
            )
        world.index_path.write_text(json.dumps(world.index))
    assert not qualifies(world)


@pytest.mark.parametrize(
    "info",
    [
        {"hashes": {"sha256": "0" * 64}},
        {"hashes": {"sha256": _DIGEST}, "hash": "sha256=" + "0" * 64},
        {"hashes": {"sha256": "0" * 64}, "hash": "sha256=" + _DIGEST},
        {"hashes": {"sha256": None}},
    ],
)
def test_payload_cannot_override_a_contradictory_or_malformed_receipt(
    wheel_world, info
):
    wheel_world.receipt["archive_info"] = info
    assert not qualifies(wheel_world)


@pytest.mark.parametrize("hashed", [False, True])
def test_shadow_import_rejected_even_with_valid_archive_receipt(
    wheel_world, tmp_path, hashed
):
    world = wheel_world
    other = tmp_path / "shadow.py"
    other.write_text("# same version in a different directory")
    world.modules["ha_mcp.tool"] = SimpleNamespace(__file__=str(other))
    if hashed:
        world.receipt["archive_info"] = {"hashes": {"sha256": _DIGEST}}
    assert not qualifies(world)


def test_package_path_extension_cannot_load_unverified_modules(wheel_world, tmp_path):
    wheel_world.modules["ha_mcp"].__path__.append(str(tmp_path))
    assert not qualifies(wheel_world)


def test_installers_record_and_generated_bytecode_do_not_replace_payload_proof(
    wheel_world,
):
    world = wheel_world
    (world.site / "ha_mcp-9.0.0.dev1.dist-info/RECORD").write_text(
        "arbitrary installer record"
    )
    cache = world.site / "ha_mcp/__pycache__"
    cache.mkdir()
    (cache / "tool.cpython-314.pyc").write_bytes(b"derived cache")
    assert qualifies(world)
    (world.site / "ha_mcp/tool.py").write_bytes(b"VALUE = 2\n")
    assert not qualifies(world)


def test_unindexed_file_inside_cache_is_not_silently_ignored(wheel_world):
    cache = wheel_world.site / "ha_mcp/__pycache__"
    cache.mkdir()
    (cache / "unexpected.py").write_text("# executable file")
    assert not qualifies(wheel_world)


async def test_uv_payload_reuse_reads_and_hashes_in_the_managers_executor(
    managed, tmp_path, monkeypatch
):
    from custom_components.ha_mcp_tools import wheel_payload

    manager, env, _entry, receipt, threads = managed
    index_path = tmp_path / "native-wheel.json"
    _prepare_payload(tmp_path / "installed", index_path)
    monkeypatch.setattr(wheel_payload, "INDEX_PATH", index_path)
    receipt["archive_info"] = {}
    original = wheel_payload._file_matches
    hashed_on = []

    def observed(*args):
        hashed_on.append(threading.get_ident())
        return original(*args)

    monkeypatch.setattr(wheel_payload, "_file_matches", observed)
    assert await manager._async_ensure_managed_package() == "9.0.0.dev1"
    assert env.calls == [] and len(hashed_on) == 4
    assert all(thread != threading.get_ident() for thread in [*threads, *hashed_on])
