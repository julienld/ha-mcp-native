"""Native file calls use the shared component owner, with mandatory snapshots."""

import json
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import ToolFailure

from ..file_io import FileImage
from .file_entry import NativeFileEntry


class NativeFilesystem(NativeFileEntry):
    async def list_files(self, path: str, pattern: str | None = None) -> dict[str, Any]:
        return await (await self._owner()).list_files(path, pattern, guard=self.guard)

    async def read_file(
        self,
        path: str,
        tail_lines: int | None = None,
        yaml_path: str | None = None,
        include_parsed: bool = False,
    ) -> dict[str, Any]:
        return await (await self._owner()).read_file(
            path, tail_lines, yaml_path, include_parsed, guard=self.guard
        )

    async def write_file(
        self,
        path: str,
        content: str,
        overwrite: bool = False,
        create_dirs: bool = True,
    ) -> dict[str, Any]:
        async def capture(path: str, image: FileImage) -> None:
            await self._capture(path, image, "ha_write_file")

        return await (await self._owner(True)).write_file(
            path,
            content,
            overwrite,
            create_dirs,
            guard=self.guard,
            before_write=capture,
        )

    async def delete_file(self, path: str, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            message = "Deletion not confirmed. Set confirm=True to delete a file."
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                message,
                data=create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message,
                    suggestions=[
                        f"Call ha_delete_file(path={json.dumps(path)}, confirm=True) to proceed"
                    ],
                    context={"path": path},
                ),
                status="not_started",
            )

        async def capture(path: str, image: FileImage) -> None:
            await self._capture(path, image, "ha_delete_file")

        return await (await self._owner(True)).delete_file(
            path, guard=self.guard, before_write=capture
        )

    async def _capture(self, path: str, image: FileImage, source: str) -> None:
        await self.guard(True)
        await self.manager.snapshots.file_backup(
            {
                "domain": "file",
                "backup_id": path,
                "current_config": image.text,
                "source_tool": source,
            },
            self.actor,
        )
