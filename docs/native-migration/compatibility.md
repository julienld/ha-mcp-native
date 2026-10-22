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

## Readiness evidence

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
