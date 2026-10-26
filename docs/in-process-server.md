# Run the MCP server inside Home Assistant

The **HA-MCP Custom Component** (`ha_mcp_tools`) provides native MCP and an
LLM API for Assist. Both use the same tool definitions and policy checks.
The native runtime runs on Home Assistant's event loop and HTTP listener,
without a separate server port or an internal Home Assistant access token.

Existing entries keep their selected runtime, authentication and connection
identifiers. Updating the component does not automatically switch a Legacy
entry to Native or migrate an external app, Docker or Python installation.

## Who it's for

The component works with Home Assistant OS, Supervised, Container and Core.
It can serve MCP clients, conversation agents using HA's LLM API, or both.
An external ha-mcp server remains a supported alternative. Configure each
client to use the intended server rather than adding duplicate connections.

## How it works

Home Assistant installs the library paired with the selected component
release, under its normal dependency constraints. Native tools call Core's
services, registries and other owners directly. MCP requests arrive through
Home Assistant's HTTP server; Assist calls enter the shared Engine directly.

Blocking file work uses Home Assistant's executor. Removing the separate
server thread does not remove ordinary background tasks or make blocking work
safe to execute on the event loop.

Legacy mode retains the older embedded server and its REST/WebSocket client.
Its port, credentials and advanced package override are compatibility settings,
not requirements of Native mode.

## Requirements

Each release declares its required Home Assistant version in HACS/Marketplace.
The supported window is the current stable monthly Core release and the
previous one. A new monthly release advances the CI window; a new ha-mcp
release must declare the corresponding minimum before publication.
An already-installed ha-mcp version does not stop working merely because
another month begins.

The initial native adoption floor is Core 2026.10.0. Use a component release
compatible with your installed Core; update Core before installing a release
that requires a newer version.

## Setup

1. Install **HA-MCP Custom Component** using HACS, or Marketplace on Core versions
   that provide it. Add `homeassistant-ai/ha-mcp` as an integration repository
   if it is not listed. An existing installation from the
   `homeassistant-ai/ha-mcp-integration` mirror can keep that repository.
2. Restart Home Assistant after replacing an already-loaded component.
3. Open **Settings → Devices & Services → Add Integration**, find
   **HA-MCP Custom Component**, and choose **HA-MCP Server**.
4. Select an active administrator as the owner. Native setup does not ask you
   to create an administrator account or mint an internal access token.
5. Enable the access you need: MCP through Home Assistant, LLM API export to
   Assist, or both. New entries leave these choices disabled until selected.
6. For MCP, copy the exact connection URL from the entry's **Configure** screen.
   For Assist, select the exported API in your conversation agent's settings.

For manual installation, extract the selected release's `ha_mcp_tools.zip`
into `config/custom_components/ha_mcp_tools/`. Preserve its manifest and paired
library requirement. Do not mix component files and library wheels from
different releases.

## Connect URLs

Native uses Home Assistant's existing address, locally or through your
configured remote access. Use the URL shown in Configure; treat secret URLs
as credentials and keep them out of logs and issue reports.

A Legacy direct-port connection has no corresponding native listener.
Switching such an entry to Native requires updating the client's URL.
HA-hosted webhook identifiers and existing authentication identities are
preserved when the same entry changes runtime.

## Chat with the toolset from Home Assistant (conversation agents / voice)

Enable the conversation-agent LLM API export in the server entry, then select
the HA-MCP API in a conversation integration that supports Home Assistant LLM
APIs. It does not require the MCP webhook to be enabled.

### Exposure modes

- **Tool search** exposes pinned tools plus search and dispatch tools.
- **Full catalog** exposes every permitted tool directly.
- **Both** makes both API presentations available for conversation agents.

### Per-tool exposure

The HA-MCP settings panel controls which tools are exposed through the LLM API.
Global enablement, read-only mode, approvals and per-tool exposure remain
effective when a tool is called indirectly. Existing exposure choices and API
identifiers survive upgrades.

Native Assist calls require an identified, active administrator. A voice
satellite that supplies no usable user identity is refused; it does not inherit
the configured administrator's authority. Core's LLM interface does not carry
every rich MCP result or continuation. Image-only calls are refused before
execution; mixed results retain their usable data and report the image limitation.

The reverse bridge, importing selected Core LLM APIs into MCP, is a separate
explicit option. Enabling one direction does not implicitly enable the other.

## Settings panel ("HA-MCP" in the sidebar)

The sidebar panel uses Home Assistant authentication. Every request checks the
active administrator session. Native serves settings locally; Legacy proxies
its embedded server. The connection secret is not placed in panel URLs.

## Independent from the app

The component and the Home Assistant MCP Server app are independent
installations. An ordinary update preserves the installation method.
The component's File & YAML entry can also support an external server without
starting an embedded server.

## Options

Configure the server entry to choose its runtime, authentication, owner, MCP
access and LLM bridges. Preserve the existing entry when upgrading; deleting
and recreating it changes identifiers that clients may have saved.

### Server updates

`ha_manage_ha_mcp_updates(action="check")` identifies the running installation
and its update capability. `action="guide"` explains its installation manager
and upgrade steps. `action="update"` applies an ordinary update where a
qualified manager is available.

The tool preserves the current runtime, authentication and repository origin.
Detected breaking changes, or insufficient release information, produce a plan
before code is changed. An ordinary restart requirement is distinguished from
a change to the connection contract.

Installed files and running code are different states. A component update
reports `pending_restart` until a Core restart activates the new code.
After restarting, check the version again. A lost response is not an invitation
to repeat an installation: the update tool records dispatched attempts and
inspects the installed version before allowing further work.

App, Docker and Python installations receive the path appropriate to their
actual owner. When the running process cannot safely control that owner, the
tool provides instructions instead of pretending to update itself.

### Upgrading from component 2.x

Keep the current entry. Migration preserves identifiers, explicit LLM choices
and historical effective export behavior. The earlier package-override cleanup
is applied only to entries that have not already passed it.

Existing Legacy credentials remain available for rollback. Native does not
use them for Core operations. A native-only entry needs an explicitly
configured valid credential before it can run Legacy mode.

To roll back code, install a compatible previous component release and its
paired library, then restart Core. Code rollback does not reverse changes made
to entities or configuration; restore the appropriate data backup separately.

### Local-only mode

An Assist-only installation can leave MCP access disabled. For MCP clients on
the LAN, use Home Assistant's local address. Whether Home Assistant itself is
reachable remotely is controlled by your HA remote-access configuration.

### Turning access on and off

The optional **Access** switch closes MCP and the LLM API while preserving the
entry. It is disabled as an entity by default and is not exposed to Assist by
default. **Turn access off after a restart** starts the entry with access off
on the next Core start; otherwise its saved state is retained.

The switch does not revoke a Home Assistant credential a client already holds.
Revoke that credential through Home Assistant when necessary.

### Rotating your connect URL

Use **Regenerate connect secrets now** only when you intend to change client
connection details. Update clients with the resulting URL. Do not delete and
recreate the entry as a routine update step.

## Security

- **Home Assistant sign-in (`ha_auth`)** uses the authenticated administrator's
  identity. The component retains upstream's DCR compatibility for clients that
  need registration; Core's CIMD support does not replace DCR.
- **Secret URL (`none`)** authenticates possession of the URL and executes as the
  selected administrator owner. Anonymous OAuth callbacks remain constrained by
  the configured allowlist unless an administrator explicitly changes that option.
- **Legacy OAuth (`legacy`)** retains its client ID/secret compatibility and
  executes native operations as the selected administrator owner.

Native checks active-user status and permissions at admission and again before
mutations after awaited preparation. A user context supplies attribution; it
does not replace authorization. A lost mutation response is reported honestly
and is never retried through another transport.

Legacy mode retains its administrator credential and separate listener.
Changing runtime does not silently change authentication or enable a disabled
webhook. See [SECURITY.md](../SECURITY.md) for the complete threat model.

## Relationship to the File & YAML services entry

Add **HA-MCP File & YAML Tools** only when you need the opt-in privileged file
and YAML tools. Native calls preserve that entry boundary and its permissions.
The shared registry and metadata commands are available from either entry;
they do not grant the privileged filesystem services.

## First start takes a little longer

Core may need to install the release's declared dependencies on the first
load. It uses its ordinary installer and constraints. Do not replace Core
packages manually to force an incompatible component to load.

## Troubleshooting

Check **Settings → System → Repairs** and the integration diagnostics for
version, dependency, owner or startup errors. The diagnostics redact stored
credentials. Do not post connection URLs, tokens or raw config-entry storage.

If Native refuses an unsupported Core version, install a compatible component
release or update Core. A silent switch to Legacy is not performed.
For an uncertain update, inspect the update tool's receipt and installed
version before trying again.
