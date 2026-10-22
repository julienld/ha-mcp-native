# Native candidate installation and rollback

The public fork is
[`julienld/ha-mcp-native`](https://github.com/julienld/ha-mcp-native).
Choose a reviewed version from its [published releases](https://github.com/julienld/ha-mcp-native/releases)
before installation. The preparation commands below create local artifacts;
they do not publish an installable release. Publication,
including a draft release or a pushed distribution tag, needs a separate final
approval. Legacy compatibility has not been deprecated.

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

Review the wheel, component ZIP, `native-build.json` and `hacs-repository/`
together. Verify that the snapshot's component files match the archive, that its
manifest names that wheel and hash, and that the source commit links resolve.
Use the same artifacts for the qualification run and eventual upload.

After approval, the minimal publication sequence is:

1. Initialize a separate local Git checkout from `hacs-repository/`, without
   adding workflows or copying the development checkout's `.git` directory.
   Commit the tree with the required session trailers. Its commit is the
   **distribution commit**; `native-source.json` retains the distinct source SHA.
2. Push only a dedicated distribution branch and the reviewed version tag to
   `julienld/ha-mcp-native`. Do not push to `master`, move the default branch,
   retag an existing release, or dispatch an upstream publishing workflow.
3. Enable immutable releases if approved, create a GitHub **draft pre-release**,
   attach the exact wheel, `ha_mcp_tools.zip` and `native-build.json`, and verify
   their uploaded digests. Publish only after the final approval. Draft assets
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
   needed. Webhook-off remains Assist-only.
5. Verify the selected HACS tag, manifest and installed library version, the
   configured webhook/Assist access, and the native Core log. The previous
   legacy-only listener URL needs legacy mode or an explicitly updated HA URL.

## Credentials and rollback

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
entry options. An exact hashed manifest wheel can be reused only when its
installed version and PEP 610 receipt prove the same URL and SHA-256. Receipt
inspection runs off the HA loop. Overrides, explicit reinstall requests,
conflicting distributions and unproven origins still require installation; after
native bindings have loaded, that path requires a full Core restart. The
receipt is installation metadata, not proof against local file tampering.

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

After publication, verify anonymously accessible ZIP/wheel assets, redirect/hash
handling by Core's real installer, receipt contents, HACS pre-release selection,
upgrade between two candidate numbers, full restart, native↔legacy switching
with the exact hashed URL, and rollback to the previous archive. Test the actual
HAOS hardware and authenticated HACS setup before claiming those deployments
qualified. No such public-install or HAOS hardware qualification is claimed here.
