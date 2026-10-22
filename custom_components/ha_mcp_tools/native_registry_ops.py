"""Native registry operations reuse the existing owner/validator component seam."""

# This is a semantic capability map, never permission to execute arbitrary WS
# handlers. NativeBackend applies the schema and calls the do/prep owners.
COMMAND_OPERATIONS: dict[str, tuple[str, bool]] = {
    "registries.snapshot": ("ha_mcp_tools/registries", False),
    "assist.exposure": ("ha_mcp_tools/exposure", False),
}
