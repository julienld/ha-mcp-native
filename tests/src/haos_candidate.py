"""Stage and attest paired checkout artifacts inside disposable HAOS images."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qualification_artifacts import (
    native_runtime_selected,
    stage_candidate_component,
    verify_candidate_artifacts,
)


def _guestfish(image: Path, commands: list[str]) -> None:
    subprocess.run(
        [
            "guestfish",
            "--rw",
            "-a",
            str(image),
            "run",
            ":",
            "mount",
            "/dev/sda8",
            "/",
            ":",
            *commands,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _configure_entry(
    document: dict[str, Any],
    auth: dict[str, Any],
    runtime: str,
    wheel: Path,
    token: str,
) -> None:
    entries = [
        entry
        for entry in document["data"]["entries"]
        if entry.get("entry_id") == "e2e_test_ha_mcp_server_entry"
    ]
    if len(entries) != 1:
        raise ValueError("HAOS image lacks its single disabled server entry")
    entry = entries[0]
    options = entry.setdefault("options", {})
    options["runtime_mode"] = runtime
    if runtime == "native":
        # Preserve a real historical upgrade in the native HAOS qualification.
        entry["minor_version"] = 1
        owners = [
            user
            for user in auth["data"]["users"]
            if user.get("is_owner")
            and user.get("is_active")
            and not user.get("system_generated")
        ]
        if len(owners) != 1:
            raise ValueError("Native HAOS qualification needs one active human owner")
        entry["data"]["native_owner_user_id"] = owners[0]["id"]
        entry["data"].pop("admin_token", None)
        for key in ("pip_spec", "server_port", "bind_host"):
            options.pop(key, None)
    else:
        entry["minor_version"] = 2
        options["pip_spec"] = f"ha-mcp @ file:///config/{wheel.name}"
        entry["data"]["admin_token"] = token


def stage_candidate_in_qcow2(image: Path, assets: Path, admin_token: str) -> None:
    """Deliver verified code and select runtime before Core can load the entry."""
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
    ).strip()
    proof = verify_candidate_artifacts(assets, expected_revision=revision)
    runtime = "native" if native_runtime_selected() else "legacy"
    with tempfile.TemporaryDirectory(prefix="haos-candidate-") as temporary:
        work = Path(temporary)
        config = work / "config"
        storage = config / ".storage"
        storage.mkdir(parents=True)
        root = "/supervisor/homeassistant"
        _guestfish(
            image,
            [
                "copy-out",
                f"{root}/.storage/core.config_entries",
                str(storage),
                ":",
                "copy-out",
                f"{root}/.storage/auth",
                str(work),
                ":",
                "copy-out",
                f"{root}/configuration.yaml",
                str(config),
            ],
        )
        wheel = stage_candidate_component(
            assets, config / "custom_components/ha_mcp_tools", config
        )
        entry_path = storage / "core.config_entries"
        document = json.loads(entry_path.read_text(encoding="utf-8"))
        auth = json.loads((work / "auth").read_text(encoding="utf-8"))
        _configure_entry(document, auth, runtime, wheel, admin_token)
        entry_path.write_text(json.dumps(document), encoding="utf-8")
        if runtime == "native":
            source = (
                Path(__file__).resolve().parents[1] / "fixtures/native_qualification"
            )
            shutil.copytree(source, config / "custom_components/native_qualification")
            configuration = config / "configuration.yaml"
            with configuration.open("a", encoding="utf-8") as handle:
                handle.write("\nnative_qualification:\n")
        archive = work / "candidate.tar"
        with tarfile.open(archive, "w") as tar:
            for child in config.iterdir():
                tar.add(child, arcname=child.name)
        _guestfish(
            image,
            [
                "rm-rf",
                f"{root}/custom_components/ha_mcp_tools",
                ":",
                "tar-in",
                str(archive),
                root,
            ],
        )
    from haos_runtime import _write_embedded_staging_status

    _write_embedded_staging_status(
        True,
        f"paired {runtime} candidate {proof['revision']} {proof['library_version']}",
    )


def attest_native_runtime(base_url: str, token: str, assets: Path) -> None:
    """Fail setup if the webhook is backed by legacy or a different candidate."""
    proof = verify_candidate_artifacts(assets)
    response = requests.get(
        f"{base_url}/api/native_qualification",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    observed = response.json()
    expected = {
        "manager": "NativeServerManager",
        "runtime": "native",
        "running": True,
        "on_core_loop": True,
        "legacy_listener": False,
        "legacy_thread": False,
        "admin_token_present": False,
        "library_version": proof["library_version"],
        "revision": proof["revision"],
    }
    if any(observed.get(key) != value for key, value in expected.items()):
        raise AssertionError(f"Native HAOS runtime attestation failed: {observed!r}")
    destination = Path("/tmp/haos-diagnostics")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "native-runtime.json").write_text(
        json.dumps(observed, indent=2), encoding="utf-8"
    )
