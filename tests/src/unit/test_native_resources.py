"""Bundled skill resources keep their contract without blocking the HA loop."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from ._embedded_stubs import install

install()


async def test_skill_resource_reads_are_off_loop_and_preserve_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_resources import register_resources
    from ha_mcp._vendor.fastmcp import Client, FastMCP
    from ha_mcp._vendor.mcp.shared.exceptions import MCPError
    from ha_mcp.utils import skill_loader

    skill = tmp_path / "example"
    skill.mkdir()
    guide = (
        "---\nname: example\ndescription: Example practices\n---\nRead the reference."
    )
    (skill / "SKILL.md").write_text(guide, encoding="utf-8")
    (skill / "reference.md").write_text("Reference content", encoding="utf-8")
    monkeypatch.setattr(skill_loader, "get_skills_dir", lambda: tmp_path)
    loop_thread = threading.get_ident()
    original = Path.read_text

    def checked_read(path: Path, *args: object, **kwargs: object) -> str:
        assert threading.get_ident() != loop_thread, "Skill disk read blocked HA"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    mcp = FastMCP("Native resources")
    manager = SimpleNamespace(
        mcp=mcp, settings_ui=SimpleNamespace(_io=asyncio.to_thread)
    )
    await register_resources(manager)
    async with Client(mcp) as client:
        listed = await client.list_resources()
        assert {str(item.uri) for item in listed} == {
            "skill://example/SKILL.md",
            "skill://example/reference.md",
            "skill://example/_manifest",
        }
        assert (await client.read_resource("skill://example/SKILL.md"))[0].text == guide
        assert (await client.read_resource("skill://example/reference.md"))[
            0
        ].text == "Reference content"
        manifest = json.loads(
            (await client.read_resource("skill://example/_manifest"))[0].text
        )
        assert {item["path"] for item in manifest["files"]} == {
            "SKILL.md",
            "reference.md",
        }
        assert all(item["hash"] for item in manifest["files"])
        with pytest.raises(MCPError, match="Resource not found"):
            await client.read_resource("skill://example/../outside.md")


def test_native_instructions_share_notices_and_use_effective_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from custom_components.ha_mcp_tools.native_resources import build_instructions
    from custom_components.ha_mcp_tools.native_server_policy import NativePolicy
    from ha_mcp.server_instructions import OWNER_HEADING
    from ha_mcp.server_tool_text import READ_ONLY_INSTRUCTIONS
    from ha_mcp.utils import skill_loader

    skill = tmp_path / "example"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: example\ndescription: Example practices\n---\nBody",
        encoding="utf-8",
    )
    monkeypatch.setattr(skill_loader, "get_skills_dir", lambda: tmp_path)
    policy = NativePolicy(
        read_only=True,
        flags={"enable_tool_search": True},
        pinned={"available", "not_ported"},
        custom_instructions="Owner preference",
    )
    instructions = build_instructions(policy, {"available"})
    assert READ_ONLY_INSTRUCTIONS in instructions
    assert f"{OWNER_HEADING}\nOwner preference" in instructions
    assert "skill://example/SKILL.md" in instructions
    assert "fallback tool is disabled" in instructions
    assert "(available)" in instructions and "not_ported" not in instructions
