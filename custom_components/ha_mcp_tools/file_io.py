"""Original component file producers; blocking operations run in an executor."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ha_mcp.tool_api import ToolFailure

from .const import DENY_PATH_SEGMENTS, DENY_READ_BASENAMES

TAILED_LOG_FILES = frozenset({"home-assistant.log", "home-assistant.log.fault"})


DEFAULT_LOG_TAIL_LINES = 1000


@dataclass(frozen=True, slots=True)
class FileImage:
    """Detached file identity and content used across an awaited safety capture."""

    real_path: Path = field(repr=False)
    stamp: tuple[int, int, int] | None
    digest: str | None
    text: str | None = field(repr=False)


@dataclass(slots=True)
class FileWriteState:
    started: bool = False
    committed: bool = False


def read_file_image(target: Path) -> FileImage:
    real = target.resolve()
    try:
        before = target.stat()
    except FileNotFoundError:
        return FileImage(real, None, None, None)
    content = target.read_bytes()
    after = target.stat()
    stamp = (after.st_ino, after.st_size, after.st_mtime_ns)
    if (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != stamp or target.resolve() != real:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "File changed while its before-image was being read",
            status="not_started",
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        # Historical file snapshots contain text only. Binary targets still
        # retain a digest for the comparison before an explicit write/delete.
        text = None
    return FileImage(real, stamp, hashlib.sha256(content).hexdigest(), text)


def _check_file_image(target: Path, expected: FileImage | None) -> None:
    if expected is not None and read_file_image(target) != expected:
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "File changed after its before-image",
            status="not_started",
        )


def _list_files_sync(
    target_dir: Path, config_dir: Path, pattern: str | None
) -> dict[str, Any]:
    """Bundle list_files blocking I/O for a single executor offload.

    Returns either {"files": [...]} or {"_error": <kind>} so the async caller
    can format the structured error response without re-entering the executor.
    """
    if not target_dir.exists():
        return {"_error": "not_found"}
    if not target_dir.is_dir():
        return {"_error": "not_a_dir"}
    files: list[dict[str, Any]] = []
    for item in target_dir.iterdir():
        if pattern and not fnmatch.fnmatch(item.name, pattern):
            continue
        # The deny floor gates the directory being listed, not the entries
        # coming back from it, so a name it protects would otherwise be
        # enumerable inside an allowed directory -- reporting its size and
        # mtime for a file no handler will open. Matched on the entry's own
        # name, which is what the floor denies; nothing here resolves a
        # symlink, because a link's own name is not the protected one and
        # reading through it is refused anyway.
        lowered = item.name.lower()
        if lowered in DENY_READ_BASENAMES or lowered in DENY_PATH_SEGMENTS:
            continue
        stat = item.stat()
        # Config-relative paths report relative to the config dir; absolute
        # HAOS sibling-volume paths (issue #1586) are not under it, so report
        # them absolute (the caller passes absolute paths for volumes too).
        try:
            reported_path = str(item.relative_to(config_dir))
        except ValueError:
            reported_path = str(item)
        files.append(
            {
                "name": item.name,
                "path": reported_path,
                "is_dir": item.is_dir(),
                "size": stat.st_size if item.is_file() else 0,
                "modified": stat.st_mtime,
            }
        )
    files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"files": files}


def _read_file_sync(target_file: Path) -> dict[str, Any]:
    """Bundle read_file blocking I/O for a single executor offload."""
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    content = target_file.read_text()
    return {"content": content, "size": stat.st_size, "mtime": stat.st_mtime}


def _read_log_tail_sync(
    target_file: Path, tail_lines: int, *, chunk_size: int = 1 << 16
) -> dict[str, Any]:
    """Tail a log for one executor offload without materialising the whole file.

    HA Core appends to ``home-assistant.log.fault`` for the life of the
    install, so reading the file whole just to keep its last ``tail_lines``
    lines scales with the crash history rather than the request. Line count
    streams the file in chunks; the tail seeks back from the end until it holds
    ``tail_lines`` newlines. The result carries the same ``content`` /
    ``total_lines`` / ``truncated`` the whole-file split produced (``split("\n")``
    semantics, so a trailing newline yields one empty last line), plus
    ``size``/``mtime`` like ``_read_file_sync``.
    """
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    newlines = 0
    with target_file.open("rb") as fh:
        for chunk in iter(lambda: fh.read(chunk_size), b""):
            newlines += chunk.count(b"\n")
        total_lines = newlines + 1
        pos = fh.tell()
        buf = b""
        while pos > 0 and buf.count(b"\n") < tail_lines:
            step = min(chunk_size, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    parts = buf.split(b"\n")
    truncated = total_lines > tail_lines
    if truncated:
        # The leading part may start mid-character at a chunk boundary; it is
        # never part of the tail, so decode only what is returned.
        parts = parts[-tail_lines:]
    content = b"\n".join(parts).decode("utf-8")
    return {
        "content": content,
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "total_lines": total_lines,
        "truncated": truncated,
    }


def _write_file_sync(
    target_file: Path,
    content: str,
    overwrite: bool,
    create_dirs: bool,
    config_dir: Path,
    *,
    expected: FileImage | None = None,
    state: FileWriteState | None = None,
) -> dict[str, Any]:
    """Bundle write_file blocking I/O for a single executor offload."""
    _check_file_image(target_file, expected)
    exists = target_file.exists()
    if exists and not overwrite:
        return {"_error": "exists_no_overwrite"}
    if create_dirs:
        if state is not None:
            state.started = True
        target_file.parent.mkdir(parents=True, exist_ok=True)
    elif not target_file.parent.exists():
        # Config-relative parents report relative to the config dir; an absolute
        # HAOS sibling-volume parent (issue #1586) is not under it, so report it
        # absolute rather than raising ValueError on relative_to.
        try:
            parent = str(target_file.parent.relative_to(config_dir))
        except ValueError:
            parent = str(target_file.parent)
        return {
            "_error": "no_parent",
            "parent": parent,
        }
    if state is not None:
        state.started = True
    target_file.write_text(content)
    if state is not None:
        state.committed = True
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime, "is_new": not exists}


def _delete_file_sync(
    target_file: Path,
    *,
    expected: FileImage | None = None,
    state: FileWriteState | None = None,
) -> dict[str, Any]:
    """Bundle delete_file blocking I/O for a single executor offload."""
    _check_file_image(target_file, expected)
    if not target_file.exists():
        return {"_error": "not_found"}
    if not target_file.is_file():
        return {"_error": "not_a_file"}
    stat = target_file.stat()
    if state is not None:
        state.started = True
    target_file.unlink()
    if state is not None:
        state.committed = True
    return {"size": stat.st_size}


def _replace_file_sync(
    target_file: Path,
    content: str,
    *,
    expected: FileImage | None = None,
    state: FileWriteState | None = None,
) -> dict[str, Any]:
    """Bundle whole-file replace I/O for a single executor offload.

    mkdir parent + atomic temp-write + rename + stat, mirroring
    ``_write_file_sync``. Used by edit_yaml_config(action="replace_file").
    """
    _check_file_image(target_file, expected)
    if state is not None:
        state.started = True
    target_file.parent.mkdir(parents=True, exist_ok=True)
    # The temporary belongs only to this invocation; an existing sibling
    # .tmp file or symlink is never opened or removed.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=target_file.parent,
        prefix=f".{target_file.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
        except BaseException:
            handle.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        _check_file_image(target_file, expected)
        os.replace(temporary, target_file)
        if state is not None:
            state.committed = True
    finally:
        temporary.unlink(missing_ok=True)
    stat = target_file.stat()
    return {"size": stat.st_size, "mtime": stat.st_mtime}
