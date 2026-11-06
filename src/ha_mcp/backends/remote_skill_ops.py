"""Local skill reads for the remote server; its legacy settings stay here."""

import asyncio
from typing import Any

from ha_mcp.runtime.skill_files import read_skill_files
from ha_mcp.runtime.skills import SkillGuideReader


async def execute_skill_operation(
    operation: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    def read() -> dict[str, Any]:
        from ha_mcp.config import get_global_settings
        from ha_mcp.strict_bps import strict_bps_ack_line, strict_bps_effective

        if operation == "skills.files":
            return read_skill_files(
                arguments["references"],
                enabled=get_global_settings().enable_mandatory_bps,
            )
        return SkillGuideReader(
            ack_line=lambda: strict_bps_ack_line() if strict_bps_effective() else None
        ).read(arguments.get("file", "SKILL.md"))

    return await asyncio.to_thread(read)
