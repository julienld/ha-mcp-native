"""Code-only recovery images and a durable no-replay receipt, never hot rollback."""

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile


def read_receipt(directory: Path) -> dict[str, Any] | None:
    path = directory / "receipt.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def recovery_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: receipt[key]
        for key in ("snapshot_id", "snapshot_sha256", "state", "scope", "location")
    }


def component_source(expected: str) -> Path:
    source = Path(__file__).parent.resolve()
    if Path(expected).resolve() != source:
        raise ValueError(
            "The owner's installation path does not match the running component"
        )
    return source


def write_receipt(directory: Path, receipt: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f"receipt-{uuid.uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(receipt))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / "receipt.json")


def capture_code(
    source: Path, directory: Path, before: dict[str, Any]
) -> dict[str, Any]:
    if source.is_symlink() or not (source / "manifest.json").is_file():
        raise ValueError("Installed component files could not be identified")
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    archive = directory / f"{identifier}.zip"
    files = []
    total = 0
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError("A component symlink cannot be captured safely")
        if path.is_file():
            total += path.stat().st_size
            if total > 64 * 1024 * 1024:
                raise ValueError("Component recovery image exceeds the size limit")
            files.append(path)
    with ZipFile(archive, "x", compression=ZIP_DEFLATED) as output:
        for path in files:
            output.write(path, path.relative_to(source).as_posix())
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    receipt = {
        **before,
        "snapshot_id": identifier,
        "snapshot_sha256": checksum,
        "state": "prepared",
        "scope": "component_files_only",
        "location": f".storage/ha_mcp_self_update/{identifier}.zip",
    }
    write_receipt(directory, receipt)
    return receipt
