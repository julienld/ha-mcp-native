# Port an existing tool to both backends

Implementation guide, 22 October 2026. These are working interfaces, not a claim
that the full catalog has native parity. The immutable catalogs and
[compatibility contract](compatibility.md) remain the review references.

## Target architecture: native first

Julien clarified the direction during implementation on 22 October: the custom
component is the canonical production path. Tools should call Core relatively
directly. The external server's REST/WebSocket implementation becomes a
compatibility mode that can eventually be deprecated. That deprecation has not
been activated.

New native handlers belong in the component and receive `hass` and the validated
`CallContext`. Call Core owners directly; use a small, typed service for a domain
whose ownership, captures or asynchronous lifecycle require one. Do not add a
universal operation-string dispatcher or a remote imitation of `hass`.

Keep `Engine`, public tool contracts, coercion and pure algorithms reusable.
`define_tool(handler=...)` can retain one signature/schema source while binding a
native handler. Every MCP, Assist or remote-component entry must still enter the
same Engine for policy, actor validation and operation ownership.

The remote-component path uses a standard FastMCP proxy to the existing HA MCP
endpoint. That endpoint already owns catalog/capability negotiation, calls,
resources and authentication; a second WebSocket tool RPC would duplicate it.
The proxy accepts an explicit MCP URL and SDK authentication. It must not add
legacy middleware or cache stale per-user catalog metadata. The pinned proxy's
background-task limitation does not restrict direct clients of the HA endpoint.

The optional [standard proxy factory](../../src/ha_mcp/native_proxy.py) can run a
stdio bridge with `create_native_component_proxy(native_mcp_url, auth=existing_token).run(transport="stdio")`.
It does not discover the secret endpoint or construct an old HA REST/WS server.

Select component delegation or the isolated legacy implementation before
invocation. Never fall back or retry a mutation after a lost or uncertain reply.
The older installation without the component retains its compatibility
implementation; it must not dictate the shape of new Core handlers.

The semantic backends below describe the first migration increment already in
the repository. They remain useful for existing tools and tests, but implementing
another pair of backends is not a prerequisite for each new native tool. Thin
existing paths progressively when it removes real indirection, keeping working
features available throughout the transition.

## Retain useful shared code

Move the existing public method, its signature, parameter metadata and result
projection to a class in `ha_mcp.runtime`. Bind it with `BackendTools`; its two
inputs are `backend` and `context`. Shared modules must import without Home
Assistant, an HA transport client, FastMCP or process-global settings.

The legacy registration uses `LegacyBackendTools` and decorates the shared
method with `legacy_errors`, `log_tool_usage` and the existing MCP metadata.
`legacy_flow` additionally binds the existing flow formatting settings. Keep
legacy acquisition overrides only where they are actual remote I/O. Do not
retain a second create/update/remove algorithm behind such an override.

Examples with different responsibilities:

- [StateTools](../../src/ha_mcp/runtime/states.py): read aggregation and projection.
- [HelperStorageTools](../../src/ha_mcp/runtime/helper_storage.py): canonical
  stored targets, validation, captures and mutation orchestration.
- [ConfigWriteContext](../../src/ha_mcp/runtime/config_write_context.py): the
  common before-image/hash/capture/write path for automation, script and scene.
- [RegistryWriteContext](../../src/ha_mcp/runtime/registry_write_context.py):
  one registry before-image and one snapshot for each real change.
- [IntegrationReadTools](../../src/ha_mcp/runtime/integration_reads.py) and
  [IntegrationWriteTools](../../src/ha_mcp/runtime/integration_writes.py): shared
  routing, options/schema handling and confirmed reconfiguration.

Use `define_tool(backend, Owner, "ha_existing_name", ...)` to derive both input
validation and JSON Schemas from the shared method. Preserve `source_module`,
tags and the old output schema. Its optional `handler` is for an existing owner
whose construction differs, such as a custom-code session; it is not a second
copy of that owner's method. `build_initial_catalog` publishes migrated tools.

## Existing finite semantic operations

The shared body calls `await backend.execute(operation, arguments, context)`.
An operation names a bounded capability such as `integrations.entries`,
`helpers.storage.write` or `config.script.write`. It is not a URL or a generic
WebSocket envelope.

`RemoteBackend` delegates to the existing clients, keeping their actual
transport behavior and error envelopes. Native ports belong under
`custom_components/ha_mcp_tools/native_operations`:

- Reuse component command `prep`/`do` when that operation already has a Core
  implementation. The native command map records its write effect explicitly.
- A synchronous Core registry owner can use a finite `DIRECT_OPERATIONS` map.
  Registered command schemas may validate input; do not call their handlers.
- An owner with awaits between validation and mutation needs `CallContext` and
  a fresh admission check at the real dispatch point. Use an owned operation
  class, as in `NativeConfigMutations`, rather than a context-free callback.

Use the actual Core serializer or producer. For example, diagnostics call the
integration's diagnostics callback and Core's artifact formatter with their
semantic arguments. They do not construct an HTTP request or invoke a view
handler. Missing owners and unknown operations are explicit capability failures;
there is no native HTTP/WS loopback fallback.

## Identity, policy and effects

`CallContext` carries a trusted actor/client identity plus an optional live
credential validator. `Backend.authorize` must revalidate it. A Core
`Context(user_id=...)` attributes a mutation but does not authorize it.

`Engine.call` canonicalizes aliases, validates arguments, determines effects,
checks policy and invokes the shared method. MCP, Assist, search proxies and
custom-code `call_tool` use this same path. Imported APIs and admin operations
use `call_owned`; `enforce_catalogue=False` is limited to trusted admin UI
actions and bypasses only catalog visibility, not caller scope or write policy.

For a mixed tool, `ToolDefinition.writes(validated_arguments)` determines the
effect before invocation. Native search proxies use that classifier too. A
conservative write hint is required when any branch can mutate. In particular,
opening an arbitrary Core configuration flow can commit immediately; calling it
"introspection" does not make it read-only. The explicit helper/integration
native metadata exceptions are checked separately from the legacy baseline.

`MutationAdmission` is invocation-local policy state, not actor authority.
Native mutators recheck it and the actor after awaited validation or snapshot
I/O, immediately before dispatch. Result filtering runs before all outward
projections and retains execution status.

Native template rendering also preserves Core's actual listener dependencies.
With visibility enforcement active, a reported all-state read or a tracked
domain containing hidden entities refuses the result before disclosure, including
scalar results. Individual entity references retain their existing filtering.
Condition checks do not provide this render-dependency contract: their existing
argument/result checks remain, without a claim of complete dynamic attribution.

## Captures, concurrency and unknown outcomes

Capture the exact target and before-image that informed the change. Pass those
values to the snapshot callback; do not re-resolve a renamed target after I/O.
Use `json_values_equal` for JSON comparisons: Python's `False == 0` is not a
valid configuration equality check. Preserve warnings when a best-effort backup
fails; its optional registry expectation still protects the observed identity.

The owners have different concurrency boundaries:

- Storage helpers validate with the real audited Core collection and compare
  the collection/registry expectation immediately before commit.
- YAML config writes use the registered Core edit owner's mutation lock,
  validator, fresh file read, writer and hook.
- Core registry callbacks compare the expected row after validation, then
  mutate without an intervening await.
- Options/subentry/reconfigure flows hold the native entry lock while owned and
  recheck `modified_at` when starting. This coordinates our flows; it is not a
  claim that external HA UI edits are locked out for every later flow step.

Install completion observers before dispatch and require actual correlated
evidence. A post-write hook task finishing does not prove a nonblocking reload
finished. Do not infer that a mutation was rejected merely because a subsequent
read happens to equal its old value.

After dispatch, callback or serialization failures are unknown outcomes unless
the owner provides stronger evidence. Never retry that mutation. Shield and
drain already-started owned I/O with `await_owned_io`. Parallel flow probes must
all finish or drain before their parent invocation returns an error.

Flow abort and watcher close may release only the caller's owned resource after
credential revocation; they must not allow continuation or a new mutation.
Failed cleanup retains ownership for a subsequent stop attempt. Shutdown tries
every resource owner and reports a failed drain instead of claiming completion.

## Evidence before publication

Preserve public input/output schemas, coercion, tags and result shapes with the
catalog guards and existing tool tests. Add focused tests for the new Core
boundary: actual target resolution, owner validation, read-only/revocation after
awaits, typed conflicts, mutate-then-raise, observer timing, cancellation and
cleanup. Keep remote-client round trips in the same cohort.

Run Ruff, focused mypy, module-size/duplication ratchets and
`scripts/extract_tools.py --check`. Test native import isolation and real FastMCP
search proxies for mixed effects. Core stubs and schema guards are not real
Home Assistant qualification. A matching HACS artifact must still pass the
isolated Core runner before its behavior can be marked verified in the inventory.
