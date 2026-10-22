"""Versioned saved-script files shared by native and remote code mode."""

import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .code_validation import SAVE_NAME_PATTERN

logger = logging.getLogger(__name__)
MAX_SAVED_TOOLS = 256
SAVED_TOOLS_SCHEMA_VERSION = 1


def _validate_saved_tool_entry(
    name: object, info: object, path: Path
) -> dict[str, str] | None:
    """Validate and normalize a single saved-tool entry from the on-disk file.

    Returns the ``{"code": ..., "justification": ...}`` dict, or ``None``
    (logging at WARNING) if the entry is malformed.
    """
    if not (isinstance(name, str) and SAVE_NAME_PATTERN.match(name)):
        logger.warning("Skipping saved tool with invalid name %r in %s", name, path)
        return None
    if not isinstance(info, dict):
        logger.warning("Skipping saved tool %r in %s: entry is not a dict", name, path)
        return None
    code = info.get("code")
    justification = info.get("justification", "")
    if not isinstance(code, str) or not code:
        logger.warning(
            "Skipping saved tool %r in %s: missing or invalid code",
            name,
            path,
        )
        return None
    if not isinstance(justification, str):
        justification = ""
    return {"code": code, "justification": justification}


class SavedToolsPersistence:
    """A failed file load belongs to one registry, never another integration."""

    def __init__(
        self, *, load_failed: bool = False, limit: int = MAX_SAVED_TOOLS
    ) -> None:
        self.load_failed = load_failed
        self.limit = limit

    def _read_raw(self, path: Path) -> str | None:
        """Read the saved-tools file's raw text, if it exists and is readable.

        Returns ``None`` for the legitimate "starting empty" cases: the file
        doesn't exist, or a FileNotFoundError race between ``exists()`` and
        ``read_text()``.

        A genuine I/O error reading an existing file (OSError that isn't
        FileNotFoundError) is logged at ERROR and ALSO sets the instance
        ``self.load_failed`` flag so callers know not to overwrite
        whatever is on disk while the load condition persists. This prevents
        a PermissionError at startup from cascading into "next save wipes
        out the unreadable file" data loss.
        """
        if not path.exists():
            logger.debug("Saved-tools file %s does not exist yet; starting empty", path)
            return None
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Race: file disappeared between exists() and read_text().
            # Treat as legitimate "not yet" rather than an I/O failure.
            return None
        except OSError as exc:
            # PermissionError / IsADirectoryError / etc. The file exists but
            # we can't read it. Block subsequent persistence so we don't
            # overwrite the unreadable original with empty content.
            logger.error(
                "Cannot read saved-tools file %s (%s); persistence will be "
                "suppressed until the load condition clears. Saves and "
                "deletes will still update the in-memory cache for the "
                "current session.",
                path,
                exc,
                exc_info=True,
            )
            self.load_failed = True
            return None

    def _parse_payload(self, raw: str, path: Path) -> dict[str, Any] | None:
        """Parse the saved-tools JSON payload and return its raw ``saved_tools`` mapping.

        Returns ``None`` (logging at WARNING/ERROR as appropriate) for any
        malformed shape: invalid JSON, non-dict top level, unexpected schema
        version, or a non-dict ``saved_tools`` value. Sets the instance
        ``self.load_failed`` flag on an unexpected schema version, for
        the same reason as ``_read_saved_tools_raw``'s OSError branch.
        """
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Saved-tools file %s is not valid JSON (%s); starting empty. "
                "The corrupt file will be overwritten on the next persist.",
                path,
                exc,
            )
            return None

        if not isinstance(data, dict):
            logger.warning(
                "Saved-tools file %s top-level is %s, expected dict; ignoring",
                path,
                type(data).__name__,
            )
            return None

        file_version = data.get("version")
        if file_version != SAVED_TOOLS_SCHEMA_VERSION:
            # Refuse to interpret the file. We don't know whether this is a
            # newer file produced by a future ha-mcp version (which might
            # have shape changes we'd silently mangle) or an older file we
            # don't have a migration for. Setting the failed flag means the
            # current session won't overwrite it on next save.
            logger.error(
                "Saved-tools file %s has schema version %r; this build expects %d. "
                "Refusing to load. Persistence is suppressed for this session "
                "to avoid overwriting an unfamiliar file. Move or delete the "
                "file to recover.",
                path,
                file_version,
                SAVED_TOOLS_SCHEMA_VERSION,
            )
            self.load_failed = True
            return None

        tools_raw = data.get("saved_tools", {})
        if not isinstance(tools_raw, dict):
            logger.warning(
                "Saved-tools file %s 'saved_tools' is %s, expected dict; ignoring",
                path,
                type(tools_raw).__name__,
            )
            return None
        return tools_raw

    def load(self, path_str: str) -> dict[str, dict[str, str]]:
        """Load saved tools from a JSON file, filtering malformed entries.

        Returns an empty dict if the path is unset or the file doesn't exist
        yet (legitimate "starting empty" cases). A corrupt JSON body or an
        unexpected schema version is logged at WARNING and returns empty —
        the file will be overwritten on the next persist.

        A genuine I/O error reading an existing file (OSError that isn't
        FileNotFoundError) is logged at ERROR and ALSO sets the instance
        ``self.load_failed`` flag so callers know not to overwrite
        whatever is on disk while the load condition persists. This prevents
        a PermissionError at startup from cascading into "next save wipes
        out the unreadable file" data loss.
        """
        self.load_failed = False
        if not path_str:
            return {}
        path = Path(path_str)

        raw = self._read_raw(path)
        if raw is None:
            return {}

        tools_raw = self._parse_payload(raw, path)
        if tools_raw is None:
            return {}

        valid: dict[str, dict[str, str]] = {}
        for name, info in tools_raw.items():
            entry = _validate_saved_tool_entry(name, info, path)
            if entry is None:
                continue
            valid[name] = entry
            if len(valid) >= self.limit:
                logger.warning(
                    "Saved-tools file %s contains more than %d tools; truncating",
                    path,
                    self.limit,
                )
                break

        logger.info("Loaded %d saved tool(s) from %s", len(valid), path)
        return valid

    def save(self, path_str: str, tools: dict[str, dict[str, str]]) -> bool:
        """Persist the saved-tools cache to a JSON file atomically.

        Returns ``True`` if persistence succeeded (or was disabled because
        ``path_str`` is empty — that's the configured-out case, not a
        failure). Returns ``False`` only when persistence was attempted and
        the underlying I/O raised. Callers that promised the user durability
        should surface a ``False`` return as a warning in the response.

        Writes to ``<dir>/.<name>.<rand>.tmp`` first and uses ``os.replace``
        to swap it in, so a crash mid-write cannot corrupt the existing
        file. Refuses to write at all when ``self.load_failed`` is
        set — see _load_saved_tools for why we'd rather skip persistence
        than overwrite an unreadable file with empty content.
        """
        if not path_str:
            return True
        if self.load_failed:
            logger.warning(
                "Skipping persist to %s because the prior load failed; "
                "saves and deletes are in-memory only for this session.",
                path_str,
            )
            return False
        path = Path(path_str)
        payload = {
            "version": SAVED_TOOLS_SCHEMA_VERSION,
            "saved_at": datetime.now(UTC).isoformat(),
            "saved_tools": tools,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Write to a temp file in the same directory so os.replace is
            # guaranteed to be atomic (cross-filesystem replace on POSIX is
            # not). delete=False because we'll replace it ourselves.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp_path = Path(tmp.name)
            tmp_path.replace(path)
        except OSError as exc:
            logger.error(
                "Failed to persist saved tools to %s (%s); the in-memory "
                "cache holds the latest change but it will be lost on restart "
                "unless this resolves before the next save",
                path,
                exc,
                exc_info=True,
            )
            return False
        return True
