# Native migration compatibility contract

Baseline: upstream `2dc504fd97d8416f87b6ea7f2fd8ce0332615956` (22 October 2026).
The [machine contract](compatibility.json) lists the persisted identifiers. This
is a preservation target, not a statement that every target is implemented.

## Keep installation identity and stored intent

Keep the integration domain `ha_mcp_tools`, existing config-entry IDs and the two
entry types. Missing `entry_type` historically means the File & YAML Tools entry.
The tools entry's unique ID is `ha_mcp_tools`; the server entry's is
`ha_mcp_tools-server`. Replacing the server must not force users to delete and
re-add either entry.

Keep shared component commands available from either active entry, while
privileged filesystem/YAML services stay tools-entry-only. Remote servers still
use the existing REST/WebSocket command surface and negotiate capabilities.
Core has no unregister operation for WebSocket commands: entry unload clears
entry state but must not attempt to undo global command registration.

Preserve webhook IDs, OAuth client IDs/secrets/signing keys, the DCR signing key,
redirect allowlists, feature flags, policies, exposure overrides, saved custom
tools and backend-independent backup metadata. Do not log or place credentials
in generated evidence. Keeping old credential fields for rollback does not mean
using the administrator token for native calls. Revoke only credentials the
component issued, with the existing ownership rules; never delete a human user.

The new runtime can add a native-owner ID for secret-URL and legacy-OAuth modes.
It must validate that owner is still active and authorized. In `ha_auth` mode,
the calling human administrator is a separately authenticated principal.
`Context(user_id=...)` supplies attribution, not authorization. Changing actions'
attribution from the historical server account to the calling account is an
observable behavior change and must be documented and tested.

## Calendar recovery limits

Calendar history retains the saved UID and occurrence scope. Core's calendar
owner exposes bounded event queries, so an unlocated UID is not proof that an
event was deleted. Native automatic restore updates a located occurrence. If
it cannot locate the identity, it reports `calendar_identity_unavailable` with
the saved arguments for an explicit creation; it does not create a possible
duplicate. A provider exception after update is an unknown outcome, with no
creation fallback. Ranged and series-rule writes do not claim a complete,
restorable series snapshot.

## Camera image content

The native camera tool calls Core's camera API, preserving the served bytes,
plain text caption, dimensions and local retrieval time. It uses no camera proxy
HTTP connection or administrator token. Images are bounded at 20 MiB each;
Core retains ownership of best-effort resizing and its camera timeout.

Assist cannot receive image content. An image-only tool is refused before
capturing an image, while a completed dashboard write can retain its structured
outcome with a warning when its supplementary screenshot cannot be delivered.
Active entity visibility enforcement refuses unfilterable camera pixels before
capture. Normal MCP clients continue to receive standard image content blocks.

## Preserve the HA-hosted MCP URLs

Keep `/api/webhook/{webhook_id}` and its `/readonly` alias. Preserve the webhook
registration and Nabu Casa/cloudhook integration; route an accepted request to
FastMCP locally instead of proxying it through another HTTP listener. Preserve
methods, CORS, authentication challenges, streaming and proxy-derived public
origin handling. Do not expose the secret ID through a fixed discovery endpoint.

Keep the component-scoped OAuth endpoints and metadata routes. Existing issued
grants depend on their signing keys and client identity translation, not just
the visible MCP URL. In HA-account mode Core remains the authority, including
refresh and revocation of `hamcp-rt-` envelopes. DCR/CIMD translation, redirect
checks and issuer response handling are existing client compatibility, not
optional new features. Legacy root `/authorize` and `/token` aliases retain
their route-ownership conflict behavior with the Webhook Proxy app (add-on).

Secret-URL mode stays a secret-URL credential. Cosmetic OAuth tokens in that mode
must not silently become a different authentication authority. `ha_auth` accepts
active human administrators and rejects inactive, non-admin and system accounts.
Legacy OAuth remains a single-tenant administrator-equivalent credential.

### A direct server-port URL is an explicit exception

The old component can expose `http://host:{server_port}/{secret_path}` directly.
Keeping that exact URL while removing every listener other than HA is impossible
when the port differs from HA's port. Similarly, old `enable_webhook=False` leaves
the direct listener as the only MCP endpoint; it cannot retain that behavior if
the listener is removed.

A native migration must give this case an explicit, recoverable route: use an
HA-hosted endpoint with the intended local-only/authenticated access policy, or
keep the old runtime as a deliberate compatibility option. Never automatically
enable a public webhook to compensate. `server_port`, `bind_host` and
`secret_path` can remain stored for rollback even when the native runtime does
not use them. The promise of unchanged URL applies to HA-hosted webhook URLs.

## Preserve Assist identity and the common engine

Existing conversation agents select APIs by ID, not their display label:

- Full catalog: `ha_mcp_tools-{config_entry_id}`.
- Compact tool search: `ha_mcp_tools-{config_entry_id}-toolsearch`.

Keep those IDs and the exposure option values `full`, `tool_search`, `both`.
The richer neutral tool API must adapt to Core's current `llm.API`, `llm.Tool`
and `ToolResult`; it must not monkeypatch the global Core LLM API. Preserve tool
schemas and coercion, result/error distinction, limits and per-tool exposure.
Anonymous Assist/satellite calls receive no implicit administrator privileges.

Imported Core LLM APIs resolve the current API for the authenticated invocation,
retain Core's validator, carry `external=True` where appropriate, and exclude
the integration's own export IDs to prevent recursion. Serialization may lose
MCP-specific rich content at the Core boundary; advertise that explicitly.

## Bound the arbitrary transport escape hatches

`ha_call_service(ws_command=...)` currently accepts arbitrary one-shot native
commands, apart from streaming/two-phase operations, service invokers, component
commands, protected writes and reserved envelopes. A native backend cannot
support an unknown third-party command by calling its owning Core API unless
that owner has a registered semantic operation. Calling raw handlers with a fake
connection would re-create the WebSocket server in-process and bypass its
intended boundary.

The native path therefore needs an explicit capability map. Mapped operations
call real owners with their validators; an unmapped command returns a structured
unsupported-capability error before any side effect. It must never fall back to
localhost WebSocket or REST after a write may have started. The remote backend
can retain its existing arbitrary transport support. Keeping the public
parameter while narrowing native capabilities is **partial compatibility** and
must be visible in discovery and release notes.

The same applies to saved custom tools' `api_get`, `api_post`, `ws_send` ABI.
`call_tool(name, arguments)` is the portable common-engine route. Keep saved
source and report the unsupported operation when an old script uses an
unmapped raw endpoint; do not delete scripts or translate them heuristically.
The existing invocation caps, endpoint normalization, protected-write denials,
snapshot restrictions and structured error behavior still apply. Optional
browser screenshot, HACS and Supervisor features must declare their real
platform prerequisites instead of returning fabricated success.

## Generated automation IDs

Existing automation IDs and their stored bodies retain their identity. A native
creation uses an opaque UUID-derived storage ID; the remote client retains its
historical millisecond-derived ID. Both return the authoritative `unique_id`
and resolve the actual Home Assistant `entity_id` after Core reloads the file.
Clients must treat generated IDs as opaque and reuse returned identifiers.
Neither backend retries an allocation or a write after dispatch. An allocated
ID that already exists is rejected before snapshot capture and mutation.

## Readiness evidence

Entity edit history distinguishes its persisted domains. Historical `entity`
snapshots retain their original state-and-attributes meaning. New
`entity_registry` snapshots preserve Core's full registry entry and use its UUID
as the key, so an `entity_id` rename does not redirect a restore to another
registration. No global snapshot format version changes.

Registry restoration changes the user settings and option domains through the
actual registry API. It compares the current row with the same typed before-image
saved as its safety copy and refuses concurrent edits. It updates an existing
durable identity; it cannot recreate an integration-owned entity registration.
Single and bulk removals capture each existing target. An uncertain Core
removal stops a native bulk call, returning its confirmed progress and explicit
`not_attempted` targets without replaying a removal.

Core can normalize an entity during its own callbacks, for example by adding
a sensor's suggested display precision. Confirmation checks the durable UUID
and explicitly written values instead of rejecting unrelated Core additions.
Effective options and normalization warnings are returned; removing an option
domain does not claim that Core left it absent if Core rematerialized fields.
Changing a proposed value, including `false` to numeric `0`, remains an unknown
outcome rather than a confirmed update.

For state-only entities, `entity_exposure` snapshots preserve the actual Core
exposure preferences. Missing preferences and explicit `false` remain distinct;
restoring absence does not materialize disabled preferences. A registration
created after the safety read causes a conflict instead of redirecting the
write to the new registry entry.

Native restoration of an old `entity` snapshot writes only its state and
attributes through the actual StateMachine, using a fresh context for the
current actor. It does not restore registry settings, saved timestamps, the old
context or a physical device. Like the old REST state write, it may create a
temporary state for a missing entity and trigger automations; an integration
may publish its own state afterward. The response carries this warning.

Native trace diagnostics distinguish an idle script (`off`) from a disabled
automation. They read trace storage settings from the actual Core owner's
configuration and return an unknown value when that configuration is unavailable.
Legacy diagnostics, public arguments and safety annotations remain unchanged.

The native HACS path omits optional `ha_mcp_update` metadata when no authoritative
Core/HACS update information is available. PyPI and Supervisor package versions
do not identify an update for the installed custom component or its fork. No
`latest` version or `update_available=false` is invented. Standalone update
checks retain their existing package source.

Native system health retains the `health_info.data` domain map and each Core
platform's metadata, including `manage_url` and typed dates. It resolves pending
values within an observation window instead of returning only the first
subscription frame. Timed-out probes started by the invocation are cancelled and
drained; shared Core Futures remain running and are reported as pending. The
`component_count` is corrected to the number of actual domains, rather than the
number of keys in the historical event envelope. Independently available Core
sections can still be returned when the system-health integration is absent,
with `baseline_available=false` and an explicit warning. Physical ZHA, Z-Wave and
Thread hardware is not qualified by the Container test fixture.

The native catalog deliberately corrects three safety hints for
`ha_config_list_helpers`: `readOnlyHint=false`, `destructiveHint=true` and
`idempotentHint=false`. Describing a flow-backed helper may initialize a Core
flow that immediately creates an entry. Its argument-specific effect policy
still permits storage listing and storage schema descriptions in read-only mode.
Native clients must treat flow discovery as a possible write. The standalone
catalog and the immutable baseline retain their original annotations; the exact
native exception is recorded in `compatibility.json` and checked separately.

`ha_config_list_dashboard_resources` also uses those conservative native hints.
Core's first storage read may migrate and save dashboard/resource data. Native
policy therefore requires write admission while the collection is unloaded;
loaded collections and YAML mode permit read-only listing. Its original
`openWorldHint=true` and the standalone annotations remain unchanged.

`ha_config_get_dashboard` uses the same three conservative native hints when
describing a custom card or listing card types. Those inspections can initialize
the actual Core resource owner and migrate resources. Its argument policy keeps
ordinary dashboard config/list/search reads and inspection of already loaded
resources available in read-only mode. Its `openWorldHint=true`, public schemas
and standalone annotations remain unchanged.

Dashboard capture is optional and requires an explicitly configured renderer
or the existing Supervisor app. Only that renderer's own credential is sent to
its associated URL; the native component creates no long-lived HA token and
does not reuse HA authority for the renderer. HTTP, cleanup and revocation paths
have unit coverage; this Core Container bench does not exercise an external
renderer. An accessory-image failure preserves primary data and outcomes with
a warning. It does not establish image support in Assist.

`ha_get_integration` has the same conservative native safety hints. Its explicit
options/subentry schema requests initialize real Core flows; password-field
redaction also probes an options flow when options are returned. Such flows can
commit immediately, so these branches require write admission. Native parameter
descriptions state this instead of the historical subentry non-creation promise.
Metadata lists, and persisted options with redaction disabled, remain available
in read-only mode. Mutation admission is rechecked before every actual flow.
An unexpected failure after a Core flow callback is an unknown outcome, never a
retry instruction. Parallel probes drain before the invocation returns.

Native Search captures complete detached Core candidates before matching,
visibility filtering, scoring, totals and pagination in an owned executor.
Automation, script and scene bodies are emitted only when read through Core's
registered config owner; a YAML entity's `unique_id` alone is insufficient.
Loaded YAML remains searchable after secret scrubbing, and YAML dashboard
matches expose metadata without their config or body-derived titles. If the
secret scrub is unavailable, all config surfaces, including their reference
graph leg, are omitted with a partial-result reason. Per-call config budgets
apply only to automation/script/scene; dashboard reads use the configured
individual timeout. Native settings use the HA entry's values. Public schemas
and safety annotations remain unchanged.

Native Blueprint snapshots retain the installed YAML text. Saving or restoring
through Core normalizes that YAML and may remove comments, as the legacy path
does. File preflight checks are optimistic: Core provides no atomic compare and
write against simultaneous external file edits. Avoid editing a blueprint
through another interface while it is being written or restored. Public schemas
and hints remain unchanged; list, get and substitute are admitted as reads.

Native themes read the actual Frontend library and use its original service
validator and actor Context to change defaults. Engine-profile reads require an
already loaded Core UserStore; cold or pending storage is explicitly unavailable
instead of being loaded by a read. Writes validate their payload before any
potentially migrating store initialization, under write admission. Only a token
proven to belong to the effective renderer app may identify its user; HA-MCP never
substitutes the caller or creates a token. Container covers real theme defaults
and absent-renderer refusal; a real Supervisor/Puppet engine account remains
outside this qualification.

Native restart and self-reload responses report a scheduled request, with
`restarted=false` or `reloaded=false`. Before handoff, the pending request
rechecks its actor, credential, unchanged policy and target. Core then owns the
job with a fresh actor Context and no inherited MCP admission context. Shutdown
cancels pending handoffs before draining tools, avoiding a self-reload cycle.
Ordinary reloads wait for Core's result and preserve partial or unknown outcomes;
there is no transport polling. The Container bench exercises actual script,
config-entry and self reloads, but does not establish a full process restart.

Native Energy uses Core's published manager, registered validator and original
source normalizer. An absent or still-loading manager returns a temporary
unavailable result; HA-MCP never initiates Core storage loading or migration
from a read or preview. Public arguments, coercion, hashes and safety hints
remain unchanged. Convenience mutations share their pure duplicate rules with
the remote adapter, but the native owner dispatches once and never rebases or
replays a write after an uncertain result.

Core callback failures or changed data during a dispatched save are unknown
outcomes. If the save is already confirmed but authority, owner or preferences
change during subsequent semantic validation, the response withholds config,
hash and validation data. It explicitly retains `write_completed=true`,
`operation_outcome=confirmed` and `requires_reread=true`; this is a refused or
stale response to a completed write, not permission to repeat it.

Native bulk control retains the public parameters and safety annotations.
Its `device-*` operation IDs belong to the invoking user, client and manager;
they are distinct from Engine invocation IDs. Repeated targets run sequentially,
with their own before-image and confirmation, and a warning explains the overlap.
An after-effect failure preserves partial results and an unknown outcome, while
the queue stops before dispatching remaining targets. Selector previews with
`dry_run=true` are admitted as reads.

Native service groups use Core's original Group owners and retain the public
parameters and safety annotations. Listing reports the owner's actual `all`
mode. Snapshots restore the name, members, icon and mode; a missing mode in an
older snapshot preserves the existing mode, or Core's default during recreation,
with a warning. Restoring a null icon clears it through Core's original setter.
These groups are runtime configuration: YAML and startup configuration remain
authoritative after reload or restart. Snapshot restoration reports that limit
instead of claiming persistent YAML changes.

For each promised tool behavior, compare native and remote execution on a real,
isolated Home Assistant. Exercise normal results, missing resources, future
Core fields, actual validators, invalid inputs that reach Core, read-only and
policy gates, mutate-then-raise, client disconnect, cancellation, reload and
shutdown. Verify one dispatch and an honest unknown outcome where confirmation
cannot be established. No transport fallback may repeat a dispatched mutation.

Migration tests restore old entry/options data and OAuth grants, retain webhook
and LLM API IDs, and test rollback of both code and stored state. HACS packaging
and startup must work without the tools entry, without opening another listener,
creating a server thread or initiating a Core loopback REST/WebSocket connection.
