"""The native API must not load standalone clients as a package side effect."""

import subprocess
import sys


def test_importing_shared_package_does_not_import_remote_stack() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys, ha_mcp, ha_mcp.tools.coercion
assert 'ha_mcp.client.rest_client' not in sys.modules
assert 'ha_mcp.server' not in sys.modules
assert 'ha_mcp.auth' not in sys.modules
assert 'ha_mcp.tools.device_control' not in sys.modules
assert 'Settings' in dir(ha_mcp)
from ha_mcp import Settings
assert Settings.__name__ == 'Settings'
""",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_existing_remote_exports_remain_available() -> None:
    from ha_mcp import (
        HomeAssistantClient,
        HomeAssistantOAuthProvider,
        HomeAssistantSmartMCPServer,
    )

    assert HomeAssistantClient.__name__ == "HomeAssistantClient"
    assert HomeAssistantOAuthProvider.__name__ == "HomeAssistantOAuthProvider"
    assert HomeAssistantSmartMCPServer.__name__ == "HomeAssistantSmartMCPServer"
