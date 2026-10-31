"""Read the historical component backups without a service or transport."""

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .const import ALLOWED_YAML_CONFIG_FILES
from .file_io import _read_file_sync
from .file_paths import _resolves_within

_LEGACY_BACKUP_DIRNAME = ".ha_mcp_tools_backups"
_LEGACY_BACKUP_RE = re.compile(r"^(?P<safe>.+)\.(?P<date>\d{8})_(?P<time>\d{6})\.bak$")


def _decode_legacy_backup_name(filename: str) -> dict[str, Any]:
    """Best-effort inverse of the legacy .bak naming.

    Returns ``{file_path, timestamp, path_ambiguous}``. ``file_path`` is the
    config-relative path the backup was taken from, or ``None`` when it can't be
    recovered. The original naming replaced every ``os.sep`` with ``_``, which is
    lossy: only the flat allowlisted shapes (``configuration.yaml``,
    ``packages/<name>.yaml``, ``themes/<name>.yaml``) invert unambiguously. A
    nested path (``packages/sub/foo.yaml``), a literal underscore in the name, or
    a pre-fix ``www/yaml_backups`` artifact cannot be distinguished and is
    flagged ``path_ambiguous`` so a caller never restores to a guessed target.
    """
    match = _LEGACY_BACKUP_RE.match(filename)
    if not match:
        return {"file_path": None, "timestamp": None, "path_ambiguous": True}
    safe = match.group("safe")
    timestamp = f"{match.group('date')}_{match.group('time')}"
    if safe in ALLOWED_YAML_CONFIG_FILES:
        return {"file_path": safe, "timestamp": timestamp, "path_ambiguous": False}
    # Mirror the subdir write-allowlist in handle_edit_yaml_config
    # (packages/*.yaml + themes/*.yaml) — keep in sync if that set grows. A
    # pattern outside this list just stays path_ambiguous (still listable/
    # readable), so drift degrades safely rather than mis-restoring.
    for prefix in ("packages", "themes"):
        if safe.startswith(f"{prefix}_"):
            rest = safe.removeprefix(f"{prefix}_")
            return {
                "file_path": f"{prefix}/{rest}",
                "timestamp": timestamp,
                # A remaining "_" is either a literal char or a collapsed nested
                # separator — indistinguishable, so the decode can't be trusted.
                "path_ambiguous": "_" in rest,
            }
    return {"file_path": None, "timestamp": timestamp, "path_ambiguous": True}


def _list_legacy_backups_sync(legacy_dir: Path) -> list[dict[str, Any]]:
    """Enumerate regular ``.bak`` files in the legacy backup dir (no recursion).

    Skips directories, symlinks, and non-``.bak`` strays (mirrors the GHSA
    migration's filter). Newest first by mtime.
    """
    if not legacy_dir.is_dir():
        return []
    backups: list[dict[str, Any]] = []
    for item in legacy_dir.iterdir():
        if not item.is_file() or item.is_symlink() or item.suffix != ".bak":
            continue
        stat = item.stat()
        decoded = _decode_legacy_backup_name(item.name)
        backups.append(
            {
                "filename": item.name,
                "file_path": decoded["file_path"],
                "path_ambiguous": decoded["path_ambiguous"],
                "timestamp": decoded["timestamp"],
                "size": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    backups.sort(key=lambda backup: backup["modified"], reverse=True)
    return backups


def _read_legacy_backup_sync(target_file: Path) -> dict[str, Any]:
    """Read a single legacy ``.bak`` file (text) for one executor offload.

    Rejects symlinks up front (defense-in-depth for this surface), then reuses
    ``_read_file_sync`` for the exists/is_file/stat/read.
    """
    if target_file.is_symlink():
        return {"_error": "not_a_file"}
    return _read_file_sync(target_file)


def read_legacy_backup(config_dir: Path, filename: str) -> dict[str, Any]:
    """Validate a bare backup name and read it inside one executor operation."""
    legacy_backup_dir = config_dir / _LEGACY_BACKUP_DIRNAME
    # Bare basename only, .bak only, confined to the legacy dir. Reject any
    # path separator, traversal, or symlink escape before touching the FS —
    # this surface only ever serves files inside .ha_mcp_tools_backups/.
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or filename in (".", "..")
        or not filename.endswith(".bak")
        or not _resolves_within(legacy_backup_dir, filename)
    ):
        return {
            "success": False,
            "error": (
                f"Invalid backup filename {filename!r}. Pass a bare "
                f"<name>.bak from {_LEGACY_BACKUP_DIRNAME}/ "
                "(no path separators)."
            ),
        }
    target_file = legacy_backup_dir / filename
    try:
        result = _read_legacy_backup_sync(target_file)
    except UnicodeDecodeError:
        return {
            "success": False,
            "error": f"Cannot read binary backup: {filename}.",
        }
    except OSError as err:
        return {"success": False, "error": str(err)}

    err_kind = result.get("_error")
    if err_kind == "not_found":
        return {"success": False, "error": f"Backup does not exist: {filename}"}
    if err_kind == "not_a_file":
        return {"success": False, "error": f"Path is not a file: {filename}"}

    modified_dt = datetime.fromtimestamp(result["mtime"])
    decoded = _decode_legacy_backup_name(filename)
    return {
        "success": True,
        "filename": filename,
        "file_path": decoded["file_path"],
        "path_ambiguous": decoded["path_ambiguous"],
        "timestamp": decoded["timestamp"],
        "content": result["content"],
        "size": result["size"],
        "modified": modified_dt.isoformat(),
    }
