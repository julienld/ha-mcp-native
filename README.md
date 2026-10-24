# HA-MCP native candidate 9.0.0.dev4

Prepared distribution snapshot from [7ffe0e7041e1a13022aed6e6a9a2cb2e4256c48c](https://github.com/julienld/ha-mcp-native/tree/7ffe0e7041e1a13022aed6e6a9a2cb2e4256c48c).

This tree becomes installable only after its matching GitHub pre-release and assets are published. Select tag `9.0.0.dev4` with pre-releases enabled in HACS; the default branch is not this candidate.

Requires Home Assistant 2026.8.0 or newer. Install the attached `ha_mcp_tools.zip`, then restart Home Assistant. The library wheel is installed by Home Assistant from the exact URL and SHA-256 in the integration manifest.

Read the [installation, permissions and rollback guide](https://github.com/julienld/ha-mcp-native/blob/7ffe0e7041e1a13022aed6e6a9a2cb2e4256c48c/docs/native-migration/installation.md) before switching runtimes. Existing configuration IDs remain in place. Do not delete the integration as an upgrade step.
