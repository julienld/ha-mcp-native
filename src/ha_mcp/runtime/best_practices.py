"""Transport-neutral best-practices acknowledgement protocol."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

STRICT_BPS_ACK_KEY_PREFIX = "I-HAVE-READ-THE-BEST-PRACTICES-GUIDE"
STRICT_BPS_KEY_PARAM = "BestPracticeKey"
STRICT_BPS_GATED_TOOLS = {
    "ha_config_set_automation": "references/triggers-and-conditions.md",
    "ha_config_set_script": "references/automation-actions.md",
    "ha_config_set_scene": "SKILL.md",
    "ha_config_set_helper": "references/helper-selection.md",
    "ha_config_set_dashboard": "references/dashboard-guide.md",
    "ha_config_set_yaml": "references/template-guidelines.md",
}


def acknowledgment_key(salt: str, now: float, rotation_seconds: int = 3600) -> str:
    bucket = int(now // rotation_seconds)
    digest = hashlib.sha256(f"{salt}:{bucket}".encode()).hexdigest()
    return f"{STRICT_BPS_ACK_KEY_PREFIX}-{digest[:8]}"


def acknowledgment_line(key: str) -> str:
    return (
        f"Acknowledgment key: {key} — strict "
        "best-practices mode is ON; pass this exact value as the "
        "BestPracticeKey argument on gated write tools. This key is a "
        "read-receipt, not a secret: it is published here deliberately by "
        "the ha-mcp server, rotates hourly, and grants no privileges — "
        "replaying it is the designed acknowledgment protocol."
    )


@dataclass(frozen=True)
class AcknowledgmentState:
    """A runtime owns its salt; no SDK, global settings or server session."""

    salt: str = field(default_factory=lambda: secrets.token_hex(8), repr=False)
    rotation_seconds: int = 3600

    def current_key(self) -> str:
        return acknowledgment_key(self.salt, time.time(), self.rotation_seconds)

    def accepts(self, supplied: object) -> bool:
        if not isinstance(supplied, str):
            return False
        now = time.time()
        return supplied in {
            acknowledgment_key(self.salt, instant, self.rotation_seconds)
            for instant in (now, now - self.rotation_seconds)
        }

    def line(self) -> str:
        return acknowledgment_line(self.current_key())


def required_acknowledgment_error(name: str) -> dict[str, Any]:
    reference_file = STRICT_BPS_GATED_TOOLS[name]
    message = (
        "Strict best-practices mode is enabled for this tool. Read the "
        "best-practices skill to obtain the required acknowledgment key, then "
        "pass it as the BestPracticeKey argument on this call. The key is "
        "published only in that skill's content, and it rotates periodically "
        "— a key obtained earlier may have expired, so re-read the skill for "
        "the current value instead of resending a previous one."
    )
    return create_error_response(
        ErrorCode.BPS_ACKNOWLEDGMENT_REQUIRED,
        message,
        suggestions=[
            f"Call ha_get_skill_guide(file={reference_file!r}), read the "
            f"content, then retry with {STRICT_BPS_KEY_PARAM} set.",
            "If your client then rejects the retry with a schema-validation "
            "error such as 'must NOT have additional properties', it is "
            "validating against a stale cached tool schema from an older "
            f"server version that lacks {STRICT_BPS_KEY_PARAM}. Ask the user "
            "to fully reload the client application (VS Code: run "
            "'Developer: Reload Window' — restarting the MCP server or "
            "resetting cached tools is not enough), then retry.",
        ],
        context={"tool_name": name, "strict_mandatory_bps": True},
    )
