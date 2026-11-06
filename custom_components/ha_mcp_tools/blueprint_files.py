"""Read an installed blueprint from its confined directory, without a transport."""

from __future__ import annotations

from dataclasses import dataclass, field
from os import stat_result
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from ha_mcp.runtime.blueprint_values import parse_blueprint_body
from ha_mcp.tool_api import ToolFailure

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _BlueprintFile(NamedTuple):
    """One blueprint file read once: its raw ``text`` and its parsed ``body``.

    ``text`` is ``None`` when the file could not be read at all; ``body`` is
    additionally ``None`` when it read but did not parse into a mapping.
    """

    text: str | None
    body: dict[str, Any] | None


_UNREADABLE_BLUEPRINT = _BlueprintFile(None, None)


def _read_blueprint_file(hass: HomeAssistant, domain: str, path: str) -> _BlueprintFile:
    """Resolve + jail + read + parse one blueprint YAML file, reading it once.

    Blueprint files live under ``<config>/blueprints/<domain>/``. The requested
    ``path`` is joined under that root and resolved symlink-safe (mirrors the
    file-tool jail's ``_resolves_within`` — resolve the RAW input, following
    symlinks, THEN check containment, so ``<root>/<symlink>/..`` cannot escape). A
    path escaping the root — via ``..``, an absolute path, or a symlink — yields
    an empty result (rejected, never opened), as does a missing file, a non-file
    target, or a read error. A file that reads but does not parse into a mapping
    keeps its ``text`` and drops its ``body``, so the caller can still round-trip
    the exact bytes it holds.

    Parsed with :class:`_BlueprintLoader`: ``!input`` markers are preserved and
    every other custom tag (``!secret`` / ``!include`` / …) is neutralized to
    ``None``, so no resolved secret plaintext can ever enter the returned body
    (defense in depth — blueprints use ``!input``, not ``!secret``).
    """
    config = getattr(hass, "config", None)
    path_fn = getattr(config, "path", None)
    if not callable(path_fn):
        return _UNREADABLE_BLUEPRINT
    try:
        base = Path(path_fn("blueprints", domain))
        candidate = Path(path) if path.startswith("/") else base / path
        real = candidate.resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return _UNREADABLE_BLUEPRINT
    if not (real == base_real or real.is_relative_to(base_real)):
        return _UNREADABLE_BLUEPRINT
    try:
        text = real.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return _UNREADABLE_BLUEPRINT
    return _BlueprintFile(text, parse_blueprint_body(text))


@dataclass(frozen=True, slots=True)
class InstalledBlueprint:
    """A detached installed-file observation, distinct from Core's parsed cache."""

    path: Path = field(repr=False)
    text: str | None = field(repr=False)
    fingerprint: tuple[int, int, int] | None


def read_installed_blueprint(folder: Path, path: str) -> InstalledBlueprint:
    """Read a before-image off the loop; only genuine absence means no image."""
    base = folder.resolve()
    candidate = (folder / path).resolve()
    if not candidate.is_relative_to(base) or candidate == base:
        raise ToolFailure(
            "VALIDATION_INVALID_PARAMETER",
            "Blueprint path escapes its domain directory",
            status="not_started",
        )
    try:
        before = candidate.stat()
    except FileNotFoundError:
        return InstalledBlueprint(candidate, None, None)
    # No text newline conversion: a snapshot represents the installed text,
    # including CRLF, rather than a new serialization of Core's parsed cache.
    text = candidate.read_bytes().decode("utf-8")
    after = candidate.stat()

    def stamp(value: stat_result) -> tuple[int, int, int]:
        return value.st_ino, value.st_size, value.st_mtime_ns

    if stamp(before) != stamp(after):
        raise ToolFailure(
            "CONFIG_CONFLICT",
            "Blueprint changed while being read",
            status="not_started",
        )
    return InstalledBlueprint(candidate, text, stamp(after))
