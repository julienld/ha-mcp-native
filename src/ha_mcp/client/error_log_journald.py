"""Compatibility imports for shared journald paging values and arithmetic."""

from ha_mcp.runtime.error_log_journald import (
    CURSOR_TOKEN_PREFIX as CURSOR_TOKEN_PREFIX,
)
from ha_mcp.runtime.error_log_journald import (
    JournaldFetch as JournaldFetch,
)
from ha_mcp.runtime.error_log_journald import (
    JournaldPage as JournaldPage,
)
from ha_mcp.runtime.error_log_journald import (
    container_page as container_page,
)
from ha_mcp.runtime.error_log_journald import (
    cursor_range_header as cursor_range_header,
)
from ha_mcp.runtime.error_log_journald import (
    cursor_token as cursor_token,
)
from ha_mcp.runtime.error_log_journald import (
    first_cursor as first_cursor,
)
from ha_mcp.runtime.error_log_journald import (
    is_cursor_token as is_cursor_token,
)
from ha_mcp.runtime.error_log_journald import (
    journald_page as journald_page,
)
from ha_mcp.runtime.error_log_journald import (
    parse_cursor_token as parse_cursor_token,
)
from ha_mcp.runtime.error_log_journald import (
    range_header as range_header,
)
from ha_mcp.runtime.error_log_journald import (
    token_cursor as token_cursor,
)
from ha_mcp.runtime.error_log_journald import (
    token_skip as token_skip,
)

__all__ = [
    "CURSOR_TOKEN_PREFIX",
    "JournaldFetch",
    "JournaldPage",
    "container_page",
    "cursor_range_header",
    "cursor_token",
    "first_cursor",
    "is_cursor_token",
    "journald_page",
    "parse_cursor_token",
    "range_header",
    "token_cursor",
    "token_skip",
]
