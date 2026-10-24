"""Retain a file before-image across the shared restore safety capture."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.runtime.snapshot_payloads import InvalidBackupSnapshotError
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..file_io import FileImage
from ..file_views import _extract_yaml_views
from ..yaml_editing import YamlEditor
from .file_entry import NativeFileEntry
from .yaml_config import NativeYamlConfig


@dataclass(slots=True)
class NativeFileRestore:
    manager: Any
    actor: CallContext
    domain: str
    identifier: str
    saved: str = field(repr=False)
    entry: NativeFileEntry = field(init=False, repr=False)
    path: str = field(init=False)
    yaml_path: str = field(default="", init=False)
    state: FileImage | None = field(default=None, init=False, repr=False)
    before: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.domain not in {"file", "yaml", "yaml_file"} or not isinstance(
            self.saved, str
        ):
            raise InvalidBackupSnapshotError("File snapshots must contain text")
        self.path = self.identifier
        if self.domain == "yaml":
            self.path, sep, self.yaml_path = self.identifier.rpartition("::")
            if not sep or not self.path or not self.yaml_path:
                raise InvalidBackupSnapshotError(
                    "YAML snapshot target must be file::yaml_path"
                )
        self.entry = (
            NativeFileEntry(self.manager, self.actor, require_auto_backup=False)
            if self.domain == "file"
            else NativeYamlConfig(self.manager, self.actor)
        )
        # The coordinator captures the mandatory safety image, independently
        # of the toggle for automatic captures made by ordinary edit tools.
        self.entry.require_auto_backup = False

    def options(self) -> dict[str, Any]:
        assert isinstance(self.entry, NativeYamlConfig)
        return {
            "file": self.path,
            "yaml_path": self.yaml_path,
            "action": "replace" if self.domain == "yaml" else "replace_file",
            "content": self.saved,
            "require_confirm": False,
            **self.entry._options(),
        }

    async def guard(self, writes: bool) -> None:
        if self.domain == "yaml":
            assert isinstance(self.entry, NativeYamlConfig)
            await self.entry.edit_guard(self.path, self.yaml_path, "replace", writes)
        else:
            await self.entry.guard(writes)

    async def current(self) -> str | None:
        files = await self.entry._owner()
        if self.domain != "file":
            await YamlEditor(files)._validate(
                self.options(), posixpath.normpath(self.path), self.entry.guard
            )
        image = await files.read_image(
            self.path, guard=self.entry.guard, write_paths_only=self.domain == "file"
        )
        self.state = image
        if self.domain == "yaml" and image.text is not None:
            views = await await_owned_io(
                files._io(
                    self.entry.guard,
                    False,
                    _extract_yaml_views,
                    image.text,
                    self.yaml_path,
                )
            )
            if views.get("parse_error"):
                raise ToolFailure(
                    "CONFIG_INVALID",
                    "Current YAML could not be read for a safety snapshot",
                    status="not_started",
                )
            self.before = views["subtree"]
        else:
            self.before = image.text
        await self.entry.guard(False)
        return self.before

    comparison = current

    async def apply(self, before: str | None) -> dict[str, Any]:
        from homeassistant.core import Context

        if self.state is None or before != self.before:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "File restore lost its safety before-image",
                status="not_started",
            )
        files = await self.entry._owner()

        async def compare(path: str, image: FileImage) -> None:
            if image != self.state:
                raise ToolFailure(
                    "CONFIG_CONFLICT",
                    "File changed during the safety backup; restore was not applied",
                    status="not_started",
                )

        if self.domain == "file":
            return await files.write_file(
                self.path,
                self.saved,
                overwrite=True,
                guard=self.guard,
                before_write=compare,
            )
        return await YamlEditor(files).edit(
            self.options(),
            guard=self.guard,
            before_write=compare,
            context=Context(user_id=self.actor.user_id),
        )
