"""Read-only YAML fragment lookup for Home Assistant config files.

The read companion to ``ha_config_set_yaml``: same ``file`` + ``yaml_path``
addressing, so a fragment found here under a ``yaml_path`` the editor accepts
for that file can be handed straight back to it.

**Dependency:** Requires the ha_mcp_tools custom component (the round-trip parse
needs ``ruamel``, which the component carries and the MCP server does not).

Gating: behind ``enable_filesystem_tools``, NOT ENABLE_YAML_CONFIG_EDITING. The
editing flag gates *edits*, and reading a fragment is not one — but this returns
config-file contents through the same ``read_file``/``list_files`` component
services as ``ha_read_file``/``ha_list_files``, so it belongs behind the same
flag those are behind. Registering it unconditionally would hand an install that
deliberately turned filesystem tools off a config-file read surface through the
back door.
"""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.tool_api import ToolFailure

from ..backends.legacy import legacy_errors
from ..runtime.yaml_lookup import YamlLookup
from ..runtime.yaml_read_tools import YamlReadTools as YamlReadContractTools
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import read_only_hints
from .tools_filesystem import (
    _assert_mcp_tools_available,
    call_mcp_tools_service,
    is_filesystem_tools_enabled,
)
from .util_helpers import unwrap_service_response

logger = logging.getLogger(__name__)


class LegacyYamlFiles:
    def __init__(self, client: Any) -> None:
        self.client = client

    async def list_files(self, **arguments: Any) -> Any:
        return await self._call("list_files", arguments)

    async def read_file(self, **arguments: Any) -> Any:
        return await self._call("read_file", arguments)

    async def _call(self, service: str, arguments: dict[str, Any]) -> Any:
        result = await call_mcp_tools_service(self.client, service, arguments)
        return unwrap_service_response(result) if isinstance(result, dict) else result


class LegacyYamlRead:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def get(
        self,
        yaml_path: str,
        file: str = "configuration.yaml",
        include_content: bool = True,
        include_parsed: bool = False,
    ) -> dict[str, Any]:
        """Retain the existing component-service route for legacy callers."""
        try:
            await _assert_mcp_tools_available(self._client)

            return await YamlLookup(LegacyYamlFiles(self._client)).get(
                yaml_path, file, include_content, include_parsed
            )

        except (ToolError, ToolFailure):
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={
                    "tool": "ha_config_get_yaml",
                    "file": file,
                    "yaml_path": yaml_path,
                },
            )
            # Unreachable (the call is typed NoReturn) but explicit, because
            # CodeQL cannot see the NoReturn and would otherwise read this
            # handler as falling through to an implicit None (py/mixed-returns).
            # The sibling tools additionally carry a `return None` AFTER the
            # try/except; that shape only works because their try ends in a
            # raise_tool_error() call CodeQL treats as returning. This try ends
            # in a real return, so the same trailer is provably dead there
            # (py/unreachable-statement).
            return None


class YamlReadTools(YamlReadContractTools, LegacyYamlRead):
    def __init__(self, client: Any) -> None:
        LegacyYamlRead.__init__(self, client)
        YamlReadContractTools.__init__(self, self)

    ha_config_get_yaml = tool(
        name="ha_config_get_yaml",
        tags={"System", "beta"},
        annotations=read_only_hints("Read YAML Config Fragment", open_world=False),
    )(log_tool_usage(legacy_errors(YamlReadContractTools.ha_config_get_yaml)))


def register_yaml_read_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register the read-only YAML lookup tool.

    Behind ``enable_filesystem_tools`` (see module docstring): this returns
    config-file contents, so it registers with the same flag as the other
    file-read tools rather than with the YAML *editing* flag.
    """
    if not is_filesystem_tools_enabled():
        logger.debug("YAML read tool disabled (set HAMCP_ENABLE_FILESYSTEM_TOOLS=true)")
        return
    register_tool_methods(mcp, YamlReadTools(client))
