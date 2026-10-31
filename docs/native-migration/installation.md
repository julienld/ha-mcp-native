# Native packaging, delivery and rollback

This maintainer guide defines the paired release and qualification contract.
For installation and runtime settings, use the
[in-process server guide](../in-process-server.md). Legacy compatibility remains
supported; an upgrade must preserve existing entries and their saved choices.

## Paired artifacts and identity

`scripts/native_release.py` exports one committed source revision and its pinned
submodules, then builds a native `ha-mcp` wheel and `ha_mcp_tools.zip`. The
component manifest selects that exact wheel through a public GitHub release URL
with a SHA-256 fragment. Both `hacs.json` files select the ZIP release asset.
Home Assistant installs the manifest requirements through its normal requirement
manager and package installer, with Core's own constraints and installer version.

Keep these identities distinct:

- The wheel and integration manifest carry the complete release version,
  including `X.Y.Z.devN` for development releases.
- `COMPONENT_VERSION` remains numeric `X.Y.Z` for released clients that parse
  the legacy protocol version as integer segments. It is not the build identity.
- `native-build.json.revision` identifies the original code SHA, with submodule
  gitlinks and artifact digests. A dev tag's `release_revision` identifies its
  full-source stamp commit, whose single parent is that code SHA. Only the
  component manifest and protocol constant differ, with exact bytes verified.
- A mirror distribution commit is separate from either source identity; its
  recorded provenance binds it to the upstream artifacts.

The ZIP's `native-wheel.json` indexes the paired wheel's package and distribution
metadata bytes, excluding installer-owned receipts and the rewritten `RECORD`.
It can establish installed payload equality when uv omits a PEP 610 archive hash.
A contradictory receipt is still refused. Digests and JSON provenance are not
publisher signatures, and payload equality is not proof of every object already
loaded in memory. Restart Core after changing the paired release.

## Dependency profiles

The HACS native wheel has **no active dependency edges**. Its original dependency
graph remains available in the optional `standalone` extra; native host packages
are declared individually in the component manifest. Core skips satisfied
individual roots, whereas installing a URL wheel with active dependencies through
its normal `uv --upgrade` path can re-resolve the whole graph. Broad ranges alone
do not prevent shared-package replacement. Do not replace this path with an
unconstrained installer or a plain pip build-time install.

The profile relies on packages supplied by Core's base installation, while keeping
required non-base packages, including Pydantic, explicitly declared. A constraint
does not prove that a package is installed. Separated extras such as `cachetools`,
`socksio` and `email-validator` preserve the required capabilities without requesting
unused provider or persistence graphs. Native FastMCP/MCP and Rich use private
copies under `ha_mcp._vendor`; legacy Assist also uses the private MCP SDK. They
must not replace Core's shared `mcp` distribution. Keep optional integration
imports behind loaded-owner checks; adding them to `after_dependencies` can cause
Core to install their requirements even when they are not configured.

The ordinary PyPI wheel and sdist retain the complete default dependency graph:
`ha-mcp` for stable, `ha-mcp-dev` for development. The native URL wheel remains
`ha-mcp` in both channels. Never upload the dependency-free native wheel as the
ordinary PyPI distribution.

### Compatible shared-package upgrades

Qualification uses Core's normal installer and constraints, with dependency
observations before installation, after setup and after a complete Core restart.
The minimal Core lane starts with Core and the exact frontend declared by that
Core image, then seeds an older permitted certifi. It does not inherit the image's
complete integration dependencies. It observes each manifest root and declared
integration dependency. Check every installed distribution, compare each observation to the
baseline and its predecessor, and retain existing conflicts separately from new
ones.

Compatible shared upgrades may pass. Reject removals, downgrades, epoch or major
changes, pre-1.0 minor changes, stable-to-prerelease transitions, new dependency
conflicts and violations of applicable Core constraints. Preinstalled Core,
shared MCP, Pydantic, HTTPX and Cryptography must retain their versions. Newly
added distributions must satisfy the same constraints and conflict checks.
The initial dependency-free wheel installation must change no preinstalled
package; allowed upgrades belong to subsequent manifest processing by Core.

Exercise both the full Container environment and minimal Core with older
permitted transitive packages. Verify actual runtime imports, protocol and state
operations as well as installed versions. These checks do not establish
compatibility with every third-party integration and do not provide a production
transaction or automatic rollback. The fixtures and policy live under
[`tests/native/`](../../tests/native/); public delivery evidence is described in
[published qualification](../../tests/native/PUBLISHED.md).

## Prepare without publishing

Use an immutable source SHA, the exact version selected by the release pipeline
and an unused output directory. For example, replace the coordinates below with
the reviewed source and version:

```powershell
uv run -s scripts/native_release.py --revision SOURCE_SHA --version X.Y.Z --tag vX.Y.Z --github homeassistant-ai/ha-mcp --source-github homeassistant-ai/ha-mcp --with-standalone --prepare-hacs --output dist/native-review
```

`--with-standalone` adds the complete wheel and sdist in `standalone/`.
`--prepare-hacs` adds a reviewable `hacs-repository/` distribution tree; it does
not replace the full source history or create a commit or tag. Review the ZIP,
wheel, index, provenance and staged tree together. Uncommitted edits are not
exported. An incomplete output directory is not a valid release.

The default asset repository is `homeassistant-ai/ha-mcp`. For a fork, supply
`--github OWNER/REPOSITORY`; use `--source-github` when source links differ.
Qualification must independently select that same asset repository with
`--repository` (and Docker's `RELEASE_REPOSITORY` build argument). Never derive
the expected origin from downloaded metadata. Existing fork releases remain
verifiable through these explicit options.

The reusable/manual `native-distribution.yml` accepts version, tag and source
revision, builds both profiles and uploads an Actions artifact. It has only
`contents: read` permission. Staging performs no tag, release, PyPI or mirror
publication and is not authorization to publish.

## Release delivery

The existing release workflows use `scripts/native_release_delivery.py`:

- **Stable:** SemVer Release builds paired native assets from the stamped
  `vX.Y.Z` source commit, uploads to a draft and reads back every asset before
  publication. Release Publish retains ordinary PyPI/container delivery from
  the same tag.
- **Development:** Publish Dev Channel uses the exact `scripts/dev_version.sh`
  version for standalone and native artifacts. Its `vX.Y.Z.devN` tag retains
  the full source tree with the minimal two-file stamp described above. HACS
  reads the tag's source manifest before selecting the ZIP, so source discovery
  and installed metadata must agree. Master's declarations remain unchanged.
- **Mirror:** the integration mirror verifies the upstream ZIP, wheel and
  provenance, extracts the ZIP unchanged and records the metadata. Its
  `vX.Y.Z` / `vX.Y.Z-dev.N` release tags deliver the same bytes, retaining the
  original upstream wheel URL and digest. The mirror does not rebuild them.

Never overwrite an existing tag, version or asset. The delivery helper verifies
existing identities and bytes before reusing them; changed or incomplete
published releases fail. Recover a partial draft using the original
`native-release-VERSION-RUN-ATTEMPT` Actions artifact and the helper's `publish`
command with the exact coordinates and notes. It verifies existing assets before
adding missing ones. Do not rebuild under that identity, use `--clobber`, or
delete and recreate the release. Keep historical tags and artifacts available
for pinned installs and rollback. The [component release guide](../agents/custom-component.md#version-cycle)
owns the detailed pipeline contract.

## Qualification and rollback

New releases start at Core **2026.10.0** for both component runtimes. The release
window selects the current released stable monthly generation and its predecessor,
bounded by that minimum. Source floors, HACS metadata and the resolved window
must agree before publication; dates and unreleased tags do not advance it. See
[compatibility](compatibility.md) for the amd64/aarch64 contract and its limits.

Require evidence tied to the exact source, repository, tag, artifact hashes,
Core/Python versions and architecture. Check the actual runtime, Core diagnostics,
dependency observations, full restart, native/legacy switching and rollback.
Local candidates rewrite only the disposable image's unavailable wheel URL;
they cannot prove public download or HACS version selection. The
[published-asset lane](../../tests/native/PUBLISHED.md) exercises the unchanged
manifest URL. Authenticated HACS upgrades and HAOS hardware need their own
completed runs before those claims are made.

`haos-native.yml` is a targeted qualification job with an editable exact Core
pin, initially `2026.10.4`; select `2026.10.0` to exercise the initial minimum.
The source admission guard still applies. `native-continuity.yml` retains the
immutable application baseline `2dc504fd97d8416f87b6ea7f2fd8ce0332615956` and one
common Core `2026.10.2` for before/after behavior. That bench is distinct from the
compatibility matrix. Both retain manual/qualification-branch triggers without
a schedule. The container fixture resolves `HA_TEST_IMAGE`, then
`HA_IMAGE_GHCR`, then its default; report the actual image, not a fixture pin
from another lane.

Before an upgrade, take a recoverable backup. Preserve config-entry, webhook and
LLM API identifiers, saved authentication choices, opt-in flags and legacy
entries; never delete/recreate entries to update code. Native is the new-entry
default, while existing entries without `runtime_mode` retain legacy. Runtime
switches must keep capability and credential checks; no automatic transport or
authentication fallback is allowed.

To roll back code, select a compatible previous published version with its
paired ZIP/wheel and restart Core. Before downgrading Core, select an artifact
whose own floor admits it; legacy mode does not bypass the new component's
minimum. Code rollback does not restore entities, configuration, credentials or
shared packages changed by installation. Restore data or the Core environment
through the platform's supported backup/recovery procedure when needed. Keep
credential-bearing backups and receipts out of public qualification artifacts.
