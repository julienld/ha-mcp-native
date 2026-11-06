"""Canonical filesystem allowlists and real-path confinement for the component."""

from __future__ import annotations

import fnmatch
import os
import posixpath
from pathlib import Path, PurePosixPath

from .const import (
    ALLOWED_READ_DIRS,
    ALLOWED_VOLUME_ROOTS,
    DENY_PATH_SEGMENTS,
    DENY_READ_BASENAMES,
)

ALLOWED_READ_FILES = [
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "secrets.yaml",
    "home-assistant.log",
    # faulthandler's crash dump (HA Core's ``__main__`` enables it on this file).
    # Written only on a native fatal signal, so it is the one place that
    # traceback exists: journald / error_log never see it (issue #2373).
    "home-assistant.log.fault",
]


def _is_within_config_dir(config_dir: Path, normalized: str) -> bool:
    """Resolve ``config_dir / normalized`` and confirm it stays within
    ``config_dir`` (symlink-aware).

    Uses ``Path.is_relative_to`` rather than a string prefix so a sibling like
    ``<config>-evil`` can't masquerade as being inside the config dir. Fails
    closed on any resolution error.
    """
    try:
        resolved = (config_dir / normalized).resolve()
        config_resolved = config_dir.resolve()
    except (OSError, ValueError):
        return False
    return resolved == config_resolved or resolved.is_relative_to(config_resolved)


def _violates_deny_floor(config_dir: Path, normalized: str) -> bool:
    """True if ``normalized`` (a ``normpath``'d, config-relative path) hits the
    non-overridable deny floor (issue #1567).

    Checked BEFORE any allow decision on every read/write/list/delete, so a
    user-configured extra directory — whether stored or supplied in-flight —
    can never reach these locations. See ``const.DENY_PATH_SEGMENTS`` /
    ``const.DENY_READ_BASENAMES`` for the rationale.
    """
    # All comparisons are case-insensitive: on a case-insensitive filesystem
    # (macOS APFS, some Docker bind mounts / SMB) ".STORAGE" opens the real
    # ".storage", so an exact-case match would let a mixed-case entry slip
    # through. The deny sets are already lowercase, so lowering the input is
    # enough. This only ever denies MORE — no legitimate path is a case-variant
    # of ".storage"/"secrets.yaml".
    parts = [p for p in normalized.split(os.sep) if p]
    # A .storage segment anywhere in the relative path (".storage",
    # ".storage/auth", "x/.storage/y").
    if any(p.lower() in DENY_PATH_SEGMENTS for p in parts):
        return True
    # Symlink defence: resolve and reject if the real target passes THROUGH a
    # denied segment (e.g. an in-config symlink pointing at .storage). Scan ONLY
    # the portion of the resolved path that is *under the config dir* — not the
    # full absolute path — so the floor doesn't blanket-ban every access when the
    # config dir itself happens to live below a ".storage" component (e.g.
    # ``/var/.storage/config``). This mirrors the pre-PR allowlist model, which
    # likewise only ever reasons about the config-relative path. ``relative_to``
    # raising ValueError means the resolved target escaped the config dir (e.g. a
    # symlink out) — fail closed. Fail closed on any resolution error too.
    try:
        resolved = (config_dir / normalized).resolve()
        rel_parts = resolved.relative_to(config_dir.resolve()).parts
    except (OSError, ValueError):
        return True
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in rel_parts):
        return True
    # Denied basenames — matched on BOTH the requested path AND the resolved
    # target, so a renamed symlink (``www/notes.txt`` → ``secrets.yaml``) can't
    # dodge it and then escape masking (the read handler masks only the literal
    # ``secrets.yaml``). The canonical config-root ``secrets.yaml`` is the one
    # exception, matched EXACTLY (not lowercased) because that is the only path
    # the handler masks — a mixed-case ``SECRETS.YAML`` at the root is NOT
    # masked, so it must be denied. The exception also requires the resolved
    # target to still be a ``secrets.yaml``, so the one permitted path cannot
    # be pointed at another denied file. No other denied basename has an
    # exception: ``approval_pin.json`` is out of reach wherever it appears.
    if normalized == "secrets.yaml" and resolved.name.lower() == "secrets.yaml":
        return False
    return (
        os.path.basename(normalized).lower() in DENY_READ_BASENAMES
        or resolved.name.lower() in DENY_READ_BASENAMES
    )


def _volume_root_for(abs_path: str) -> str | None:
    """Return the HAOS sibling-volume root (``const.ALLOWED_VOLUME_ROOTS``) that
    ``abs_path`` falls within, or ``None`` (issue #1586).

    Boundary-aware on a path-separator: ``/share`` and ``/share/x`` match
    ``/share``; ``/shared`` and ``/backups`` do NOT (a string-prefix match would
    wrongly admit them). ``abs_path`` must already be ``posixpath.normpath``'d —
    the volume roots are inherently POSIX and the component only runs on HA Core
    (Linux), so we never use the host ``os.sep`` here.
    """
    for root in ALLOWED_VOLUME_ROOTS:
        if abs_path == root or abs_path.startswith(root + "/"):
            return root
    return None


def _resolves_within(base: Path, raw_path: str) -> bool:
    """Resolve ``raw_path`` exactly as ``open(2)`` will — following symlinks,
    THEN applying ``..`` against the real target — and confirm it stays within
    ``base`` (symlink-safe containment; issue #1586 review).

    The lexical allow checks run on ``os.path.normpath(raw_path)``, which
    collapses ``..`` *textually* and so erases an intermediate symlink component
    (``<allowed>/<symlink>/..``) that the kernel would actually traverse at open
    time — the divergence that let a configured volume reach arbitrary files.
    Resolving the RAW input (not the normpath-collapsed form) closes it, because
    the handlers open ``config_dir / raw_path`` and the OS resolves it the same
    way ``Path.resolve()`` does. Fails closed on any resolution error.
    """
    try:
        candidate = Path(raw_path) if raw_path.startswith("/") else base / raw_path
        real = candidate.resolve()
        base_real = base.resolve()
    except (OSError, ValueError):
        return False
    return real == base_real or real.is_relative_to(base_real)


def _violates_volume_deny_floor(abs_path: str) -> bool:
    """True if an absolute HAOS volume path hits the non-overridable deny floor
    (issue #1586).

    The same floor as the config dir — a ``.storage`` segment anywhere, or a
    denied basename (``const.DENY_READ_BASENAMES``) — applied to both the
    requested path and its symlink-resolved target, case-insensitively. Unlike
    the config dir there is NO canonical ``secrets.yaml`` exception: volume
    reads are never masked, so a ``secrets.yaml`` on any volume is always
    denied. Fails closed on any resolution error.
    """
    req = PurePosixPath(abs_path)
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in req.parts):
        return True
    if req.name.lower() in DENY_READ_BASENAMES:
        return True
    try:
        resolved = Path(abs_path).resolve()
    except (OSError, ValueError):
        return True
    if any(seg.lower() in DENY_PATH_SEGMENTS for seg in resolved.parts):
        return True
    return resolved.name.lower() in DENY_READ_BASENAMES


def _normalize_volume_dir(entry: str) -> str | None:
    """Validate one absolute HAOS sibling-volume directory (issue #1586).

    Returns the cleaned absolute path, or ``None`` if it is not at/under a known
    volume root or hits the deny floor. POSIX-normalized regardless of host OS
    (the roots are inherently POSIX and the component only runs on HA Core).
    Existence is NOT required (mirrors config-relative extra dirs); an unmounted
    volume just yields ``not found`` at use time.
    """
    normalized = posixpath.normpath(entry)
    root = _volume_root_for(normalized)
    if root is None:
        return None
    if _violates_volume_deny_floor(normalized):
        return None
    return normalized


def _is_volume_path_allowed(abs_path: str, extra_dirs: list[str] | None) -> bool:
    """Enforce a read/write/list/delete against an absolute HAOS volume path
    (issue #1586).

    Allowed iff the path is at/under a configured extra dir that is itself a
    volume path, clears the deny floor, and — after FULL symlink + ``..``
    resolution of the RAW path (matching ``open(2)``) — stays within its volume
    root. Resolving the raw path rather than the lexically ``normpath``'d form
    closes the ``<volume>/<symlink>/..`` escape (issue #1586 review). Read and
    write share this gate — a configured volume grants read+write (#1567).
    """
    normalized = posixpath.normpath(abs_path)
    root = _volume_root_for(normalized)
    if root is None:
        return False
    if _violates_volume_deny_floor(normalized):
        return False
    # POSIX-explicit allowlist match (not _matches_extra_dir, which joins on
    # os.sep): a volume path is inherently "/"-separated, so match on a "/"
    # boundary so a configured "/share" grants "/share" and "/share/..." but
    # never "/shared". Config-relative entries in the mixed list never match an
    # absolute path here.
    if not extra_dirs or not any(
        normalized == d or normalized.startswith(d + "/") for d in extra_dirs
    ):
        return False
    return _resolves_within(Path(root), abs_path)


def _normalize_extra_dir(entry: str, config_dir: Path) -> str | None:
    """Validate and normalize one user-supplied extra directory (issues #1567,
    #1586).

    Returns the cleaned directory, or ``None`` if the entry must be rejected.
    Two accepted shapes:

    * A config-relative directory (issue #1567) — rejected if empty, the config
      root, uses ``..`` traversal, resolves outside the config dir, or hits the
      deny floor.
    * An absolute HAOS sibling-volume path (issue #1586) — kept absolute and
      validated against its volume root (``const.ALLOWED_VOLUME_ROOTS``) instead
      of the config dir. Any other absolute path is still rejected.

    Rejected entries are dropped (mirrors the filter-to-known-good discipline
    used for ``disabled_packages_keys``).
    """
    if not isinstance(entry, str):
        return None
    stripped = entry.strip()
    if not stripped:
        return None
    # Absolute HAOS sibling-volume path (issue #1586) — validated against its
    # volume root, not the config dir. Detected by a POSIX-absolute leading "/"
    # (not os.path.isabs, which is False for "/share" on a non-POSIX host) so the
    # logic is identical on every OS the tests run on. Any non-volume absolute
    # path is rejected inside _normalize_volume_dir.
    if stripped.startswith("/"):
        return _normalize_volume_dir(stripped)
    cleaned = stripped.strip("/")
    if not cleaned:
        return None
    normalized = os.path.normpath(cleaned)
    if (
        normalized in (".", "")
        or normalized.startswith("..")
        or normalized.startswith("/")
    ):
        return None
    if not _is_within_config_dir(config_dir, normalized):
        return None
    if _violates_deny_floor(config_dir, normalized):
        return None
    return normalized


def normalize_allowed_paths(
    config_dir: Path, raw_paths: object
) -> tuple[list[str], list[object]]:
    """Normalize configured paths in an executor: the guard resolves symlinks."""
    kept: list[str] = []
    rejected: list[object] = []
    for entry in raw_paths if isinstance(raw_paths, list) else []:
        normalized = (
            _normalize_extra_dir(entry, config_dir) if isinstance(entry, str) else None
        )
        if normalized is None:
            rejected.append(entry)
        elif normalized not in kept:
            kept.append(normalized)
    return kept, rejected


def _matches_extra_dir(normalized: str, extra_dirs: list[str] | None) -> bool:
    """True if ``normalized`` IS one of the user-configured extra directories
    or a path under one (issue #1567).

    Prefix match on a path-separator boundary so a multi-segment entry like
    ``foo/bar`` grants ``foo/bar`` and ``foo/bar/...`` exactly as configured
    (the built-in allowlists are single-segment and matched on ``parts[0]``,
    but extra dirs may be nested), while ``foo`` never matches ``foobar``.
    """
    if not extra_dirs:
        return False
    return any(normalized == d or normalized.startswith(d + os.sep) for d in extra_dirs)


def _path_in_package_dir(normalized: str, package_dirs: set[str] | None) -> bool:
    """True if ``normalized`` is a ``*.yaml`` file at any depth under one of the
    configured package folders.

    Literal folder match (not ``fnmatch``), so a folder name containing glob
    metacharacters — valid on HA's Linux filesystem — is matched as the literal
    include root rather than a wildcard (#1854 review).
    """
    normalized = normalized.replace(os.sep, "/")
    if not normalized.endswith(".yaml"):
        return False
    return any(
        normalized.startswith(folder + "/") for folder in (package_dirs or {"packages"})
    )


def _dir_in_package_dir(normalized: str, package_dirs: set[str] | None) -> bool:
    """True if ``normalized`` IS a configured package folder, or a folder under one.

    The file-level twin is ``_path_in_package_dir``; this one matches the
    FOLDER itself (and nested folders) so ``list_files`` can enumerate a
    packages directory the way ``read_file`` can already read the ``*.yaml``
    inside it (issue #1854).

    Unlike ``_path_in_package_dir``, this does NOT default to ``{"packages"}``
    when ``package_dirs`` is None: ``_is_path_allowed_for_dir`` is shared with
    write_file and delete_file, which must never gain package access
    (``edit_yaml_config`` is the only write path to config YAML). Only the
    read-side lister passes ``package_dirs``.
    """
    normalized = normalized.replace(os.sep, "/")
    if not package_dirs:
        return False
    return any(
        normalized == folder or normalized.startswith(folder + "/")
        for folder in package_dirs
    )


def _is_path_allowed_for_dir(
    config_dir: Path,
    rel_path: str,
    allowed_dirs: list[str],
    extra_dirs: list[str] | None = None,
    package_dirs: set[str] | None = None,
) -> bool:
    """Check if a path is within allowed directories.

    ``extra_dirs`` are the user-configured custom directories (issue #1567),
    granted in addition to ``allowed_dirs``. The non-overridable deny floor is
    checked first, so a custom directory can never grant access to ``.storage``
    or other floored paths.

    ``package_dirs`` (issue #1854) widens ONLY the allow decision — every
    containment, deny-floor and symlink check below still runs — and is passed
    by the read-side lister alone; write and delete leave it None so a
    packages folder stays non-writable through this helper.
    """
    # Absolute HAOS sibling-volume path (issue #1586) — enforced against its
    # volume root rather than the config dir. Detected by a POSIX-absolute
    # leading "/" and passed RAW (not normpath'd) so symlink resolution matches
    # what the handler's open() does. Any non-volume absolute path is rejected
    # inside the helper.
    if rel_path.startswith("/"):
        return _is_volume_path_allowed(rel_path, extra_dirs)

    # Normalize the path
    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # NON-OVERRIDABLE deny floor — before any allow decision (issue #1567).
    if _violates_deny_floor(config_dir, normalized):
        return False

    # Built-in allowlist matches on the first segment; user-configured extra
    # dirs match on a path-boundary prefix (so nested entries work). A
    # configured packages folder matches literally (it may itself be nested,
    # e.g. "conf/packages", so a first-segment test would miss it).
    parts = normalized.split(os.sep)
    builtin_ok = bool(parts) and parts[0] in allowed_dirs
    if (
        not builtin_ok
        and not _dir_in_package_dir(normalized, package_dirs)
        and not _matches_extra_dir(normalized, extra_dirs)
    ):
        return False

    # Symlink-safe containment on the REAL path the handler will open (issue
    # #1586 review): resolve the RAW rel_path — open(2) follows symlinks then
    # applies "..", which the lexical normpath above cannot model — and confirm
    # it stays under the config dir. Closes the `<allowed>/<symlink>/../escape`
    # traversal the lexical checks miss.
    if not _resolves_within(config_dir, rel_path):
        return False

    # Resolve full path and verify it's still under config_dir
    return _is_within_config_dir(config_dir, normalized)


def _is_path_allowed_for_read(
    config_dir: Path,
    rel_path: str,
    extra_dirs: list[str] | None = None,
    package_dirs: set[str] | None = None,
) -> bool:
    """Check if a path is allowed for reading.

    Allowed:
    - Files directly in config dir: configuration.yaml, automations.yaml, etc.
    - Files in allowed directories: www/, themes/, custom_templates/,
      dashboards/, blueprints/ (blueprints is read-only — issue #1965)
    - Files matching patterns: <packages-folder>/*.yaml, custom_components/**/*.py
    - User-configured extra directories (``extra_dirs``), granted read+write
      (issue #1567)

    ``package_dirs`` is the set of configured packages folder name(s) (issue
    #1854); it defaults to ``{"packages"}`` when not supplied so callers that
    don't resolve the live config keep the historical behaviour.

    The non-overridable deny floor is checked first, so a custom directory can
    never reach ``.storage`` or an unmasked ``secrets.yaml``.
    """
    # Absolute HAOS sibling-volume path (issue #1586) — enforced against its
    # volume root rather than the config dir. Detected by a POSIX-absolute
    # leading "/" and passed RAW so symlink resolution matches the handler's
    # open(). A configured volume grants read too, so reads route through the
    # same gate as dir/write.
    if rel_path.startswith("/"):
        return _is_volume_path_allowed(rel_path, extra_dirs)

    normalized = os.path.normpath(rel_path)

    # Check for path traversal attempts
    if normalized.startswith("..") or normalized.startswith("/"):
        return False

    # Resolve full path and verify it's still under config_dir
    if not _is_within_config_dir(config_dir, normalized):
        return False

    # NON-OVERRIDABLE deny floor — before any allow decision (issue #1567).
    if _violates_deny_floor(config_dir, normalized):
        return False

    # Symlink-safe containment on the REAL path the handler will open (issue
    # #1586 review): resolve the RAW rel_path so a `<allowed>/<symlink>/..` that
    # lexically stays inside but physically escapes is rejected.
    if not _resolves_within(config_dir, rel_path):
        return False

    # Check if it's one of the explicitly allowed files in config root
    if normalized in ALLOWED_READ_FILES:
        return True

    # Check if path starts with an allowed directory
    parts = normalized.split(os.sep)
    if parts and parts[0] in ALLOWED_READ_DIRS:
        return True

    # Check for <packages-folder>/*.yaml at any depth. The configured folder(s)
    # default to {"packages"} (issue #1854); literal match so a folder name with
    # glob metacharacters is treated as the include root, not a wildcard.
    if _path_in_package_dir(normalized, package_dirs):
        return True

    # Check for custom_components/**/*.py pattern
    if fnmatch.fnmatch(normalized, "custom_components/**/*.py"):
        return True

    # User-configured extra directories (read+write) — issue #1567. Prefix
    # match so nested entries (e.g. "foo/bar") grant paths under them.
    return _matches_extra_dir(normalized, extra_dirs)


def _validate_dashboard_filename(filename: str) -> str | None:
    """Validate a YAML-mode dashboard `filename:` value.

    Returns None if valid, otherwise a human-readable error string.

    Rules:
    - Must be a non-empty string.
    - Must end in '.yaml'.
    - Must resolve to a path under 'dashboards/' (no traversal escape).
    - No absolute paths, no '..' segments.
    """
    if not filename or not isinstance(filename, str):
        return "filename must be a non-empty string"
    if filename.startswith("/"):
        return "filename must not be an absolute path"
    if not filename.endswith(".yaml"):
        return "filename must end with .yaml"

    normalized = os.path.normpath(filename)
    if normalized.startswith("..") or normalized.startswith("/"):
        return "filename must not escape the config directory"

    parts = normalized.split(os.sep)
    if not parts or parts[0] != "dashboards":
        return "filename must be under dashboards/"
    # Reject any '..' segment (defence-in-depth after normpath collapse)
    if ".." in parts:
        return "filename must not contain path traversal segments"
    return None
