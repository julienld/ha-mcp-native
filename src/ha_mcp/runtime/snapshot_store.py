"""Small snapshot coordinator using the same file operations as legacy backups."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .snapshot_files import SnapshotFilesMixin

_LOGGER = logging.getLogger("ha_mcp.backup_manager")


class SnapshotStore(SnapshotFilesMixin):
    def __init__(
        self,
        directory: Path,
        *,
        retain_per_entity: int = 100,
        throttle_seconds: int = 0,
    ) -> None:
        self.directory = directory
        self.retention = max(1, retain_per_entity)
        self.throttle_seconds = max(0, throttle_seconds)
        self._capture_lock = threading.RLock()
        self._snapshot_pin_lock = threading.Lock()
        self._protected_snapshot_names: Counter[str] = Counter()
        self._entry_identity_lock = threading.Lock()
        self._entry_identity_cache = {}
        self._init_dir_error = None
        self._directory_checked = False
        self._last_capture: dict[tuple[str, str], float] = {}

    @property
    def _dir(self) -> Path:
        return self.directory

    @property
    def retain_per_entity(self) -> int:
        return self.retention

    def capture(
        self,
        domain: str,
        entity_id: str,
        config: Any,
        tool_name: str,
        *,
        force: bool = False,
    ) -> Path | None:
        """Capture one known pre-write state; invoke from an I/O executor."""
        if not re.fullmatch(r"[A-Za-z0-9_]+", domain):
            raise ValueError("Invalid snapshot domain")
        if not entity_id or config is None:
            return None
        key = (domain, entity_id)
        with self._capture_lock:
            now = time.monotonic()
            previous = self._last_capture.get(key)
            if (
                not force
                and previous is not None
                and now - previous < self.throttle_seconds
            ):
                return None
            path = self._write_snapshot(domain, entity_id, config, tool_name)
            self._protect_snapshot(path.name)
            try:
                self._rotate(domain, entity_id, frozenset({path.name}))
            except OSError as error:
                _LOGGER.warning("Auto-backup rotation failed: %s", type(error).__name__)
            finally:
                self._unprotect_snapshot(path.name)
            if not path.is_file():
                raise OSError("New snapshot disappeared before capture completed")
            self._last_capture[key] = now
            if len(self._last_capture) > 4096:
                for old in sorted(
                    self._last_capture, key=self._last_capture.__getitem__
                )[:1024]:
                    self._last_capture.pop(old, None)
            return path
