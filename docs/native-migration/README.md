# Native HACS migration evidence

This directory records the public contracts of upstream revision
`2dc504fd97d8416f87b6ea7f2fd8ce0332615956`, before the native runtime migration.
It contains upstream code metadata only, never a real Home Assistant configuration
or credentials.

## Complete Core checkpoint

On 22 October 2026, source revision
[`23c6094c5`](https://github.com/julienld/ha-mcp-native/commit/23c6094c54253ee78a97970821f69c47b491533e)
passed [the complete native workflow](https://github.com/julienld/ha-mcp-native/actions/runs/39859887952):
17,570 unit tests passed, 132 were skipped, and 148 distinct checks passed on
Home Assistant Container 2026.10.2. Later changes need their own qualification;
this is an immutable checkpoint, not a claim about an untested branch head.

The Container checks include direct MCP and its standard proxy, Assist export,
Core LLM API import, OAuth grant translation and revocation, native tool
families, real local backup archives, entry reload, legacy/native switching,
and a complete Core process restart. After restart the original entry, MCP URL,
actor token and both LLM directions remain usable. The final process has only
the HA listener on 8123, no server thread, remote client import, loopback attempt
or remaining Engine task. Shared Rich, MCP, Pydantic, httpx and cryptography
versions remain unchanged, with no new dependency conflicts.

The static catalogue declares 86 historical tools; the conditional Code tool
completes the original 87. The skill guide and native API bridge add their own
surfaces. Declaration and schema preservation do not establish every provider's
runtime behavior. The Container cannot qualify physical radios, HAOS/Supervisor,
a real screenshot renderer, authenticated HACS, third-party clients or a full
installation restore. Public release delivery also requires a separate test of
Core's installer against the actual wheel URL; the unpublished-candidate lane
uses the byte-identical wheel locally. See the
[compatibility limits](compatibility.md) and
[candidate installation guide](installation.md).

## Contract references

- [Compatibility contract](compatibility.md): installation, endpoints,
  authentication, Assist and deliberately bounded native transport behavior.
- [Machine-readable compatibility contract](compatibility.json): identifiers,
  persisted-state requirements and explicit exceptions.
- [Porting guide](porting.md): the implemented shared-body, semantic-operation,
  policy, snapshot, concurrency and lifecycle interfaces for subsequent tools.
- [Runtime catalog baseline](catalog-baseline.json): actual FastMCP input/output
  JSON Schemas, annotations and names for every decorated tool, with all boolean
  feature flags and developer mode enabled. The 87 entries include 8 beta-tagged
  tools. These are not 87 stable tools plus another 8 tools.
- [Server-generated catalog baseline](catalog-extras-baseline.json): stable
  schemas, title, hints and tags of `ha_get_skill_guide`, captured separately
  from the same immutable upstream archive. Its asset-dependent description is
  excluded because Git archives omit submodule assets. This extra tool does not
  increase the decorated-tool count of 87.
- [Migration inventory](inventory.json): tools grouped by implementation family,
  candidate REST/WebSocket calls, command strings and existing Core prep/do seams.
  Every tool starts `not_verified`; candidate source links do not establish native
  implementation or behavioral parity.

The inventory has **module-level scope**. A tool can inherit sibling tools' call
sites, while dynamically dispatched and transitive calls can be absent. Counts
are useful for locating work, not estimating effort or proving transport removal.
The inventory includes Core-owner functions already separated from WebSocket
envelopes. Those are reuse opportunities; they are not themselves a native backend.
`shared_source` and `semantic_operation_candidates` follow shared methods into
their actual implementation module. `native_catalog_declaration` records only a
declaration in the current native catalog; it never establishes verified parity.
Its explicit source list includes the component's composed Core bindings and the
initial migration catalog. Replaced getters count once. A contract test compares
that static list with the actual component catalog, so adding a binding also
requires updating the inventory sources.
The working-tree scan evolves during porting while both baseline catalogs remain
immutable preservation references.

Regenerate the current static inventory with:

```text
uv run -s scripts/native_inventory.py
uv run -s scripts/native_inventory.py --check
```

The adjacent PEP 723 lock has no third-party dependencies. Capturing the runtime
catalog additionally needs the repository's development environment:

```text
uv run --no-sync python scripts/native_inventory.py --capture-runtime
```

`catalog-baseline.json` is a migration reference, not an automatically refreshed
generated file. Do not replace the baseline with a changed tool surface merely to
make a compatibility failure pass. Assess intentional public changes explicitly.
Unit guards allow new optional parameters and new tools but detect lost baseline
tools, changed existing parameter contracts, newly required inputs and changed
MCP safety annotations. Live Core tests establish results, permission handling,
confirmation and failure semantics; catalog tests cannot establish those.

## Surfaces outside the decorated catalog

The raw registry is only one part of the public surface:

- `server.py` always adds `ha_get_skill_guide`, including an explicit unavailable
  error when the bundled skill is missing. Do not turn a packaging failure into
  silent absence.
- `CategorizedSearchTransform` adds `ha_search_tools`, `ha_call_read_tool`,
  `ha_call_write_tool` and `ha_call_delete_tool` when tool search is enabled.
  These must invoke the same policy and tool engine as direct calls. Code-mode
  transforms add their own execution/discovery surfaces; they are conditional.
- `SkillsDirectoryProvider` exposes `skill://...` resources and supporting files
  from the bundled skills submodule. That bundle must actually be included in a
  HACS artifact. The baseline has no project-owned prompt registrations; a
  historical comment mentioning “expert prompts” is not evidence of any.
- Component search schemas vary with the connected component's capabilities.
  Static capture records the raw tool contract, not every negotiated variant.
- LLM exports have `full`, `tool_search` and `both` selections, independent of
  MCP catalog search, plus persisted per-tool exposure and policy settings.
- Retired names in `renamed_tools.py` remain callable through alias middleware.
  Blueprint aliases also adapt arguments. Renamed settings and policy keys keep
  the most restrictive state when multiple old tools consolidate.

Qualification must exercise direct MCP, search proxies, custom-code `call_tool`
and Assist. A successful direct call does not prove a proxy's policy cannot be
bypassed.
