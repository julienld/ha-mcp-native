# Native candidate installation and rollback

The public fork is
[`julienld/ha-mcp-native`](https://github.com/julienld/ha-mcp-native).
Choose a reviewed version from its [published releases](https://github.com/julienld/ha-mcp-native/releases)
before installation. The preparation commands below create local artifacts;
they do not publish an installable release. Publication is a separate maintainer
operation after artifact review and qualification. Legacy compatibility has not
been deprecated. Consult [published-candidate status](../../tests/native/PUBLISHED.md): dev2 failed
the shared-dependency installation check and must not be treated as qualified.

## What HACS must install

The native release builder produces a library wheel and `ha_mcp_tools.zip` from
one committed source revision and its pinned skills submodule. Home Assistant
installs the wheel named by the ZIP's integration manifest. The wheel URL uses
the public GitHub release asset route, with a SHA-256 fragment; it contains no
credentials or expiring redirect URL.

HACS needs `zip_release: true` and `filename: ha_mcp_tools.zip` in the selected
version's root `hacs.json` to download that asset. Without them it installs the
repository sources. See the [HACS archive settings](https://www.hacs.dev/docs/publish/start/#hacsjson)
and the [integration release requirements](https://hacs.dev/docs/publish/integration/#github-releases-optional).

The source repository's `hacs.json` and release versions stay unchanged. Its
upstream mirror workflow copies those files and publishes source snapshots;
changing them globally to require a ZIP would break that separate channel.
Instead, `--prepare-hacs` creates a **distribution tree** containing:

- `hacs.json`: ZIP selection, hidden default-branch choice, and the native Core
  minimum read from the candidate's source;
- `custom_components/ha_mcp_tools/`: exactly the ZIP's component files;
- a short README, the license, and `native-source.json` linking the source SHA,
  library requirement and ZIP digest.

This tree is intended for a separate commit and release tag in the **same fork**.
It has no `.github/workflows` and does not change the default branch. HACS reads
the manifest for the selected version, as implemented in
[HACS 2.0.5](https://github.com/hacs/integration/blob/c0dfd8b44297c3673c21973e2539375a53687a9c/custom_components/hacs/repositories/base.py#L715).

## Versions and build evidence

For a candidate such as `9.0.0.dev123`:

| Surface | Value | Purpose |
| --- | --- | --- |
| Library wheel | `9.0.0.dev123` | Exact installed Python distribution |
| Integration manifest | `9.0.0.dev123` | Unique candidate version in Home Assistant |
| Distribution tag | `9.0.0.dev123` (or `v9.0.0.dev123`) | HACS release selection |
| Legacy protocol `COMPONENT_VERSION` | `9.0.0` | Numeric compatibility floor for already-released clients |

The last value is **not the complete candidate identity**. Historical clients
parse its dot-separated segments as integers. Keeping that wire value numeric
preserves their compatibility check while the manifest and library identify each
candidate. `native-build.json` records both versions, the source SHA, submodule
SHAs, artifact hashes and distribution-tree file hashes.

The ZIP also carries `native-wheel.json`, a versioned index of the paired wheel's
package and distribution-metadata bytes. It excludes the installed `RECORD`, which
uv rewrites, and rejects installer-owned receipts in the source wheel. The index
is bounded to 10,000 files, 128 MiB of payload and 4 MiB of JSON. It lets the
runtime verify the installed payload when uv's PEP 610 receipt omits the hash;
an explicitly contradictory receipt is still a refusal. This is artifact
identity evidence, not a publisher signature.

PEP 440 ordering gives `9.0.0.dev9 < 9.0.0.dev10 < 9.0.0`. This comparison was
also exercised with AwesomeVersion 25.8.0, the version library used by HACS.
Use a new, increasing development number and a new tag for every public
candidate. Never replace an existing candidate's assets or reuse its version.

The asset URL alone does not make a release immutable. GitHub's
[immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases)
lock its tag and assets after publication and provide release attestations.
The local JSON evidence is a digest record, not a signature. HACS does not verify
that JSON or a release attestation during ordinary ZIP installation. The wheel's
[SHA-256 URL fragment](https://packaging.python.org/en/latest/specifications/version-specifiers/#direct-references)
allows the Python installer to verify its downloaded bytes.

## Dependency profile and the dev2 finding

Core's [requirement manager](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/requirements.py)
skips individually satisfied requirements. When installation is needed, its
[package installer](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/util/package.py)
uses `uv pip install --upgrade` under Core's constraints. A URL wheel with active
`Requires-Dist` entries therefore re-resolves its dependency graph, including
already-compatible packages. Installing the same wheel with ordinary pip in a
Docker build did not exercise that behavior.

The [dev2 public-install run](https://github.com/julienld/ha-mcp-native/actions/runs/39866932217)
observed these replacements in Core 2026.10.2:

| Package | Before → after | Relevant edge in the old wheel graph |
| --- | --- | --- |
| authlib | 1.7.2 → 1.8.0 | Unused native OIDC/provider dependency |
| joserfc | 1.7.5 → 1.7.7 | Direct declaration and Authlib |
| click | 8.1.8 → 8.3.1 | Uvicorn |
| cachetools | 7.2.2 → 7.2.4 | `py-key-value-aio[memory]` |
| certifi | 2026.7.22 → 2026.10.19 | HTTPX |
| pycparser | 3.11 → 3.12 | Cryptography → CFFI |
| pydantic-settings | 2.16.0 → 2.16.1 | Direct declaration |
| platformdirs | 4.13.3 → 4.13.5 | Direct declaration |
| starlette | 1.8.0 → 1.9.0 | Direct declaration and SSE-Starlette |

Authlib 1.8 violated PySenz's upper bound; Click 8.3 violated gTTS and PyOmie's
upper bounds. Broader compatibility ranges alone did not prevent these changes.

The revised candidate wheel has **no active `Requires-Dist` entries**. All
original project dependencies remain available through its `standalone` extra.
The component manifest declares the required host packages individually, so Core
can retain each compatible installed version. The profile:

- relies on the [Core base contract](https://github.com/home-assistant/core/blob/2026.10.2/pyproject.toml)
  for HTTPX/AnyIO, Cryptography, PyJWT, PyYAML, typing extensions, packaging and
  psutil, rather than redeclaring their graphs through the URL wheel;
- keeps Pydantic, Pydantic Settings, Starlette/SSE, HTTPX2, Uvicorn and other
  required non-base packages in the component requirements. A pin in Core's
  constraints file does not itself prove that a package is installed;
- declares `cachetools`, `socksio` and `email-validator` independently, retaining
  their runtime capabilities without requesting the parent extras. The native
  session store uses memory, not filetree/keyring persistence;
- leaves unused OIDC/provider Authlib/Joserfc and OpenAPI-provider dependencies
  in `standalone`, and uses the existing private MCP SDK for legacy Assist too.
  It does not install Core's shared `mcp` package.

No additional vendor tree or custom installer is introduced by this profile.
Missing packages still have their own transitive dependencies, so the profile
must pass both the full Container environment and a fresh Core-base environment
seeded with the older image's certifi. Both use Core's real installer and compare
**all** distributions: the full lane after setup/restart, the minimal lane after
each installation and its SDK/state/transport probes. Compatibility ranges admit
future Core versions; they are not an assurance that every future dependency
graph is unchanged. A changed shared dependency remains a qualification failure.

This correction prevents new resolver churn; it does not undo packages already
changed by dev2. Restore or recreate an affected Core environment through the
installation platform's supported procedure, preserving its configuration.

## Prepare a reviewable candidate

From a checkout of the exact source commit that passed native CI, with its
submodule available:

```powershell
uv run -s scripts/native_release.py --version 9.0.0.dev123 --tag 9.0.0.dev123 --prepare-hacs --output dist/native-9.0.0.dev123
```

Choose the actual unused version before running this command. The destination
must not exist. An interrupted build is not a valid candidate; use a new output
directory and inspect the complete evidence before continuing. The builder uses
tracked `HEAD`, ignores uncommitted source edits, performs no installation in the
working environment and creates no commit, tag, remote branch or release.

For another GitHub repository, pass `--github OWNER/REPOSITORY` to the builder
and the same trusted `--repository OWNER/REPOSITORY` to release qualification.
The workflow passes its own `GITHUB_REPOSITORY` through the builder, public
fetch, image verification and minimal Core worker. The old fork remains the
default for manual preview commands. Never derive the expected repository
from downloaded metadata: it must match the independently selected repository,
source revision, tag, wheel URL and digests before execution.

Review the wheel, component ZIP, `native-build.json` and `hacs-repository/`
together. Verify that the snapshot's component files match the archive, that its
manifest names that wheel and hash, and that the source commit links resolve.
Use the same artifacts for the qualification run and eventual upload.

After artifact review and qualification, the minimal publication sequence is:

1. Initialize a separate local Git checkout from `hacs-repository/`, without
   adding workflows or copying the development checkout's `.git` directory.
   Commit the tree with the required session trailers. Its commit is the
   **distribution commit**; `native-source.json` retains the distinct source SHA.
2. Push only a dedicated distribution branch and the reviewed version tag to
   `julienld/ha-mcp-native`. Do not push to `master`, move the default branch,
   retag an existing release, or dispatch an upstream publishing workflow.
3. Create a GitHub **draft pre-release**,
   attach the exact wheel, `ha_mcp_tools.zip` and `native-build.json`, and verify
   their uploaded digests. Publish the reviewed artifacts. If repository immutable
   releases are enabled, publication locks the tag and assets; that setting is
   not assumed or required by this qualification. Draft assets
   are not anonymously installable by Home Assistant.
4. Verify the published release and assets with `gh release verify` and
   `gh release verify-asset` when attestations are available. Then perform the
   HACS installation and rollback checks below.

The workflow audit found only `Native migration` active in the fork. In the
source tree, SemVer Release is scheduled/manual; Release Publish follows that
workflow; dev publication and mirror sync follow `master`. The mirror's separate
`release-on-tag.yml` is a template outside `.github/workflows` and is omitted
from the generated tree. Recheck the fork's enabled workflows and their triggers
immediately before a future push/publication; this audit does not authorize
running any publishing workflow.

## Install a published candidate

The candidate requires **Home Assistant 2026.10.2 or newer**. The regular source
channel's older HACS minimum remains unchanged for legacy compatibility. Native
Core admission enforces its own minimum even if an archive is copied manually.

1. Take a recoverable Home Assistant backup and record the currently installed
   integration/library version and configuration. Keep credential-bearing
   backups private.
2. Add `https://github.com/julienld/ha-mcp-native` as a HACS custom repository of
   type Integration. Enable its pre-release switch and explicitly select the
   published version tag. Before a regular release exists, registration may
   initially inspect `master`; downloading that branch does not install this
   candidate. Avoid having two HACS repositories manage the same
   `custom_components/ha_mcp_tools` directory.
3. Download the selected candidate and restart **Home Assistant**, so the new
   component and library share one generation. An entry reload alone cannot
   replace already-imported Python/SDK classes.
4. Configure the existing HA-MCP Server entry. New entries default to native;
   existing entries without `runtime_mode` retain legacy until an explicit
   change. Keep the File & YAML Tools entry when those opt-in capabilities are
   needed. With the webhook off, the administrator sidebar remains available; Assist requires its separate opt-in.
5. Verify the selected HACS tag, manifest and installed library version, the
   configured webhook/Assist access, and the native Core log. The previous
   legacy-only listener URL needs legacy mode or an explicitly updated HA URL.

## Credentials and rollback

The two LLM bridges are independent and **off by default**, in both initial setup
and Configure. `enable_llm_api=true` offers HA-MCP tools as an API that an Assist
agent can select. `enable_native_llm_import=true` lets MCP clients discover and
call the explicit `native_import_apis` selection in Native mode. Switching either
bridge off preserves the selected APIs and exposure mode. Home Assistant's own
built-in Assist tools are unchanged.

An existing explicit `enable_llm_api=true` remains enabled; an old entry with
that option absent now has export disabled. For compatibility, an existing
nonempty `native_import_apis` list with no import switch is migrated to an
explicit enabled import. A saved `false` always wins over the retained list.
Legacy runtime supports export, but retains the native import choice for a later
runtime switch without exposing an import bridge.

Native calls use validated Home Assistant identity. They do not create an
internal administrator token or reconnect to HA. Secret-URL and legacy OAuth
modes retain an explicitly configured owner. Preserve the existing config-entry,
webhook and LLM API identifiers; do not delete and recreate an entry to upgrade.

Retained legacy credential fields are used only by legacy mode. A native entry
created without a legacy credential needs a valid administrator credential
configured explicitly before it can run legacy. A missing or revoked credential
must produce the existing setup/repair refusal. Do not export tokens, private
keys, secret webhook paths, config-entry storage or real authentication receipts
in build evidence or public issue reports.

To switch runtime within the same installed candidate, select legacy in the
entry options. An exact hashed manifest wheel requires the installed version
and PEP 610 URL to match. A conflicting archive hash is refused. If uv omitted
the hash, the component's independent index must instead prove equality of the
installed files; this does not invent an archive digest in the receipt.
Receipt and payload inspection run off the HA loop. Overrides, explicit reinstall
requests, conflicting distributions and unproven origins still require installation; after
native bindings have loaded, that path requires a full Core restart. The
receipt is installation metadata. The separate payload check establishes file
equality at verification time, not a publisher signature or arbitrary in-memory
code identity.

To roll back code, select the previous published HACS version, retain its paired
wheel/ZIP, and restart Home Assistant. Keep older releases available. Before
downgrading Core below the native minimum, return to a compatible legacy
configuration and artifact. Native mode must refuse the downgrade rather than
silently activate another transport. HACS file rollback does not undo changes
to HA entities, configuration or credentials; use the appropriate backup when
those data must also be restored.

## Remaining public-install qualification

Local tests cover archive selection/layout, versions, hashes, source isolation,
no overwrite, deterministic ZIP bytes and the managed-install receipt decision.
The disposable Core CI installs the matching wheel locally and replaces the
unpublished URL with an exact version pin **only in its image**. Those tests do
not prove GitHub delivery or a real HACS download of the public candidate.
The [public-wheel qualification lane](../../tests/native/PUBLISHED.md) exercises
Core's installer with the unchanged published manifest and checks its receipt.

After publication, verify anonymously accessible ZIP/wheel assets, redirect/hash
handling by Core's real installer, receipt contents, HACS pre-release selection,
upgrade between two candidate numbers, full restart, native↔legacy switching
with the exact hashed URL, and rollback to the previous archive. Test the actual
HAOS hardware and authenticated HACS setup before claiming those deployments
qualified. No such public-install or HAOS hardware qualification is claimed here.
