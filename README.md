# HA-MCP native candidate 9.0.0.dev3

Prepared distribution snapshot from [7e5bc20ec596341adefd2a3e027516304abdef53](https://github.com/julienld/ha-mcp-native/tree/7e5bc20ec596341adefd2a3e027516304abdef53).

This tree becomes installable only after its matching GitHub pre-release and assets are published. Select tag `9.0.0.dev3` with pre-releases enabled in HACS; the default branch is not this candidate.

Requires Home Assistant 2026.10.2 or newer. Install the attached `ha_mcp_tools.zip`, then restart Home Assistant. The library wheel is installed by Home Assistant from the exact URL and SHA-256 in the integration manifest.

Read the [installation, permissions and rollback guide](https://github.com/julienld/ha-mcp-native/blob/7e5bc20ec596341adefd2a3e027516304abdef53/docs/native-migration/installation.md) before switching runtimes. Existing configuration IDs remain in place. Do not delete the integration as an upgrade step.
