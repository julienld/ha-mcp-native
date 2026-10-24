# Custom component development

Read this document before changing `custom_components/ha_mcp_tools/` or a
server feature that depends on the component. The HACS component and the
`ha-mcp` server ship through separate installation paths, so compatibility
must hold in both update directions.

## Native and legacy runtimes

The native custom component is the canonical implementation. New server
entries select `runtime_mode=native`; an existing entry without that option
keeps the legacy runtime until the administrator changes it. Runtime selection
is separate from the webhook authentication modes `none`, `ha_auth` and
`legacy`. Legacy compatibility has not been deprecated.

Native MCP runs its FastMCP lifespan on Home Assistant's event loop and serves
ASGI requests through HA's existing HTTP listener. It creates no server thread
or server subprocess, opens no additional HTTP listener and makes no HA
REST/WebSocket loopback calls. Tools use Core owners directly or small domain
services, through the shared Engine. Follow the
[native porting guide](../native-migration/porting.md)
for these bindings; the earlier finite semantic dispatcher remains in use for
the first migrated tools and is not a requirement for new tools.

The legacy embedded runtime retains its separate listener, worker thread and
HA client. External app, Docker and standalone servers also retain their
REST/WebSocket compatibility paths. Keep these transports separate from native
tool logic; an uncertain mutation must never fall back to another transport.

## Version cycle

In the regular release channels, the component shares the server's version and
pins the server build it runs (#2427). `manifest.json`'s `version`, its `ha-mcp==`
requirement and `COMPONENT_VERSION` in `const.py` always equal `pyproject.toml`'s
version.
Never edit them by hand: semantic-release stamps all four in each release
commit (`version_variables` in `pyproject.toml`), and the PR **Component
Version Gate** fails a pull request that moves any of them.

Releases follow from that:

- **Stable:** after **SemVer Release**, the mirror sync snapshots the release
  tag's component and tags the mirror `vX.Y.Z` once PyPI serves
  `ha-mcp==X.Y.Z`. A server-only release is therefore also a component
  release.
- **Development:** after **Publish Dev Channel**, the mirror sync stamps the
  snapshot of the commit it built with the next release number (what
  semantic-release would cut) and pins the exact build it uploaded,
  `ha-mcp-dev==<next>.dev<N>` (the version `scripts/dev_version.sh` gave
  that run, handed over as its `dev-version` artifact), and tags it
  `v<next>-dev.<N>` as a HACS pre-release once PyPI serves that build.

The component never reports a dev suffix: released servers parse the version
segment by segment as integers, so the suffix lives only in the pre-release tag
and the pin.

When a change adds a component service or argument that the server depends
on, raise `MIN_COMPONENT_VERSION` in `src/ha_mcp/tools/tools_filesystem.py`,
in the same PR, to the release that will carry it: the number
`scripts/dev_version.sh` prints without its `.devN` suffix (it computes the
next release from `master` on any branch, so count the PR's own commit type:
a `feat:` on a `master` that only has fixes pending raises the minor).

Every development pre-release cut while that release is pending reports the
same plain number, so the floor cannot tell a pre-release that has the new
service from an earlier one that lacks it. Only an external server (app,
Docker, pip) is affected; the in-process server runs the build its component
release pins. This gap is accepted: pre-release users update the component
from HACS.

Native migration candidates use `scripts/native_release.py` to build a paired
component archive and library wheel from one immutable Git revision, including
the pinned skills submodule. The script stamps the staged artifacts and their
exact dependency URL; it does not change the working tree's release versions.
The HACS candidate wheel has no active dependency edges. Its complete original
dependency list is available through the optional `standalone` extra; the
component manifest lists native host requirements individually. Core can then
skip compatible installed roots instead of re-resolving the wheel's graph with
`uv --upgrade`. The [installation guide](../native-migration/installation.md)
records the provided-by-Core contract, separated extras and remaining checks.

## Compatibility

The embedded server always runs the build its component release pins, but an
external server (app, Docker, standalone) talking to the component through
the tools entry still updates independently, so a new component can run
against an older released server. Do not remove or tighten an existing
service schema without a compatibility shim the previous server can still
satisfy. Remove that shim only after the matching server version becomes the
minimum supported component consumer.
The version gate cannot protect this direction because the older server is the
caller and does not know to demand the new component.

For component behavior changes, follow the
[repository live-testing requirement](../../AGENTS.md#testing-and-verification)
before merge, using the user's choice of the
[live dev environment](../dev-ha-env.md) or another live Home Assistant.
The dev environment runs a fork branch's component and server against a
throwaway Home Assistant on a GitHub runner, so live
validation does not require merging or publishing a release first. Start
with the embedded server and File & YAML Tools entry, then exercise the
other affected topologies described below. Follow the environment guide for
setup, iteration, and cleanup when using it; this manually dispatched
environment is separate from automated CI.

## Dependencies shared with Core

The embedded server installs into Home Assistant's Python environment using
Core's `package_constraints.txt`. The standalone `uv.lock` is not the embedded
installation contract. For libraries shared with HA, keep package requirements as
bounded compatibility ranges that admit supported Core versions. Preserve the
standalone lock's selected versions when widening a range; widen or raise a
floor only with compatibility evidence. Pydantic follows its major-version
boundary; HTTPX is pre-1.0, so its minor-version boundary remains capped.

Dependabot continues updating Pydantic and HTTPX for standalone, Docker, and
app installations. Their standalone lock versions may advance independently
of Core's pins while the package requirements still admit those pins. Review
requirement changes against supported Core versions; an incompatible floor
must fail alignment checks rather than silently strand embedded users.
Renovate advances the Core E2E image, not these package requirements. Never
fix an embedded resolver failure by dropping HA's constraints or eagerly
upgrading its shared packages.

Core's normal installer uses `uv --upgrade` for a missing requirement. Broad
ranges alone do not preserve the installed environment, and a local pip install
under the constraints is not an equivalent qualification. Julien authorized
compatible shared-package upgrades on 23 October 2026. Keep Core's installer
and constraints, record all changes and reject removals, downgrades, incompatible
release changes, protected Core/SDK replacements and new dependency conflicts.
The [installation policy](../native-migration/installation.md#compatible-shared-package-upgrades)
defines the qualification contract. Check every preinstalled distribution after
setup/restart, also compare consecutive observations, and exercise a minimal
Core environment with older permitted transitive packages. These are CI gates,
not a production transaction or rollback mechanism. Keep Pydantic declared:
its constraint is not proof of presence in Core's base installation. Do not add
a dependency on `homeassistant` itself, which would re-resolve Core.

Fast Checks verifies direct dependency alignment against both the current
Core image and the `hacs.json` minimum, without adding E2E lanes. Existing
legacy current-Core embedded E2E on x64/ARM64 covers transitive installation and
preinstall/runtime replacement of HA-governed packages; nightly beta E2E
provides advance notice of upcoming Core regressions.

The minimum-version check does not prove transitive installability or runtime
behavior there. These checks also do not cover every intervening release,
every installed third-party integration, or future API compatibility.
Keep the HACS minimum honest; passing current-Core E2E alone does not justify
raising a dependency floor past versions older supported Core releases need.

Native FastMCP and MCP SDKs live under `ha_mcp._vendor`; they do not replace
Core's shared `mcp` package. The native wheel also uses a private Rich copy.
The published manifest does not require the shared MCP SDK, and legacy Assist
uses the private SDK as well. The candidate and public-install lanes compare all
installed distributions and new `pip check` conflicts, including after Core
processes every manifest requirement. A minimal Core lane distinguishes base
providers from packages preloaded only in the Container image. These checks do
not cover every optional integration's dependency combination.

The native development line preserves the upstream Core **2026.8.0** minimum.
Its qualification matrix covers three consecutive monthly Core trains on
amd64 and aarch64, using each image's own Python rather than an independent
Python matrix. The exact minimum is exercised, not replaced by its latest patch.
Creation, selection in options and startup check the native
floor before loading modern owners, including for an imported entry or a Core
downgrade. They refuse an unsupported native selection without changing its
runtime or webhook automatically. The source/legacy channel's HACS installation
floor remains **2026.8.0**, retained for legacy and shared-component compatibility.
The generated native distribution snapshot derives its HACS minimum from the
runtime floor. Historical public dev3 and the earlier qualification remain tied
to **2026.10.2**; they do not establish this new minimum's runtime support.
Record the matrix's results before claiming the development candidate qualified.
Fail qualification on Core deprecations and blocking calls attributed to our
component on the version actually running; an old API supported by an older
Core may remain in its tested compatibility branch. Retain other integrations'
diagnostics without treating them as ours. Do not globally ignore warnings.
Keep the matrix pinned and update it deliberately with the compatibility floor;
do not silently drop the advertised minimum as the current month advances.
Native runtime support and the component installation floor are distinct claims. Candidate
qualification records belong in
[native migration evidence](../native-migration/README.md).

## Two entries, one command surface

The integration has two config-entry types:

- **HA-MCP Server** runs the server in-process and exposes it through a Home
  Assistant webhook.
- **File & YAML Tools** registers privileged filesystem and YAML services.

Both entries register the shared `ha_mcp_tools/*` WebSocket command surface.
An external server therefore reaches in-process capabilities through the tools
entry alone. Every shared handler must read live Home Assistant state rather
than tools-entry `hass.data`, or a server-entry-only installation breaks.
Native tools call the underlying owners without making WebSocket requests to
this surface.

`async_register_command` is idempotent, and Home Assistant provides no
unregister operation. Commands remain registered after an entry unload until
Home Assistant restarts. Do not attempt to tear them down; do clear that
entry's `hass.data` caches so the next setup reads storage again.

Privileged filesystem and YAML services remain tools-entry-only. The
`ha_mcp_tools/info` command answers for either entry, so the server gates
those services on the additive `tools_services` handshake field—not the
general capability list.
Native File/YAML tools require the live tools entry and use the same local file
owners, permissions and opt-ins as those services; direct calls do not bypass
the tools-entry boundary.

When changing shared command or service behavior:

- Update the component producer, server consumer, capability gate, and legacy
  fallback in the same pull request.
- Serve shared commands identically from both entry types.
- Gate privileged or beta behavior on an explicit tools-entry signal.
- Exercise both topologies, including the no-tools lanes described in
  [`tests/AGENTS.md`](../../tests/AGENTS.md#no-tools-lanes-e2e_no_tools_entry1-2292).

## Embedded server security

The native Engine accepts only identified, active human Home Assistant
administrators. In `ha_auth` mode, Core validates the caller's Bearer and that
caller's user/client identity becomes the execution context. Secret-URL and
legacy-OAuth modes instead use the entry's selected administrator owner, whose
active/admin status is checked on calls. `Context(user_id=...)` supplies
attribution; the authorization checks remain explicit. The native runtime
neither creates nor uses an internal administrator LLAT for Core operations.

MCP, Assist and nested custom-code `call_tool` enter the same Engine. Actor,
credential, enabled-tool and read-only checks apply there; native mutation
owners recheck admission at dispatch after their preparatory awaits. Assist
exposure adds a live per-tool guard, including child calls. Approval state is
bound to the caller/client and arguments. The transport-neutral
[`tool_api` projection](../../src/ha_mcp/tool_api/llm_projection.py) keeps rich
descriptions, schemas and results separate from FastMCP and Core; the
[`Core adapter`](../../custom_components/ha_mcp_tools/llm_projection.py) supplies
schema conversion and full/search presentation. Exported API IDs and exposure
settings remain stable, and anonymous Assist calls receive no owner fallback.

Core remains the `ha_auth` OAuth authority. Authorization redirects into Core;
translated code/refresh forms call its registered TokenView's grant methods with
the real incoming request. Revocation uses Core's token registry. The native
path preserves the existing DCR/CIMD translation and signed refresh envelope
without an HTTP relay, fabricated request or copied grant validator.

The legacy embedded runtime makes upstream calls with an administrator's LLAT
(`server_credentials.py`): one the user pastes, or one issued at setup from the
login of an account the user chose, which the component revokes on a switch or
removal. The component never creates an account: entries from older releases
keep their provisioned pair while it works,
and a missing or refused credential files the fixable `server_token_needed`
repair instead. The token is passed in memory, not through the Home Assistant
process environment; disabling the entry stops the server. Removing the entry
deletes only an account an older release created, never the user's.
Retaining those credential fields for rollback does not make them native
execution credentials. Existing entries and HA-hosted webhook/OAuth URLs are
preserved; a legacy direct-port URL has no native listener equivalent. Changing
runtime does not automatically enable a disabled webhook.

The settings panel is served through Home Assistant. Browser
access uses a short-lived HttpOnly session cookie issued to an authenticated
administrator. Every request revalidates that the session still maps to an
active administrator. Native requests call the settings handlers directly;
legacy requests proxy to the separate listener. The panel never exposes that
listener's secret path or puts session credentials in URLs.

The full threat model and route-ownership rules are in
[`SECURITY.md`](../../SECURITY.md).
