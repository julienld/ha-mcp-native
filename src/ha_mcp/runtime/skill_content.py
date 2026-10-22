"""Pure skill-content projection shared by all write-tool entry points."""

from typing import Any

_SKILLS_VENDOR_MISSING_WARNING = (
    "skill_content unavailable — the home-assistant-best-practices "
    "skills-vendor submodule is not initialised. The agent will not "
    "receive the relevant best-practice guidance for this write. "
    "Run `git submodule update --init` on the server install."
)


_SKILL_CONTENT_OPTOUT_HINT = (
    "Pass `MandatoryBPS=false` on subsequent calls to this tool in "
    "this session to skip this content."
)


def apply_skill_content(
    response: dict[str, Any],
    content: dict[str, str],
    *,
    enabled: bool,
    requested: bool,
    vendor_missing: bool,
) -> None:
    if content:
        # Reorder so the hint is the FIRST key in the response and the
        # bulky skill_content is the LAST. LLMs (especially smaller
        # models) process responses top-down and BAT showed the
        # trailing-hint placement was getting buried under ~25KB of
        # markdown — Opus needed five tries to find it, Sonnet/Haiku
        # never found it. The mutation is in place because callers
        # pass the dict by reference and expect their handle to keep
        # pointing at the same response object.
        existing_items = list(response.items())
        response.clear()
        response["skill_content_hint"] = _SKILL_CONTENT_OPTOUT_HINT
        response.update(existing_items)
        response["skill_content"] = content
        return

    if enabled and requested and vendor_missing:
        response.setdefault("warnings", []).append(_SKILLS_VENDOR_MISSING_WARNING)
