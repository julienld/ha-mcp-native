# HA-MCP native candidate 9.0.0.dev2

Prepared distribution snapshot from [7402a72e1595029a29f09b0a299ac462e1b57159](https://github.com/julienld/ha-mcp-native/tree/7402a72e1595029a29f09b0a299ac462e1b57159).

This tree becomes installable only after its matching GitHub pre-release and assets are published. Select tag `9.0.0.dev2` with pre-releases enabled in HACS; the default branch is not this candidate.

Requires Home Assistant 2026.10.2 or newer. Install the attached `ha_mcp_tools.zip`, then restart Home Assistant. The library wheel is installed by Home Assistant from the exact URL and SHA-256 in the integration manifest.

Read the [installation, permissions and rollback guide](https://github.com/julienld/ha-mcp-native/blob/7402a72e1595029a29f09b0a299ac462e1b57159/docs/native-migration/installation.md) before switching runtimes. Existing configuration IDs remain in place. Do not delete the integration as an upgrade step.
