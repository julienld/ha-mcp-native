"""Component-owned file operations shared by services and native tool calls."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .const import ALLOWED_READ_DIRS, ALLOWED_WRITE_DIRS, DOMAIN
from .file_io import (
    DEFAULT_LOG_TAIL_LINES,
    TAILED_LOG_FILES,
    FileImage,
    FileWriteState,
    _delete_file_sync,
    _list_files_sync,
    _read_file_sync,
    _read_log_tail_sync,
    _write_file_sync,
    read_file_image,
)
from .file_paths import (
    ALLOWED_READ_FILES,
    _is_path_allowed_for_dir,
    _is_path_allowed_for_read,
)
from .file_views import _shape_read_file_response
from .package_dirs import _detect_package_dirs
from .runtime_generation import capture_component_generation

capture_component_generation(ToolFailure, await_owned_io, registry_update_lock)

_LOGGER = logging.getLogger(__package__)
DATA_FILE_ACCESS = "file_access_owner"
Guard = Callable[[bool], Awaitable[None]]
BeforeWrite = Callable[[str, FileImage], Awaitable[None]]
_HASS_DATA_ALLOWED_PATHS_KEY = "allowed_paths"
_HASS_DATA_EXTRA_YAML_KEYS_KEY = "extra_yaml_keys"


def _current_extra_dirs(hass: Any) -> list[str]:
    """Return the live user-configured extra directories from hass.data."""
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        paths = domain_data.get(_HASS_DATA_ALLOWED_PATHS_KEY)
        if isinstance(paths, list):
            return paths
    return []


def _current_extra_yaml_keys(hass: Any) -> list[str]:
    """Return the live component-configured extra YAML write keys from hass.data."""
    domain_data = hass.data.get(DOMAIN)
    if isinstance(domain_data, dict):
        keys = domain_data.get(_HASS_DATA_EXTRA_YAML_KEYS_KEY)
        if isinstance(keys, list):
            return keys
    return []


def service_file_access(hass: Any) -> FileAccess:
    """Service builders use the loaded entry's owner; direct tests may bind one."""
    data = hass.data.get(DOMAIN)
    owner = data.get(DATA_FILE_ACCESS) if isinstance(data, dict) else None
    return owner if isinstance(owner, FileAccess) else FileAccess(hass)


class FileAccess:
    def __init__(self, hass: Any, *, entry: Any = None) -> None:
        self.hass = hass
        self.entry = entry
        self.config_dir = Path(hass.config.config_dir)
        self.active = True
        self.pending: set[asyncio.Task[Any]] = set()
        self.stop_seconds = 10.0

    async def _authorize(self, guard: Guard | None, writes: bool) -> None:
        if not self.active:
            raise ToolFailure(
                "INTEGRATION_STOPPING",
                "The File & YAML entry is stopping",
                status="not_started",
            )
        if guard is not None:
            await guard(writes)
        if not self.active:
            raise ToolFailure(
                "INTEGRATION_STOPPING",
                "The File & YAML entry is stopping",
                status="not_started",
            )

    async def _io(
        self, guard: Guard | None, writes: bool, function: Any, *args: Any
    ) -> Any:
        await self._authorize(guard, writes)
        return await self.hass.async_add_executor_job(function, *args)

    async def run_owned(
        self,
        invoke: Any,
        guard: Guard | None,
        *,
        writes: bool,
        verify: Callable[[Guard | None], Awaitable[None]] | None = None,
    ) -> dict[str, Any]:
        state = FileWriteState()

        async def run() -> dict[str, Any]:
            try:
                await self._authorize(guard, writes)
                result: dict[str, Any] = await invoke(guard=guard, state=state)
                await self._authorize(guard, False)
                if not result.get("success", False):
                    raise ToolFailure(
                        "SERVICE_CALL_FAILED",
                        str(result.get("error", "File operation failed")),
                        data=result,
                        status="not_started",
                    )
                if verify is not None:
                    await verify(guard)
                return result
            except asyncio.CancelledError as error:
                if state.started:
                    raise self._unknown(state) from error
                raise
            except Exception as error:
                if state.started:
                    raise self._unknown(state) from error
                raise

        task = asyncio.create_task(run(), name="ha-mcp file operation")
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return await await_owned_io(task)

    @staticmethod
    def _unknown(state: FileWriteState) -> ToolFailure:
        message = "File I/O started but did not complete normally; inspect the target before retrying"
        return ToolFailure(
            "OUTCOME_UNKNOWN",
            message,
            data={
                "success": False,
                "error": message,
                "write_committed": True if state.committed else None,
                "post_write_verified": False,
            },
            status="outcome_unknown",
        )

    async def async_stop(self) -> None:
        self.active = False
        tasks = tuple(
            task for task in self.pending if task is not asyncio.current_task()
        )
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self.stop_seconds)
            for task in done:
                if not task.cancelled():
                    task.exception()
            if pending:
                raise RuntimeError(
                    "File I/O remains owned; retry unloading the entry after it drains"
                )

    async def list_files(
        self, path: str, pattern: str | None = None, *, guard: Guard | None = None
    ) -> dict[str, Any]:
        return await self.run_owned(
            partial(self._list_files, path, pattern),
            guard,
            writes=False,
            verify=partial(self._verify_read_path, path, directory=True),
        )

    async def read_file(
        self,
        path: str,
        tail_lines: int | None = None,
        yaml_path: str | None = None,
        include_parsed: bool = False,
        *,
        guard: Guard | None = None,
    ) -> dict[str, Any]:
        return await self.run_owned(
            partial(self._read_file, path, tail_lines, yaml_path, include_parsed),
            guard,
            writes=False,
            verify=partial(self._verify_read_path, path),
        )

    async def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        create_dirs: bool = True,
        *,
        guard: Guard | None = None,
        before_write: BeforeWrite | None = None,
    ) -> dict[str, Any]:
        return await self.run_owned(
            partial(
                self._mutate_file,
                path,
                partial(self._write_file, path, content, overwrite, create_dirs),
                before_write,
            ),
            guard,
            writes=True,
        )

    async def delete_file(
        self,
        path: str,
        *,
        guard: Guard | None = None,
        before_write: BeforeWrite | None = None,
    ) -> dict[str, Any]:
        return await self.run_owned(
            partial(
                self._mutate_file, path, partial(self._delete_file, path), before_write
            ),
            guard,
            writes=True,
        )

    async def read_image(
        self, path: str, *, guard: Guard | None = None, write_paths_only: bool = False
    ) -> FileImage:
        """Read an internal before-image, never a user-visible secrets projection."""

        async def read(*, guard: Guard | None, state: FileWriteState) -> dict[str, Any]:
            packages = await _detect_package_dirs(self.hass)
            await self._authorize(guard, False)
            extra = list(_current_extra_dirs(self.hass))

            def produce() -> FileImage:
                allowed = (
                    _is_path_allowed_for_dir(
                        self.config_dir, path, ALLOWED_WRITE_DIRS, extra
                    )
                    if write_paths_only
                    else _is_path_allowed_for_read(
                        self.config_dir, path, extra, packages
                    )
                )
                # Public root-secret reads are masked. An internal raw image
                # must never bypass that boundary through the backup API.
                if not allowed or os.path.normpath(path).lower() == "secrets.yaml":
                    raise ToolFailure(
                        "ACCESS_DENIED",
                        "Snapshot target is not allowed",
                        status="not_started",
                    )
                return read_file_image(self.config_dir / path)

            image = await self.hass.async_add_executor_job(produce)
            return {"success": True, "image": image}

        result = await self.run_owned(
            read,
            guard,
            writes=False,
            verify=partial(
                self._verify_read_path, path, write_paths_only=write_paths_only
            ),
        )
        image: FileImage = result["image"]
        return image

    async def _verify_read_path(
        self,
        path: str,
        guard: Guard | None,
        *,
        directory: bool = False,
        write_paths_only: bool = False,
    ) -> None:
        packages = await _detect_package_dirs(self.hass)
        await self._authorize(guard, False)
        extra = list(_current_extra_dirs(self.hass))
        validator = (
            partial(
                _is_path_allowed_for_dir,
                self.config_dir,
                path,
                ALLOWED_WRITE_DIRS if write_paths_only else ALLOWED_READ_DIRS,
                extra,
                None if write_paths_only else packages,
            )
            if directory or write_paths_only
            else partial(
                _is_path_allowed_for_read, self.config_dir, path, extra, packages
            )
        )
        allowed = await self.hass.async_add_executor_job(validator)
        await self._authorize(guard, False)
        if not allowed or extra != _current_extra_dirs(self.hass):
            raise ToolFailure(
                "ACCESS_DENIED", "Read path is not allowed", status="not_started"
            )

    async def _mutate_file(
        self,
        path: str,
        invoke: Any,
        before_write: BeforeWrite | None,
        *,
        guard: Guard | None,
        state: FileWriteState,
    ) -> dict[str, Any]:
        target = self.config_dir / path
        resolved = await self._io(guard, True, target.resolve)
        async with registry_update_lock("file", str(resolved)):

            async def check_target(path: str, image: FileImage) -> None:
                if image.real_path != resolved:
                    raise ToolFailure(
                        "CONFIG_CONFLICT",
                        "File target changed while waiting for its lock",
                        status="not_started",
                    )
                if before_write is not None:
                    await before_write(path, image)

            result: dict[str, Any] = await invoke(
                guard=guard, state=state, before_write=check_target
            )
            return result

    async def _before_file_write(
        self,
        path: str,
        guard: Guard | None,
        before_write: BeforeWrite | None,
    ) -> FileImage:
        image: FileImage = await self._io(
            guard, True, read_file_image, self.config_dir / path
        )
        if before_write is not None:
            await self._authorize(guard, True)
            await before_write(path, image)
        return image

    async def _write_io(
        self, path: str, guard: Guard | None, function: Any, *args: Any
    ) -> Any:
        await self._authorize(guard, True)
        extra_dirs = list(_current_extra_dirs(self.hass))

        def invoke() -> Any:
            # Path checks and the final image comparison run in the same job
            # as the write; a safety-snapshot await cannot reuse old rights.
            if not _is_path_allowed_for_dir(
                self.config_dir, path, ALLOWED_WRITE_DIRS, extra_dirs
            ):
                raise ToolFailure(
                    "ACCESS_DENIED",
                    "Write path is no longer allowed",
                    status="not_started",
                )
            return function(*args)

        return await self.hass.async_add_executor_job(invoke)

    async def _list_files(
        self,
        rel_path: str,
        pattern: str | None,
        *,
        guard: Guard | None,
        state: FileWriteState,
    ) -> dict[str, Any]:
        extra_dirs = _current_extra_dirs(self.hass)
        package_dirs = await _detect_package_dirs(self.hass)
        if not await self._io(
            guard,
            False,
            _is_path_allowed_for_dir,
            self.config_dir,
            rel_path,
            ALLOWED_READ_DIRS,
            extra_dirs,
            package_dirs,
        ):
            _LOGGER.warning("Attempted to list files in disallowed path: %s", rel_path)
            allowed_display = ALLOWED_READ_DIRS + sorted(package_dirs) + extra_dirs
            return {
                "success": False,
                "error": f"Path not allowed. Must be in: {', '.join(allowed_display)}",
                "files": [],
            }

        target_dir = self.config_dir / rel_path

        try:
            result = await self._io(
                guard, False, _list_files_sync, target_dir, self.config_dir, pattern
            )
        except PermissionError:
            _LOGGER.error("Permission denied accessing: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
                "files": [],
            }
        except OSError as err:
            _LOGGER.error("Error listing files in %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
                "files": [],
            }

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"Directory does not exist: {rel_path}",
                "files": [],
            }
        if err_kind == "not_a_dir":
            return {
                "success": False,
                "error": f"Path is not a directory: {rel_path}",
                "files": [],
            }

        files = result["files"]
        return {
            "success": True,
            "path": rel_path,
            "pattern": pattern,
            "files": files,
            "count": len(files),
        }

    async def _read_file(
        self,
        rel_path: str,
        tail_lines: int | None,
        yaml_path: str | None,
        include_parsed: bool,
        *,
        guard: Guard | None,
        state: FileWriteState,
    ) -> dict[str, Any]:
        extra_dirs = _current_extra_dirs(self.hass)
        package_dirs = await _detect_package_dirs(self.hass)
        if not await self._io(
            guard,
            False,
            _is_path_allowed_for_read,
            self.config_dir,
            rel_path,
            extra_dirs,
            package_dirs,
        ):
            _LOGGER.warning("Attempted to read disallowed path: %s", rel_path)
            allowed_patterns = (
                ALLOWED_READ_FILES
                + [f"{d}/**" for d in ALLOWED_READ_DIRS]
                + [f"{d}/*.yaml" for d in sorted(package_dirs)]
                + ["custom_components/**/*.py"]
                + [f"{d}/**" for d in extra_dirs]
            )
            return {
                "success": False,
                "error": f"Path not allowed. Allowed paths: {', '.join(allowed_patterns)}",
            }

        target_file = self.config_dir / rel_path

        try:
            if os.path.normpath(rel_path) in TAILED_LOG_FILES:  # noqa: ASYNC240
                # Bounded read: the fault log is append-only for the life of
                # the install, so never materialise the whole file for a tail.
                result = await self._io(
                    guard,
                    False,
                    _read_log_tail_sync,
                    target_file,
                    tail_lines if tail_lines else DEFAULT_LOG_TAIL_LINES,
                )
            else:
                result = await self._io(guard, False, _read_file_sync, target_file)
        except PermissionError:
            _LOGGER.error("Permission denied reading: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except UnicodeDecodeError:
            _LOGGER.error("Cannot read binary file: %s", rel_path)
            return {
                "success": False,
                "error": f"Cannot read binary file: {rel_path}. Only text files are supported.",
            }
        except OSError as err:
            _LOGGER.error("Error reading file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }
        if err_kind == "not_a_file":
            return {
                "success": False,
                "error": f"Path is not a file: {rel_path}",
            }

        return await _shape_read_file_response(
            self.hass,
            rel_path,
            result,
            tail_lines=tail_lines,
            yaml_path=yaml_path,
            include_parsed=include_parsed,
        )

    async def _write_file(
        self,
        rel_path: str,
        content: str,
        overwrite: bool,
        create_dirs: bool,
        *,
        guard: Guard | None,
        state: FileWriteState,
        before_write: BeforeWrite | None,
    ) -> dict[str, Any]:
        extra_dirs = _current_extra_dirs(self.hass)
        if not await self._io(
            guard,
            False,
            _is_path_allowed_for_dir,
            self.config_dir,
            rel_path,
            ALLOWED_WRITE_DIRS,
            extra_dirs,
        ):
            _LOGGER.warning("Attempted to write to disallowed path: %s", rel_path)
            return {
                "success": False,
                "error": f"Write not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS + extra_dirs)}",
            }

        target_file = self.config_dir / rel_path

        try:
            image = await self._before_file_write(rel_path, guard, before_write)
            result = await self._write_io(
                rel_path,
                guard,
                partial(_write_file_sync, state=state, expected=image),
                target_file,
                content,
                overwrite,
                create_dirs,
                self.config_dir,
            )
        except PermissionError:
            _LOGGER.error("Permission denied writing: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except OSError as err:
            _LOGGER.error("Error writing file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

        err_kind = result.get("_error")
        if err_kind == "exists_no_overwrite":
            return {
                "success": False,
                "error": f"File already exists: {rel_path}. Set overwrite=true to replace.",
            }
        if err_kind == "no_parent":
            return {
                "success": False,
                "error": f"Parent directory does not exist: {result['parent']}",
            }

        size = result["size"]
        modified_dt = datetime.fromtimestamp(result["mtime"])
        is_new = result["is_new"]

        _LOGGER.info("Wrote file: %s (%d bytes)", rel_path, size)

        return {
            "success": True,
            "path": rel_path,
            "size": size,
            "modified": modified_dt.isoformat(),
            "created": is_new,
            "message": f"File {'created' if is_new else 'updated'} successfully",
        }

    async def _delete_file(
        self,
        rel_path: str,
        *,
        guard: Guard | None,
        state: FileWriteState,
        before_write: BeforeWrite | None,
    ) -> dict[str, Any]:
        extra_dirs = _current_extra_dirs(self.hass)
        if not await self._io(
            guard,
            False,
            _is_path_allowed_for_dir,
            self.config_dir,
            rel_path,
            ALLOWED_WRITE_DIRS,
            extra_dirs,
        ):
            _LOGGER.warning("Attempted to delete from disallowed path: %s", rel_path)
            return {
                "success": False,
                "error": f"Delete not allowed. Must be in: {', '.join(ALLOWED_WRITE_DIRS + extra_dirs)}",
            }

        target_file = self.config_dir / rel_path

        try:
            image = await self._before_file_write(rel_path, guard, before_write)
            result = await self._write_io(
                rel_path,
                guard,
                partial(_delete_file_sync, state=state, expected=image),
                target_file,
            )
        except PermissionError:
            _LOGGER.error("Permission denied deleting: %s", rel_path)
            return {
                "success": False,
                "error": f"Permission denied: {rel_path}",
            }
        except OSError as err:
            _LOGGER.error("Error deleting file %s: %s", rel_path, err)
            return {
                "success": False,
                "error": str(err),
            }

        err_kind = result.get("_error")
        if err_kind == "not_found":
            return {
                "success": False,
                "error": f"File does not exist: {rel_path}",
            }
        if err_kind == "not_a_file":
            return {
                "success": False,
                "error": f"Path is not a file (cannot delete directories): {rel_path}",
            }

        size = result["size"]
        _LOGGER.info("Deleted file: %s (%d bytes)", rel_path, size)

        return {
            "success": True,
            "path": rel_path,
            "deleted_size": size,
            "message": f"File deleted successfully: {rel_path}",
        }
