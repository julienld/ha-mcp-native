# Public-wheel qualification

The ordinary `Native migration` job builds unpublished candidates and installs
their wheels in a disposable image. Its image-only manifest pin cannot prove a
Home Assistant installation from the public release URL.

The optional `published_tag` workflow-dispatch input selects a separate lane for
that proof. It does not build, tag, publish, or repair a release. Use it only after
the matching assets are publicly available and publication has been approved.

## Preconditions

- Select the source ref whose HEAD is exactly `native-build.json.revision`, not
  the separate HACS distribution-snapshot commit. The workflow refuses a mismatch.
- The tag belongs to the explicitly selected repository (the manual default is
  `julienld/ha-mcp-native`) and has public `native-build.json`,
  `ha_mcp_tools.zip`, and the wheel named by that metadata.
- Metadata must identify the native dependency profile and the exact source
  revision/submodule gitlinks. Both archives must match its SHA-256 values.

The workflow selects its own `GITHUB_REPOSITORY`; manual qualification accepts
`--repository OWNER/NAME`. This expected repository is trusted input, never
inferred from downloaded metadata. The downloader accepts only a single tag and
asset basenames in that repository.
HTTPS redirects are limited to GitHub's asset hosts and bounded in count and time.
JSON, component ZIP, and wheel downloads are capped at 1, 64, and 128 MiB. Archive
paths, duplicate entries, links, and extraction size are checked before extraction.
No GitHub credential is passed to the download script or container.

These checks bind the supplied assets to their declared source and digests. The
JSON is release metadata, **not a signed build-provenance attestation**.

## What runs

1. The runner downloads all three public assets and verifies the wheel's metadata,
   archive hashes, source revision, and unchanged manifest requirement.
2. Docker's `published` target starts from the pinned Core image. It records every
   installed distribution/version and the existing `pip check` conflicts. The
   build refuses either HA-MCP distribution already being installed. The image
   contains the verified component ZIP and receipts; it does **not** contain the
   downloaded wheel or an install command.
3. The same config-flow and complete existing `run_container.py` bench run.
   Home Assistant processes the original URL and hash in the manifest through
   its own requirements manager. The first config-flow has a larger timeout for
   this real download/install; no scenario is skipped.
4. After setup, then again after all scenarios and the real process restart, the
   fixture collects installation evidence in an executor. The actual PEP 610
   record must contain the public URL and expected version. A contradictory
   archive hash is refused. If the installer omits that hash, the installed
   files must match the independent wheel index shipped with the component;
   the evidence distinguishes payload equality from an archive-hash receipt.
   The manifest bytes must be unchanged; every preinstalled distribution must
   retain its version; `pip check` may contain no new conflicts. New dependencies
   are recorded and permitted.

The existing OAuth, standard MCP proxy, tool families, temporary backups, legacy
runtime round trip, self-reload, and process restart checks remain authoritative.
The lane therefore exercises the component's paired-wheel receipt checks through
the actual Core install path. It creates no production installation mechanism.

## Evidence and failure

The `native-published-container-evidence` artifact contains the ordinary sanitized
bench evidence/log, `published-install.json`, and the public asset verification
metadata. Both dependency observations are retained. An unavailable asset, source
mismatch, invalid archive, rewritten manifest, wrong receipt, changed preinstalled
version, or new dependency conflict fails the lane. There is no fallback to a
local wheel or unpublished-candidate manifest.

An empty `published_tag` keeps the existing candidate lane. Local unit tests cover
the verification/refusal paths without networking. A prepared workflow and passing
unit tests do not establish public installation success: only the completed lane
against existing public assets can establish it.

The published dispatch skips the source unit job, which must already be green at
the selected source revision. It reruns the complete real Core bench against the
published artifacts; it does not repeat the unchanged unit suite.

The ordinary candidate image also collects every preinstalled distribution before
installing its local wheel. It calls Core's own installer with its normal upgrade
behavior and constraints, then refuses any changed preinstalled version or new
dependency conflict. A plain pip install without upgrade can reuse satisfied
versions and conceal conflicts that Core's real installation would introduce.
The separate public lane remains the proof of the unchanged manifest URL.
Both lanes repeat the full dependency observation after entry setup and after
the real restart. This also covers separate manifest requirements installed by
Core after the library wheel itself, rather than checking the wheel alone.

Before the Container bench, the candidate lane creates an isolated environment
containing Core's base dependencies rather than the Container's full integration
collection. It seeds an older compatible certifi, then uses Core's actual
requirements preflight and installer for each manifest entry. Existing versions
and dependency conflicts must remain unchanged. Real protocol, storage and native
import probes expose dependencies that the full image could supply incidentally.
Declared integration dependencies and after-dependencies are processed by Core's
requirements manager before the runtime probes, with separate observations.
The unpublished wheel URL is explicitly mapped to its paired local wheel in this
test only. `minimal-install.json` and its sanitized log record this distinct
dependency/import qualification; it does not replace the functional Core bench
or prove installation from the public URL.
