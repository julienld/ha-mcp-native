# Published-asset qualification

Two paths exercise different installation evidence in
[`native-migration.yml`](../../.github/workflows/native-migration.yml):

- With no `published_tag`, the candidate lane uses locally built paired artifacts.
  Core's constrained installer installs the local wheel, and only the disposable
  image replaces the unavailable manifest URL with its exact installed version.
- With `published_tag`, the manual lane downloads an **existing public release**
  and runs Core against its unchanged manifest URL and hash. It does not build,
  tag, publish or repair a release, and never falls back to a local candidate.

The first path cannot prove GitHub delivery. The second proves the observed Core
installation and runtime, not an authenticated HACS selection or upgrade by
itself. The [maintainer installation guide](../../docs/native-migration/installation.md)
owns release delivery and rollback policy.

## Select trusted coordinates

Use the checkout whose HEAD equals `native-build.json.revision`, with its pinned
submodules. For a dev release this is the original code SHA, not the stamped
tag's `release_revision`; a mirror distribution commit is also not the source
checkout. The selected revision's unit suite must already pass: published
dispatch skips that job and the candidate compatibility-window build.

Choose the repository and tag independently of downloaded metadata. The manual
default is `homeassistant-ai/ha-mcp`; the workflow passes its own
`GITHUB_REPOSITORY` explicitly. For example, from the selected source checkout:

```powershell
uv run -s scripts/native_published_assets.py fetch --repository OWNER/REPOSITORY --tag RELEASE_TAG --output dist/native
docker build --target published --build-arg SOURCE_REVISION=SOURCE_SHA --build-arg RELEASE_REPOSITORY=OWNER/REPOSITORY --build-arg HA_IMAGE=ghcr.io/home-assistant/home-assistant:CORE_VERSION -f tests/native/Dockerfile -t ha-mcp-native-candidate .
uv run -s tests/native/run_container.py --published-tag RELEASE_TAG
```

Replace every coordinate with the chosen release, exact source SHA and Core pin.
Use a new output directory (the Dockerfile above expects `dist/native`). Historical
fork assets remain verifiable with an explicit `--repository julienld/ha-mcp-native`
and matching `RELEASE_REPOSITORY`. Builder commands use the corresponding
`--github` option. A consistent but unexpected origin is still rejected.

The release must publicly serve `native-build.json`, `ha_mcp_tools.zip` and the
wheel named by the metadata. Draft assets are not anonymously installable. The
metadata must identify the native dependency profile, exact revision and submodule
gitlinks; both archives must match its SHA-256 values. The JSON is a provenance
record, not a signed build attestation.

Downloads accept only a single tag and asset basenames in the selected repository.
Redirects are bounded and limited to GitHub HTTPS asset hosts. JSON, ZIP and wheel
downloads are capped at 1, 64 and 128 MiB respectively; component archive paths,
duplicates, links and extraction size are checked. No GitHub credential is passed
to the downloader or container. These origin and archive guards must not be
relaxed to accommodate a failed qualification.

## What the public lane verifies

1. The runner verifies public asset hashes, source identity, wheel metadata and
   the unchanged manifest requirement. It records `published-verification.json`.
2. Docker's `published` target records untouched Core distributions, constraints
   and existing dependency conflicts. It refuses an already-installed HA-MCP
   distribution and copies only the verified component and metadata, not the wheel.
3. The real Core HTTP bench runs config flow and its existing scenarios. Core's
   normal requirements manager downloads and installs the manifest's original URL
   under its constraints. Its own installer version is used; the runner uv pin
   does not select Core's installer.
4. After setup and again after scenarios and a full Core restart, the fixture
   checks the installed version and public URL in the PEP 610 receipt. A conflicting
   archive hash fails. If the installer omits it, the component's independent wheel
   index must prove installed payload equality, recorded as such rather than as
   an archive-hash receipt. Manifest bytes must remain unchanged.
5. Every dependency observation follows the
   [compatible shared-package upgrade policy](../../docs/native-migration/installation.md#compatible-shared-package-upgrades).
   Compatible upgrades from Core's manifest processing may pass; protected-package
   replacements, removals, downgrades, incompatible changes, constraint violations
   and new dependency conflicts fail. Compare consecutive observations too, so a
   package introduced at setup cannot disappear or regress at restart.

The OAuth, standard MCP proxy, tool-family, temporary-backup, legacy round-trip,
self-reload and process-restart checks exercise actual runtime behavior in
addition to file identity. No production installer or automatic rollback is added.

## Evidence and limits

`native-published-container-evidence` retains sanitized `evidence.json`,
`published-install.json`, the Core log, `native-build.json` and
`published-verification.json`. Keep both dependency observations and the actual
Core/Python versions, architecture, source and tag with the result. An unavailable
asset or any failed identity, dependency or runtime check fails the lane; passing
unit tests or a prepared workflow is not public-install evidence.

The public workflow currently uses the Dockerfile's Core `2026.10.2` default on
its amd64 runner. That is a single public-install bench, separate from the dynamic
compatibility matrix. Override `HA_IMAGE` explicitly for a different manual bench
and report what ran. HAOS, another architecture, authenticated HACS selection,
upgrade between releases and rollback require their own evidence; a public
Container run does not establish them.

Candidate qualification additionally exercises minimal Core, with older permitted
transitive packages, through the compatibility-window lane. It processes each
manifest requirement and declared integration dependency using Core's real
installer, observes dependency changes, and probes protocol, storage and native
imports. Only that test maps the unpublished URL to its paired local wheel.
`minimal-install.json` and its sanitized log record this dependency/import proof;
they do not establish public delivery. The full candidate Container bench also
checks all distributions immediately after the dependency-free wheel installation
(no existing package may change), after setup and after restart.

Local unit tests cover refusal paths without network access. Continue to require
live evidence for delivery and runtime claims, and keep existing conflicts visible
instead of describing an environment with pre-existing diagnostics as clean.
