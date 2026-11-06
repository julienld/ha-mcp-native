"""Presentation settings for command-line server entry points."""


def _get_show_banner() -> bool:
    """Read the validated SDK banner setting without importing it at startup."""
    from ha_mcp._vendor import fastmcp

    show_banner: bool = fastmcp.settings.show_server_banner
    return show_banner
