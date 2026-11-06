"""Snapshot file format, retention and safe paths shared by both HA backends."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
import time
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NotRequired, TypedDict

import yaml  # type: ignore[import-untyped]

from ha_mcp.errors import HomeAssistantError

from .helper_schema import SIMPLE_HELPER_TYPES

logger = logging.getLogger("ha_mcp.backup_manager")
_HELPER_LIST_TYPES = SIMPLE_HELPER_TYPES
_TEXT_KIND: Literal["text"] = "text"
_DASHBOARD_KIND = "dashboard_settings"

SCHEMA_VERSION = 1

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")


class SnapshotInUseError(ValueError):
    """A snapshot is pinned by capture or restore and cannot be deleted yet."""


class BulkDeleteResult(TypedDict):
    """Keep filename lists stable while explaining safe, actionable refusals."""

    deleted: list[str]
    failed: list[str]
    failure_reasons: NotRequired[dict[str, Literal["snapshot_in_use"]]]


def _is_flow_helper_domain(domain: str) -> bool:
    """A ``helper_<type>`` snapshot domain of a config-entry (flow) helper.

    Any type that is neither a storage helper nor a config subentry counts; the
    type itself is not checked here. The tools check it against Core's helper
    flow list (``helper_flows.require_helper_type``; ha_config_set_helper only
    after its auto-backup capture, which checks an uncovered type itself), as
    does ``resolve_config_entry_backup_domain`` for an arbitrary config entry.
    """
    helper_type = domain[7:] if domain.startswith("helper_") else ""
    return helper_type not in ("", *_HELPER_LIST_TYPES, "config_subentry")


_FILENAME_RE = re.compile(
    r"^(?P<domain>[A-Za-z0-9_]+)\."
    r"(?P<entity_id>[A-Za-z0-9._-]+)\."
    r"(?P<ts>\d{8}_\d{6})(?:_(?P<seq>\d{2}))?\.yaml$"
)

_MAX_SAME_SECOND = 100

_TEXT_DOMAINS = frozenset(
    {
        "file",
        "yaml",
        "yaml_file",
        "blueprint_automation",
        "blueprint_script",
    }
)


def _entity_id_aliases(entity_id: str) -> list[str]:
    """Every filename stem a snapshot of ``entity_id`` may carry.

    Sanitised ids gained a digest suffix, so snapshots written before that
    sit under the bare sanitised name. Rotation and entity filtering match
    both, otherwise those older files would never be counted again: they
    would never be pruned, and would drop out of an entity-filtered listing.
    """
    current = _safe_entity_id(entity_id)
    legacy = _SAFE_ID_RE.sub("_", entity_id).lstrip(".") or "_"
    return [current] if legacy == current else [current, legacy]


def _safe_entity_id(entity_id: str) -> str:
    """Sanitize an entity id for use in a filename.

    Replaces any character outside ``[A-Za-z0-9._-]`` with ``_``. Path
    separators get caught by this (the regex excludes both ``/`` and ``\\``).
    Strips leading dots to prevent dotfile collisions.

    Sanitizing is lossy, so ids that needed it get a short digest of the
    ORIGINAL appended. Blueprint domains (#2329) key on paths, where
    ``user/motion.yaml`` and ``user_motion.yaml`` both clean to
    ``user_motion.yaml`` -- without the digest they share one snapshot
    namespace, so captures in the same second overwrite each other and
    rotation counts both histories as one, able to delete the only restore
    point for a blueprint that was never written. Entity ids are already
    within the safe set, so their filenames are unchanged and existing
    snapshots stay discoverable.
    """
    if not entity_id:
        return "_"
    cleaned = _SAFE_ID_RE.sub("_", entity_id).lstrip(".")
    if not cleaned:
        return "_"
    if cleaned == entity_id:
        return cleaned
    digest = hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


def _snapshot_order(path: Path) -> tuple[str, str, str]:
    """Sort key placing snapshots in capture order, oldest first.

    Filenames cannot be compared directly: an id the sanitiser rewrote has
    two stems, and the digest stem sorts before the bare one on every name
    (``-`` < ``.``) regardless of timestamp, so by name every new capture
    would age below every pre-digest file. The parsed timestamp and the
    same-second sequence order them; the name only breaks exact ties.
    """
    m = _FILENAME_RE.match(path.name)
    if m is None:
        return ("", "", path.name)
    return (m.group("ts"), m.group("seq") or "", path.name)


def _id_matches(payload_id: str | None, wanted: str) -> bool:
    """Whether a snapshot whose payload names ``payload_id`` is ``wanted``.

    Two spellings select an entity: the id itself, and the digest stem
    ``_safe_entity_id`` derives from it — the stem a listing used to show and
    a filename still carries. Both name exactly one entity. The bare
    pre-digest stem is deliberately not accepted: it is the current stem of a
    differently-named entity, which is the ambiguity this check exists for.
    """
    if payload_id is None:
        return False
    return payload_id == wanted or _safe_entity_id(payload_id) == wanted


def _now_ts() -> str:
    return datetime.now(UTC).strftime("%Y%m%d_%H%M%S")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _require_list(value: Any, endpoint: str) -> list[Any]:
    """Return ``value`` if it's a list, else raise.

    The WS registry-list fetchers distinguish two cases that used
    to both collapse to ``None`` (which the diff/capture callers read as
    "entity missing"): a genuine miss (entity not in the list) stays
    ``None``, but an unexpected non-list envelope — a degraded response
    from an auth-scope change or API drift — raises instead. The raise
    funnels through the diff tool's ``exception_to_structured_error`` and
    the capture pipeline's ``_CAPTURE_TRANSIENT_ERRORS`` warning, so a
    broken fetch is never reported as a confident ``entity_missing``.
    """
    if not isinstance(value, list):
        raise HomeAssistantError(
            f"Expected a list from {endpoint!r}, got {type(value).__name__}"
        )
    return value


def _require_dict(value: Any, endpoint: str) -> dict[str, Any]:
    """Return ``value`` if it's a dict, else raise.

    Dict-shaped counterpart to :func:`_require_list` for the
    ``execute_script``-backed fetchers (calendar / todo). Their service
    response is a dict envelope; a non-dict body is a degraded/malformed
    200 (auth-scope change, API drift), not a genuine miss. Raising
    funnels it through the diff tool's ``exception_to_structured_error``
    and the capture pipeline's ``_CAPTURE_TRANSIENT_ERRORS`` warning,
    instead of collapsing to ``None`` — which callers read as
    ``entity_missing``. The genuine-miss signal stays the nested ``uid``
    lookup returning ``None``.
    """
    if not isinstance(value, dict):
        raise HomeAssistantError(
            f"Expected a dict from {endpoint!r}, got {type(value).__name__}"
        )
    return value


def _flow_type(domain: str) -> str:
    return domain.removeprefix("helper_")


def _flow_label(helper_type: str) -> str:
    return helper_type.replace("_", " ").capitalize()


def _flow_entry_id(entity_id: str, config: Any, helper_type: str) -> str:
    """Resolve a captured alias to the stable identity used for diff and restore."""
    label = _flow_label(helper_type)
    if not isinstance(entity_id, str) or not entity_id:
        raise HomeAssistantError(f"{label} helper snapshot has no target identity")
    snapshot = _require_dict(config, f"{helper_type} helper snapshot")
    entry_id = snapshot.get("entry_id")
    if not isinstance(entry_id, str) or not entry_id:
        raise HomeAssistantError(
            f"{label} helper snapshot has no config-entry identity"
        )
    if entity_id != entry_id and "." not in entity_id:
        raise HomeAssistantError(
            f"{label} helper snapshot target does not match its entry"
        )
    return entry_id


class SnapshotFilesMixin:
    _entry_identity_cache: dict[str, tuple[tuple[int, int, int, int, int], str | None]]
    _entry_identity_lock: threading.Lock
    _snapshot_pin_lock: threading.Lock
    _protected_snapshot_names: Counter[str]
    _init_dir_error: str | None
    _directory_checked: bool

    @property
    def _dir(self) -> Path:
        raise NotImplementedError

    @property
    def retain_per_entity(self) -> int:
        raise NotImplementedError

    def _snapshot_now_ts(self) -> str:
        return _now_ts()

    def _snapshot_now_iso(self) -> str:
        return _now_iso()

    def _write_snapshot(
        self, domain: str, entity_id: str, config: Any, tool_name: str | None
    ) -> Path:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            self._init_dir_error = f"{type(err).__name__}: {err}"
            self._directory_checked = True
            raise
        safe = _safe_entity_id(entity_id)
        target = self._unclaimed_target(domain, safe, self._snapshot_now_ts())
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "domain": domain,
            "entity_id": entity_id,
            "captured": self._snapshot_now_iso(),
            "tool": tool_name,
            "config": config,
        }
        if domain in _TEXT_DOMAINS:
            payload["kind"] = _TEXT_KIND
        elif domain == "dashboard":
            payload["kind"] = _DASHBOARD_KIND
        body = yaml.safe_dump(payload, default_flow_style=False, sort_keys=False)
        # mkstemp creates a unique 0600 inode before any configuration is written.
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        try:
            try:
                stream = os.fdopen(fd, "w")
            except BaseException:
                os.close(fd)
                raise
            with stream:
                stream.write("# ha_mcp_backup\n" + body)
            os.replace(tmp_name, str(target))
        finally:
            with suppress(OSError):
                Path(tmp_name).unlink()
        self._init_dir_error = None
        self._directory_checked = True
        logger.info("Auto-backup: snapshot written")
        return target

    def _unclaimed_target(self, domain: str, safe: str, ts: str) -> Path:
        """First filename for ``ts`` that no snapshot of this entity holds yet.

        The bare name is taken when it is free; each later capture inside the
        same second appends ``_01``, ``_02``, ... so the atomic replace in
        ``_write_snapshot`` can never discard an earlier pre-write state.
        ``maybe_snapshot`` serialises captures per key, so the ``exists()``
        probe is not racing another capture of the same entity.
        """
        target = self._dir / f"{domain}.{safe}.{ts}.yaml"
        for seq in range(1, _MAX_SAME_SECOND):
            if not target.exists():
                return target
            target = self._dir / f"{domain}.{safe}.{ts}_{seq:02d}.yaml"
        if not target.exists():
            return target
        raise OSError(
            f"{_MAX_SAME_SECOND} snapshots of {domain}:{safe} inside one second"
        )

    def _payload_entity_id(self, path: Path) -> str | None:
        """The id the snapshot at ``path`` was taken for, from its payload.

        Filename stems are ambiguous in both directions: the legacy stem for
        ``user/motion.yaml`` is ``user_motion.yaml``, which is also the
        CURRENT stem of a differently-named blueprint. The payload keeps the
        original id, so rotation and the entity filter behind ``list_snapshots``
        / ``delete_bulk`` ask it rather than trusting the name. A file that
        cannot be read answers ``None`` and is treated as not ours: skipping it
        wastes a rotation slot or hides it from one entity's listing, deleting
        it could destroy another entity's only restore point.

        Only the header is read. ``_write_snapshot`` emits ``entity_id`` before
        ``config`` (which is the whole automation, dashboard or file), so the
        read stops at the ``config:`` line and never parses the body: every
        listing row opens its file, and an unfiltered listing of hundreds of
        whole-file snapshots has to stay a header read per row. Flow-helper
        snapshots are the exception: template snapshots from before #2632
        used entity aliases as headers, so the stable identity of every
        flow-helper snapshot is read from config.entry_id.
        """
        domain = path.name.split(".", 1)[0]
        if _is_flow_helper_domain(domain):
            return self._entry_snapshot_identity(path, _flow_type(domain))
        try:
            with path.open(encoding="utf-8") as handle:
                header: list[str] = []
                for line in handle:
                    if line.startswith("config:"):
                        break
                    header.append(line)
                loaded = yaml.safe_load("".join(header))
        except (OSError, yaml.YAMLError) as err:
            logger.warning(
                "Auto-backup: cannot identify %s, leaving it in place: %s",
                path.name,
                err,
            )
            return None
        if not isinstance(loaded, dict):
            return None
        found = loaded.get("entity_id")
        return found if isinstance(found, str) else None

    def _entry_snapshot_identity(self, path: Path, helper_type: str) -> str | None:
        """Validate new/changed flow-helper YAML once, including legacy alias headers.

        Rotation still enumerates and stats the history to discover imported or
        changed files, but it does not repeatedly parse unrelated YAML bodies.
        The cache is shared with listing/deletion and guarded across IO threads.
        """
        try:
            before = path.stat()
            signature = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            with self._entry_identity_lock:
                cached = self._entry_identity_cache.get(path.name)
                if cached is not None and cached[0] == signature:
                    return cached[1]
            identity = None
            try:
                data = self.read_snapshot(path.name)
                entity_id = data.get("entity_id")
                if isinstance(entity_id, str):
                    identity = _flow_entry_id(
                        entity_id, data.get("config"), helper_type
                    )
            except (ValueError, HomeAssistantError) as err:
                # A stable malformed file is cached as unowned, never deleted.
                # Parser messages may contain option values; retain only type.
                logger.warning(
                    "Auto-backup: cannot identify %s, leaving it in place: %s",
                    path.name,
                    type(err).__name__,
                )
            after = path.stat()
            if signature != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                self._forget_entry_identity(path.name)
                return None
        except OSError as err:
            logger.warning(
                "Auto-backup: cannot identify %s, leaving it in place: %s",
                path.name,
                type(err).__name__,
            )
            self._forget_entry_identity(path.name)
            return None
        with self._entry_identity_lock:
            self._entry_identity_cache[path.name] = (signature, identity)
        return identity

    def _forget_entry_identity(self, name: str) -> None:
        with self._entry_identity_lock:
            self._entry_identity_cache.pop(name, None)

    def _snapshot_is_for(self, path: Path, entity_id: str) -> bool:
        """Whether ``path`` holds a snapshot of ``entity_id`` (see ``_id_matches``)."""
        return _id_matches(self._payload_entity_id(path), entity_id)

    def _rotate(
        self, domain: str, entity_id: str, protected: frozenset[str] = frozenset()
    ) -> None:
        candidates: set[Path] = set()
        if _is_flow_helper_domain(domain):
            # Template snapshots from before #2632 used the entity alias as
            # their filename/header.
            candidates.update(self._dir.glob(f"{domain}.*.yaml"))
            names = {path.name for path in candidates}
            with self._entry_identity_lock:
                for name in self._entry_identity_cache.keys() - names:
                    del self._entry_identity_cache[name]
        else:
            for safe in _entity_id_aliases(entity_id):
                candidates.update(self._dir.glob(f"{domain}.{safe}.*.yaml"))
        files = sorted(
            (p for p in candidates if self._snapshot_is_for(p, entity_id)),
            key=_snapshot_order,
        )
        excess = len(files) - self.retain_per_entity
        for old in files:
            if excess <= 0:
                break
            try:
                # A restore can pin a file after this rotation was dispatched.
                # Check and unlink atomically against those new pins as well as
                # the frozen pins retained after a cancelled caller unwinds.
                with self._snapshot_pin_lock:
                    if (
                        old.name in protected
                        or old.name in self._protected_snapshot_names
                    ):
                        continue
                    old.unlink()
                    self._forget_entry_identity(old.name)
                    excess -= 1
            except OSError as err:
                logger.warning("Auto-backup: failed to rotate %s: %s", old.name, err)

    def list_snapshots(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if not self._dir.exists():
            return []
        # ``entity_id`` comparison: filenames hold the sanitized form (see
        # ``_safe_entity_id`` — any char outside ``[A-Za-z0-9._-]`` becomes
        # ``_``), so composite IDs like ``area:foo`` / ``automation:UUID``
        # would otherwise never match a filter passed in original form.
        # Sanitize the filter once up front so the per-file comparison is
        # symmetric: filter and ``meta["entity_id"]`` both come from the
        # same sanitization function. The stem only narrows the candidates:
        # sanitising is lossy in both directions (``user/motion.yaml``'s
        # pre-digest stem IS ``user_motion.yaml``'s current stem), so each
        # stem match is confirmed against the id the payload stores before it
        # counts as this entity's — ``delete_bulk`` unlinks whatever is
        # returned here, and the caller named one entity.
        safe_filter = set(_entity_id_aliases(entity_id)) if entity_id else None
        out: list[dict[str, Any]] = []
        # Newest first by capture time — see ``_snapshot_order`` for why the
        # filename itself is not the key.
        for path in sorted(self._dir.glob("*.yaml"), key=_snapshot_order, reverse=True):
            row = self._listing_row(
                path, domain=domain, safe_filter=safe_filter, entity_id=entity_id
            )
            if row is None:
                continue
            out.append(row)
            if limit and len(out) >= limit:
                break
        return out

    def _listing_row(
        self,
        path: Path,
        *,
        domain: str | None,
        safe_filter: set[str] | None,
        entity_id: str | None,
    ) -> dict[str, Any] | None:
        """One ``list_snapshots`` entry for ``path``, or ``None`` if filtered out."""
        meta = self._parse_filename(path.name)
        if meta is None:
            return None
        if domain and meta["domain"] != domain:
            return None
        if (
            safe_filter
            and not _is_flow_helper_domain(meta["domain"])
            and meta["entity_id"] not in safe_filter
        ):
            return None
        # Every row reports the id its payload names, not its stem: a filter
        # needs the stored id to settle the stem ambiguity anyway, and a stem
        # shown to the caller — digest or pre-digest — is a value the filter
        # would refuse or misroute, where the stored id is what the settings
        # UI and ha_manage_backup can send straight back and get that entity's
        # whole history. Flow-helper payloads also supply the stable config-entry
        # ID; other domains need only the header. A file that cannot identify
        # itself keeps its stem in an unfiltered
        # listing and is left out of a filtered one.
        payload_id = self._payload_entity_id(path)
        if entity_id and not _id_matches(payload_id, entity_id):
            return None
        if payload_id is not None:
            meta["entity_id"] = payload_id
        try:
            stat = path.stat()
        except OSError:
            return None
        meta["size"] = stat.st_size
        meta["mtime"] = stat.st_mtime
        return meta

    def _parse_filename(self, name: str) -> dict[str, Any] | None:
        m = _FILENAME_RE.match(name)
        if m is None:
            return None
        return {
            "name": name,
            "domain": m.group("domain"),
            "entity_id": m.group("entity_id"),
            "timestamp": m.group("ts"),
        }

    def read_snapshot(self, name: str) -> dict[str, Any]:
        path = self._resolve_snapshot_path(name)
        text = path.read_text()
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as err:
            raise ValueError(f"Snapshot {name!r} is not valid YAML: {err}") from err
        if not isinstance(data, dict):
            raise ValueError(f"Snapshot {name!r} is not a YAML mapping")
        sv = data.get("schema_version")
        if sv != SCHEMA_VERSION:
            raise ValueError(
                f"Snapshot {name!r} has unsupported schema_version={sv!r} "
                f"(expected {SCHEMA_VERSION})"
            )
        return data

    def delete_snapshot(self, name: str) -> Path:
        path = self._resolve_snapshot_path(name)
        with self._snapshot_pin_lock:
            if name in self._protected_snapshot_names:
                raise SnapshotInUseError(
                    "Snapshot is in use by a capture or active restore; retry after it finishes"
                )
            path.unlink()
            self._forget_entry_identity(name)
        return path

    def delete_bulk(
        self,
        *,
        domain: str | None = None,
        entity_id: str | None = None,
        older_than_days: int | None = None,
    ) -> BulkDeleteResult:
        """Delete snapshots matching ``domain`` / ``entity_id`` / age.

        Returns a dict ``{"deleted": [...], "failed": [...]}`` so callers
        can surface partial failures. ``failure_reasons`` optionally maps
        in-use filenames to a safe retryable code. Other exception details
        remain in the server WARNING log.
        """
        deleted: list[str] = []
        failed: list[str] = []
        failure_reasons: dict[str, Literal["snapshot_in_use"]] = {}
        cutoff: float | None = None
        if older_than_days is not None:
            if older_than_days < 0:
                raise ValueError("older_than_days must be >= 0")
            cutoff = time.time() - (older_than_days * 86400)
        for meta in self.list_snapshots(domain=domain, entity_id=entity_id):
            if cutoff is not None and meta["mtime"] >= cutoff:
                continue
            try:
                self.delete_snapshot(meta["name"])
                deleted.append(meta["name"])
            except (OSError, ValueError) as err:
                failed.append(meta["name"])
                if isinstance(err, SnapshotInUseError):
                    failure_reasons[meta["name"]] = "snapshot_in_use"
                logger.warning(
                    "Auto-backup: bulk-delete failed for %s: %s", meta["name"], err
                )
        result: BulkDeleteResult = {"deleted": deleted, "failed": failed}
        if failure_reasons:
            result["failure_reasons"] = failure_reasons
        return result

    def _resolve_snapshot_path(self, name: str) -> Path:
        """Validate a snapshot name and return its absolute Path.

        Rejects any name that contains path separators or escapes the
        backup directory.
        """
        if not name or os.sep in name or "/" in name or ".." in name:
            raise ValueError(f"Invalid snapshot name: {name!r}")
        path = (self._dir / name).resolve()
        # Defence-in-depth: post-resolve, verify still under backup_dir.
        try:
            path.relative_to(self._dir.resolve())
        except ValueError as err:
            raise ValueError(f"Invalid snapshot name: {name!r}") from err
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def _protect_snapshot(self, name: str) -> None:
        with self._snapshot_pin_lock:
            self._protected_snapshot_names[name] += 1

    def _unprotect_snapshot(self, name: str) -> None:
        with self._snapshot_pin_lock:
            self._protected_snapshot_names[name] -= 1
            if not self._protected_snapshot_names[name]:
                del self._protected_snapshot_names[name]
