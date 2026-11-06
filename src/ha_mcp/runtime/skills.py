"""Shared skill guide, file safety and response formatting without MCP or HA."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.runtime.errors import raise_failure
from ha_mcp.server_tool_text import SKILL_TOOL_NAME
from ha_mcp.tool_api import BackendTools
from ha_mcp.utils.skill_loader import (
    BEST_PRACTICES_SKILL_NAME,
    best_practices_skill,
    get_skills_dir,
    list_skill_files,
)

_SKILL_GUIDE_MANDATORYBPS_HINT = "You now have this best-practice reference in your context. Pass `MandatoryBPS=false` on subsequent write-tool calls in this session (ha_config_set_automation / _script / _scene / _helper / _dashboard / _yaml) to avoid re-receiving the canonical reference files inline."


class SkillGuideMixin:
    def _guide_raise(self, payload: dict[str, Any]) -> NoReturn:
        raise_failure(payload)

    def _guide_ack_line(self) -> str | None:
        return None

    def _handle_skill_guide_call(
        self, skills_dir: Path | None, file: str = "SKILL.md"
    ) -> dict[str, Any]:
        """Serve one file of the bundled best-practices skill.

        Split out from the registered async closure so the same logic is
        unit-testable without round-tripping through the MCP tool layer.
        Synchronous because every operation is bounded local disk I/O.

        The default is SKILL.md, whose table links every reference file
        by the path this tool accepts. ``file`` is matched exactly against
        the regular files that exist in the skill; no path is built from
        it, so traversal, absolute paths and symlinks are refused by the
        same lookup. Failures raise ``ToolError`` (via
        ``raise_tool_error``) per .gemini/styleguide.md.
        """
        skill = BEST_PRACTICES_SKILL_NAME
        entry = best_practices_skill(skills_dir)
        if entry is None:
            self._guide_raise(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=(
                        f"The {skill!r} skill is not available on this "
                        "server. The skills-vendor submodule may be missing "
                        "or uninitialized, or its SKILL.md failed to parse."
                    ),
                    context={"file": file},
                    suggestions=[
                        "Ask the server operator to initialize the "
                        "skills-vendor submodule "
                        "(`git submodule update --init`).",
                    ],
                )
            )

        skill_dir = entry[1]
        requested = file or "SKILL.md"
        allowed = list_skill_files(skill_dir)
        if requested not in allowed:
            self._guide_raise(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    message=f"Unknown file {requested!r} in skill {skill!r}.",
                    context={"file": requested},
                    suggestions=[
                        "Pass one of these paths exactly as written: "
                        + ", ".join(allowed),
                        f"Call {SKILL_TOOL_NAME}() with no arguments to read "
                        "SKILL.md, whose table says which file to read.",
                    ],
                )
            )
        content = self._read_skill_file_content(skill, requested, skill_dir / requested)
        return self._build_skill_guide_response(skill, requested, content)

    def _read_skill_file_content(self, skill: str, file: str, target: Path) -> str:
        """Read one skill file, raising a structured error on I/O failure."""
        try:
            return target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return self._guide_raise(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    message=f"Could not read file {file!r} in skill {skill!r}: {e}",
                    context={"skill": skill, "file": file},
                    suggestions=[
                        "Check filesystem permissions on the skills-vendor directory.",
                        "Check the server logs for the underlying OSError.",
                    ],
                )
            )

    def _build_skill_guide_response(
        self, skill: str, file: str, content: str
    ) -> dict[str, Any]:
        """Build the response for one skill file.

        In strict mode the acknowledgment key goes at the top of every
        file this tool serves (#1779), so a model obtains it whether it
        reads SKILL.md or the reference file a block error points to.
        The skill:// resources read raw files and never carry it;
        resource-preferring clients recover through the block error's
        suggestion, which points back at this tool.

        The ``MandatoryBPS=false`` hint rides only on reference files.
        SKILL.md is what every first call returns, and a model that took
        the hint there would drop the reference files the write tools
        attach before it had read any of them.
        """
        if ack_line := self._guide_ack_line():
            content = f"{ack_line}\n\n{content}"

        response: dict[str, Any] = {}
        if file != "SKILL.md":
            response["skill_content_hint"] = _SKILL_GUIDE_MANDATORYBPS_HINT
        response.update(
            {
                "success": True,
                "file": file,
                "uri": f"skill://{skill}/{file}",
                "content": content,
            }
        )
        if file == "SKILL.md":
            response["how_to_use"] = (
                f"Call {SKILL_TOOL_NAME}(file='<path>') for the reference "
                "files the table above points to for your task, using the "
                "path exactly as linked (e.g. 'references/triggers-and-"
                "conditions.md'). Read only those; do not load every file."
            )
        return response


class SkillGuideReader(SkillGuideMixin):
    def __init__(
        self,
        ack_line: Callable[[], str | None] | None = None,
        skills_dir: Callable[[], Path | None] = get_skills_dir,
    ) -> None:
        self.ack_line = ack_line
        self.skills_dir = skills_dir

    def _guide_ack_line(self) -> str | None:
        return self.ack_line() if self.ack_line else None

    def read(self, file: str = "SKILL.md") -> dict[str, Any]:
        return self._handle_skill_guide_call(self.skills_dir(), file)


class SkillGuideTools(BackendTools):
    async def ha_get_skill_guide(
        self,
        file: Annotated[
            str,
            Field(
                description="Path of the file to read, exactly as SKILL.md links it (e.g. 'references/triggers-and-conditions.md'). Omit to read SKILL.md."
            ),
        ] = "SKILL.md",
    ) -> dict[str, Any]:
        """Get the bundled Home Assistant best-practices skill."""
        result: dict[str, Any] = await self.backend.execute(
            "skills.guide", {"file": file}, self.context
        )
        return result
