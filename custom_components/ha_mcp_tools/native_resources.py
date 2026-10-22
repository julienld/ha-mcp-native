"""Expose the bundled skill provider through HA-owned executor reads."""

from __future__ import annotations

import mimetypes
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from pydantic import PrivateAttr

from ha_mcp._vendor.fastmcp.resources.base import Resource
from ha_mcp._vendor.fastmcp.server.providers.skills import SkillsDirectoryProvider
from ha_mcp._vendor.fastmcp.server.providers.skills.skill_provider import (
    SkillFileResource,
    SkillResource,
)
from ha_mcp._vendor.mcp.shared.path_security import safe_join

from .native_server_policy import NativePolicy

_NOTICE = (
    "Use Home Assistant tools with the caller's permissions. Preserve errors and "
    "uncertain operation outcomes; do not retry an uncertain write automatically. "
    "This native runtime is experimental: consult each tool's schema and native "
    "support notes for the operations currently available."
)


def _skill_provider() -> SkillsDirectoryProvider | None:
    """Scan once in the executor, exactly as the standalone provider does."""
    from ha_mcp.utils.skill_loader import get_skills_dir

    mimetypes.init()
    directory = get_skills_dir()
    if directory is None:
        return None
    return SkillsDirectoryProvider(roots=[directory], supporting_files="resources")


def _read_file(source: SkillResource | SkillFileResource) -> str | bytes:
    """Resolve inside the trusted skill root again at every read."""
    if isinstance(source, SkillResource):
        if source.is_manifest:
            return cast(str, source._generate_manifest())
        relative = source.skill_info.main_file
    else:
        relative = source.file_path
    path = cast(Path, safe_join(source.skill_info.path, relative))
    if source.mime_type.startswith("text/"):
        return path.read_text(encoding="utf-8")
    return path.read_bytes()


class _ExecutorSkillResource(Resource):
    """Retain provider metadata while moving filesystem work off the HA loop."""

    _source: SkillResource | SkillFileResource = PrivateAttr()
    _io: Callable[..., Awaitable[Any]] = PrivateAttr()

    def __init__(
        self,
        source: SkillResource | SkillFileResource,
        io: Callable[..., Awaitable[Any]],
    ) -> None:
        fields = {name: getattr(source, name) for name in Resource.model_fields}
        fields["meta"] = source.get_meta()
        super().__init__(**fields)
        self._source = source
        self._io = io

    async def read(self) -> str | bytes:
        return cast(str | bytes, await self._io(_read_file, self._source))


async def register_resources(manager: Any) -> None:
    """Mount the same enumerated skill:// resources without a second loop."""
    provider = await manager.settings_ui._io(_skill_provider)
    if provider is None:
        return
    for resource in await provider.list_resources():
        if not isinstance(resource, (SkillResource, SkillFileResource)):
            raise TypeError("Unsupported bundled skill resource")
        manager.mcp.add_resource(
            _ExecutorSkillResource(resource, manager.settings_ui._io)
        )


@dataclass(frozen=True)
class _InstructionSettings:
    enable_tool_search: bool
    read_only_mode: bool
    custom_instructions: str


def build_instructions(policy: NativePolicy, enabled_tools: set[str]) -> str:
    """Use the shared composer with this entry's settings, never global Settings."""
    from ha_mcp.server_instructions import (
        build_skills_instructions,
        compose_instructions,
    )
    from ha_mcp.utils.skill_loader import get_skills_dir

    settings = _InstructionSettings(
        policy.flag("enable_tool_search"), policy.read_only, policy.custom_instructions
    )
    skills = build_skills_instructions(
        get_skills_dir(),
        settings,
        "ha_get_skill_guide" not in enabled_tools,
        pinned=sorted(policy.pinned & enabled_tools),
    )
    return _NOTICE + "\n\n" + compose_instructions(skills, settings)
