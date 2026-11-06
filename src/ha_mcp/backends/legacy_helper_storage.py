"""Compatibility binding for internal remote helper callers."""

from typing import Any

from ha_mcp.runtime.helper_storage import HelperStorageTools
from ha_mcp.tool_api import CallContext

from .remote import RemoteBackend


class LegacyHelperStorage(HelperStorageTools):
    # Private executors accept fields whose public facade already validated
    # collisions. The zone facade instead allows Core's normal suffixing.
    _check_create_name = False

    async def _attach_helper_skill(
        self, response: dict[str, Any], mandatory: bool
    ) -> None:
        from ha_mcp.tools.config_helpers.schemas import _attach_helper_skill

        _attach_helper_skill(response, mandatory)


def storage_tools(client: Any) -> LegacyHelperStorage:
    return LegacyHelperStorage(RemoteBackend(client), CallContext(None))
