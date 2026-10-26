# Isolated HACS owner qualification

Status: **first runner attempt failed during bootstrap; local repair awaits a
new checkpoint/run**. On 2026-10-26 UTC, [run 40264628510](https://github.com/julienld/ha-mcp-native/actions/runs/40264628510)
at `1a9cdadef8c5661b4f09dbbadc7866bd1bd78a52` failed in both topologies,
before any HACS qualification check. Local Windows has no Docker executable.
A green local protocol test is not a live Home Assistant qualification.

This lane fills the gap left by `tests/native/hacs_checks.py`, which proves only
HACS absence. It reuses the native candidate image, fixture identities, real HA
login/config flows and MCP client. It does not modify the shared runner, shared
fixture, production adapter or self-updater.

## Owners and fixture boundary

- Actual Home Assistant Container **2026.10.4**, initially amd64. Both the
  server-only and server + tools entry topologies are selected by the manual
  workflow. The shared WS cache capability is exercised in both.
- Actual, unchanged HACS **2.0.5** from its official release archive:
  `https://github.com/hacs/integration/releases/download/2.0.5/hacs.zip`.
  SHA-256: `97be6b824a4f38e683728cc6dd72367f6b8bad0a43428b1b3b987a3087adf413`.
  The existing vendored HACS fixture has five source files with lint-only
  differences from this archive. The dedicated image uses the official archive,
  including its frontend; no vendored file is changed.
- Real HACS configuration-entry setup, startup, repositories, storage, validators,
  download/extraction/backup/restore, update platform and `update.install` service.
  `aiogithubapi==26.0.0` matches Core 2026.10.4's package constraint, verified from
  its tagged `homeassistant/package_constraints.txt`. Core's own uv stays intact.
- The **network data is synthetic**. A bench-only aiohttp URL redirection sends
  GitHub and HACS catalog URLs to a real local HTTP fixture server. It does not
  return mocked client objects or replace any owner method/service. Unexpected
  destinations fail closed; unexpected fixture routes are recorded and fail the
  run. No GitHub credential is needed. The literal HACS token is dummy text.
- Runtime has `docker --network none`, no published port and no host bind mount.
  The driver runs through `docker exec` in the same disposable container. Image
  build downloads are setup, before isolation. The build installs the requirements
  of the actual HACS and candidate component manifests using Core's installer and
  constraints, skips satisfied requirements and rechecks the whole set. It adds
  no bench-specific replacement for a missing native manifest requirement.
  Core's `core.network` store selects the real `lo` interface before startup;
  neither Core's resolver nor its Zeroconf implementation is replaced.
  Config, identities and auth tokens
  never leave the container. Only scoped observations and logs are copied out.
- The installed subject is the explicit `qualification_dummy` integration, with
  fixture versions 1.0.0, 2.0.0 and 3.0.0. These are **not HA-MCP releases**. HA-MCP
  code under test comes from the native artifacts built from the committed SHA.

The checks assert registration, cache-only MCP/WS reads preserving the `new`
flag and disk/network state, admin rejection before effects, first installation
without an update entity, creation of the actual HACS update entity, subsequent
Core `update.install` with the real user Context, installed-versus-running code,
partial ZIP extraction followed by HACS backup restoration, explicit retry after
inspection, loss of the MCP response while the archive is in flight, owner reload,
actual Core process restart, and uninstall while the old module remains loaded.

The failed update uses a bad CRC in the **second ZIP member**: the first manifest
has already been written. Comparing the restored code hashes therefore exercises
rollback rather than merely handling a refused network request. The lost-response
case cancels the client request, waits for the archive request already observed by
the local HTTP server, then verifies one owner dispatch and observes completion.
It never substitutes an exception-throwing fake `update.install`.

`HACS reload` and `Core restart` are separate checks. HACS can reconstruct its
pending-restart flag on reload while Python still holds the old module; the bench
records the actually loaded dummy version independently. This is not evidence
that an installed HA-MCP version is active.

## Execute after the parent's checkpoint

The workflow has `workflow_dispatch`, `workflow_call` and `contents: read`; it contains no
personal repository name, ownership guard or permanent restriction to forks.
The current task still requires the parent to execute on the authorized public
fork. No workflow dispatch has been performed by this sub-agent.

Once the workflow is discoverable in that repository (GitHub may require it on
the default branch), the parent can select the committed checkpoint:

```powershell
gh workflow run native-hacs-owner-qualification.yml --repo <authorized-fork> --ref <committed-checkpoint-branch>
```

It builds an unpublished wheel/component pair with `scripts/native_release.py
--revision "$GITHUB_SHA" --qualification-only`, builds the two disposable images,
runs both entry topologies, and uploads only evidence. The generated candidate
tag string is artifact metadata: nothing creates a Git tag or GitHub release.
If GitHub does not yet recognize the new workflow, do not automatically copy it
to another branch or change repository settings; the parent owns that next step.

On an already selected Linux Docker runner, with `dist/native` built from the
checkpoint using the existing native release builder:

```sh
docker build --build-arg HA_IMAGE=ghcr.io/home-assistant/home-assistant:2026.10.4 -f tests/native/Dockerfile -t ha-mcp-native-candidate .
docker build -f tests/native/qualification_hacs/Dockerfile -t ha-mcp-hacs-qualification .
uv run -s tests/native/qualification_hacs/run.py --topology server-only --output tests/native/qualification_hacs/out-server-only
uv run -s tests/native/qualification_hacs/run.py --topology both --output tests/native/qualification_hacs/out-both
```

Output directories must be fresh so an old `complete: true` cannot hide a failed
run. The container is removed after evidence collection, including on failure.
Inspect `evidence.json`, both driver logs, `ha.log`, and `isolation.json`. Only the
second driver phase can set `complete: true`. `ha_mcp_self_update_qualified`,
`publication_chain_qualified`, and `marketplace_qualified` remain false even on
success. The image ID, source build metadata, actual Core/HACS versions, owner
class, dispatch Context and filesystem digests support causal diagnosis.

Hook for reuse: `checks.exercise_hacs_owner(checks, control, base=..., admin_id=...)`
uses the existing native `Checks` interface and the isolated fixture's control
endpoint. Parent integration can call it without editing the shared runner or
fixture. Do not aim it at an existing/personal Home Assistant.

## Local validation

The complete `ha.log` and `exercise.log` artifacts from **both** failed jobs were
read, along with their evidence/isolation records and the external job failure
log. Both failures have the same causes:

- Core's aiohttp resolver creates Zeroconf for Analytics and HACS. Automatic
  adapter selection leaves no enabled adapter in `--network none`; Zeroconf
  tries default multicast membership and raises `OSError: [Errno 19] No such
  device`. Selecting `lo` in Core's persisted network preferences avoids that
  default-interface branch. Core itself filters loopback from multicast source
  addresses. This follows the unchanged tagged
  [network selection](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/network/util.py),
  [Zeroconf arguments](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/zeroconf/__init__.py)
  and [socket construction](https://github.com/python-zeroconf/python-zeroconf/blob/0.151.5/src/zeroconf/_utils/net.py).
- `ruamel.yaml>=0.18.0` is declared by the component but absent from the candidate
  image. Core attempts installation after isolation; DNS retries exhaust the
  driver's config-flow deadline. The dedicated image now processes both manifests
  before isolation, including the candidate's generated native requirements.

The `AssertionError` is in this fixture's `started()` callback, after Core's
`async_set_disabled_by()` returns the failed HACS reload result. It is retained:
it is a symptom of the resolver failure, not a reason to weaken owner checks.
No product bug outside the bench was confirmed by these traces. `checks` remains
empty and `complete` false in the original artifacts; none were rewritten.

The bootstrap regression failed locally before the fix (`core.network` absent),
then passed. Build-policy tests cover missing requirements, preserving satisfied
packages, installer failure and a later install invalidating an earlier
requirement. These use an installer stand-in; the actual Docker build and Linux
Core startup still require the parent's runner. The HTTP tests use both real
aiogithubapi clients and prove the archive fault occurs after a first write.

Local setup and tests with the existing Windows venv, without modifying that venv
or running `uv sync`:

```powershell
uv pip install --python .venv/Scripts/python.exe --target local/qualification-1a9/hacs-test-deps --no-deps aiogithubapi==26.0.0 backoff==2.2.1
$env:PYTHONPATH = (Resolve-Path local/qualification-1a9/hacs-test-deps).Path
.venv/Scripts/python.exe -m pytest tests/native/qualification_hacs --timeout-method=thread -o addopts='' -q
```

Observed after repair: **7 passed**. Ruff check/format and compilation passed for
this directory. No workflow, owner implementation, update service or provenance
check was changed; no dispatch, index, commit or push was performed by this repair.

Before the first runner attempt, the candidate boundary tests were run with the ready
Windows `.venv`, `pytest --timeout-method=thread`: **106 passed** across
`test_native_hacs.py`, `test_native_marketplace.py`, `test_native_self_update.py`,
`test_self_update_provenance.py`, and `test_hacs_cache_bridge.py`. They use their
existing stand-ins and are strictly historical unit evidence, not a revalidation
of concurrent parent changes.

## Remaining qualification and publication requirements

| Scope | Requires a new public candidate? | Current evidence / next step |
|---|---|---|
| HACS dummy registration, install, upgrade, rollback, response loss, restore, restart | **No** | This complete disposable lane is prepared; runner execution is still required. |
| HACS cached MCP/WS reads and admin rejection | **No** | Same lane, both topologies; no network token. |
| Real Marketplace consent/admin refusal, cache and install lifecycle | **No**, an isolated exact Core dev build suffices | Not executed or implemented as a live lane here. Reviewed adapter source target remains Core dev `7f6df9f7c69c2158ca7709ce1d3c457b243d7c72`; stable 2026.10.4 has no such owner. Do not report 2026.11 as released. |
| HA-MCP self-update owner discovery refuses the dummy | **No** | Negative MCP check is included; it must keep `manual_required` and perform no owner write. |
| Full `NativeSelfUpdate` installation, durable receipt, anti-replay, pending restart and recovery of HA-MCP code | **Not inherently**, but not covered by this builder | The reused candidate image replaces the immutable manifest wheel URL with `ha-mcp==<version>` to use its local wheel. That cannot establish paired fork-origin provenance. This lane does not change URL/hash/origin checks, fabricate a receipt, register the dummy as HA-MCP, or falsify installed/running versions. |
| Unchanged production URL/hash/origin download and actual GitHub release/index delivery for a new candidate | **Yes**, for a candidate not already published | Requires an explicitly authorized immutable wheel + component release, correct repository/origin pairing, and owner discovery of that release. Existing public releases can qualify only the code they contain. No new tag is authorized or created here. |

An alternative future **unpublished** NativeSelfUpdate bench would need two real,
paired build artifacts, an initial installation whose unchanged manifest and
distribution establish the same repository/URL/hash origin, and controlled content
delivery for **both HACS and Core's package installer**, followed by a real process
restart and receipt checks. The current aiohttp redirect covers HACS HTTP, not
Core's separate package-installer process. Completing that fixture delivery is
additional work, not permission to bypass provenance or publish a new candidate.

HACS 2.0.5 has no Marketplace-style per-user warning acceptance. Its admin gate
does not qualify Marketplace consent. The existing boundary unit tests remain
unit evidence for Marketplace and NativeSelfUpdate; the dummy lifecycle never
upgrades those claims to live qualification. No client installers, external hosts,
HAOS/Supervisor mutation, architecture matrix expansion or recurring automation
is introduced by this lane.
