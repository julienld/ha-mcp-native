# Paired modern protocol validation

This harness compares two **separate disposable embedded installations**:
the committed upstream application and its own legacy component, and a committed
native candidate and its own component. It uses the same HTTP client, fixture,
Core image, read arguments, and measurement schedule. It is not the old
8.7.0-versus-native benchmark and does not attribute a performance difference to
either the SSE EOF fix or the derived-text cache in isolation.

Local status on 2026-10-26: **50 harness/launcher tests pass on Windows**;
**no live paired Core run has been performed by this implementation agent**.
Docker is absent locally. The parent owns the checkpoint and fork execution.
This does not qualify authentication, owner coverage or installation beyond the
specific checks that actually pass in the resulting report.

## Executable launcher

`launch.py` now performs the entire bring-up/comparison/cleanup. The continuity
sources are [`tests/continuity/README.md`](../../continuity/README.md) and
[`.github/workflows/native-continuity.yml`](../../../.github/workflows/native-continuity.yml)
(the repository has no `tests/native/continuity/` directory).

Use **three separate checkouts**: this harness, baseline at exactly
`3690af6e0f62ccec6c7fa70f748494ba2605f419` (U2), and the committed candidate.
Initialize their pinned submodules. Both application checkouts must be disposable:
the launcher overlays only test helpers, never `src`, `custom_components`,
`pyproject.toml` or `uv.lock`. It checks their Git identities before staging,
after building, and after comparison. It neither commits nor changes branches.

```powershell
uv run -s tests/native/protocol_comparison/launch.py run --baseline-source <U2-checkout> --candidate-source <candidate-checkout> --candidate-revision <full-sha> --output <new-report-directory> --state <private-cleanup-state.json>
```

`--candidate-revision` defaults to `GITHUB_SHA` in Actions and is required outside
Actions. Baseline is not a floating/ref input. `--core-image` defaults to the
continuity-compatible Core `2026.10.4`; the launcher pulls it once and starts both
roles from that **same immutable image ID**. It verifies each container's actual
Docker `.Image`, privilege flag and published bindings. Runtime checks additionally
require equal Core/Python/architecture and private FastMCP **4.1.2**, MCP **2.3.0**
and mcp-types **2.3.0** before the measured calls.

The launcher reuses continuity's `_build_embedded_server_wheel`,
`_install_custom_component`, `pair_component_library`, `_install_embedded_server`,
`stage_native_runtime` and `_build_ha_testcontainer`, with
`tests/qualification_artifacts.py` for the native paired artifacts. Each role
imports the same overlaid helpers inside its **own source checkout and process**;
legacy builds U2's own wheel/component, and native builds the candidate's own
paired release-profile artifacts. It seeds a minimal fresh config for these
contracts, rather than copying the broad suite's recorder, HACS or known tokens.
No shared continuity source file needs modification.

Each container has its own Docker bridge, the same two-CPU/two-GiB limit, no
privileged mode, and only Core port 8123 published on `127.0.0.1` with a distinct
dynamic host port. Package installation can use outbound network access; neither
the legacy private listener nor any public tunnel is published. A nonlocal Docker
daemon is refused. The candidate webhook uses `ha_auth`; the legacy webhook uses
its random secret URL and its configured administrator token. These are deliberate
runtime semantics: baseline calls must be attributed to its configured owner;
native calls must be attributed to the two distinct authenticated callers.

For each role the launcher seeds two actual, active human administrator records
with random identifiers and signed run-local LLATs expiring after two hours. Core
validates the credentials normally. The provenance fixture remains an authenticated
observer, never an authentication/owner substitute. The native entry contains no
internal admin token. No household token, GitHub token or ambient HA/proxy setting
is passed to workers or the comparison CLI.

Read-only begins off; module selection is identical, automatic backups, developer
mode and mandatory BPS are off. The existing CLI runs **60 rounds × (1 + 4) × 2 =
600 measured reads**, plus its separate warmups and protocol/actor/read-only
contracts. It alternates AB/BA and restores real persisted read-only policy through
the entry lifecycle. No performance threshold or faster-native claim is made.

Continuous stdout/stderr capture starts immediately after each container starts,
and remains in a mode-0700 private temporary directory until cleanup. Source
builds, worker errors, tokens, URLs, identities and raw logs are never uploaded.
Only `comparison.json` (the existing sanitized CLI report) and `launch.json`
(source/helper hashes, immutable image ID, completion/cleanup and bounded failure
phase/type) are deliverable artifacts. `launch.json.complete` also requires clean
worker exits and successful cleanup; a green comparison alone is insufficient.

The controller cleans up on success, exceptions, timeouts and SIGTERM. Worker
stdin is a lifetime lease; normal completion closes both leases and joins both
workers. POSIX cancellation also terminates their process groups. A label-scoped,
idempotent fallback removes only this run's containers/networks, repairs ownership
of the private mount, and deletes its configs/logs. It never runs Docker prune.
If cleanup failed or the controller was killed, retry using its private state:

```powershell
uv run -s tests/native/protocol_comparison/launch.py cleanup --state <private-cleanup-state.json>
```

The new [`native-protocol-comparison.yml`](../../../.github/workflows/native-protocol-comparison.yml)
has `workflow_call` and manual `workflow_dispatch` only, read-only permissions,
three checkouts, locked launcher dependencies, unconditional cleanup and an exact
two-JSON upload allowlist. It selects candidate `github.sha`. The parent wires it
into `native-migration` as `target=protocol`, because a new workflow on a fork branch
may not yet have a dispatch registration. This implementation does not dispatch,
publish a release, or modify `native-migration.yml`.

## Build receipt and private configuration

One receipt per role, using the same random 32-hex run nonce and observed Core
image ID. `--component` names the actual final staged component directory, after
packaging/version generation, and `--wheel` names that role's paired wheel.

```powershell
uv run -s tests/native/protocol_comparison/run.py receipt --source <clean-source> --revision <full-sha> --component <staged-ha_mcp_tools> --wheel <paired.whl> --core-image-id sha256:<64hex> --run-nonce <32hex> --output <config>/protocol-build.json
```

This checks a clean committed application source and records its Git tree IDs.
The builder remains responsible for building those artifacts from that source;
the receipt is not a cryptographic build attestation. At runtime the fixture hashes
the installed component and the entire installed `ha_mcp` package (excluding
bytecode), compares them with the staged component/wheel bytes, and observes the
manager, private SDK, Core, Python and architecture. A staged wheel file alone
does not establish that it is the imported runtime. Both checks are required.

Example private configuration shape (placeholders are intentionally non-runnable):

```json
{
  "baseline": {
    "mcp_url": "http://127.0.0.1:PORT/SECRET_PATH",
    "fixture_url": "http://127.0.0.1:PORT/api/protocol_fixture",
    "admin_token_env": "BASELINE_ADMIN_TOKEN",
    "actors": [
      {"token_env": "BASELINE_ADMIN_TOKEN", "expected_user_env": "BASELINE_OWNER_ID"},
      {"token_env": "BASELINE_SECOND_TOKEN", "expected_user_env": "BASELINE_OWNER_ID"}
    ],
    "server_log": "private-baseline.log",
    "expected": {"runtime": "legacy", "...": "all keys from that role's protocol-build.json"}
  },
  "candidate": {
    "mcp_url": "http://127.0.0.1:OTHER_PORT/SECRET_PATH",
    "fixture_url": "http://127.0.0.1:OTHER_PORT/api/protocol_fixture",
    "admin_token_env": "CANDIDATE_ADMIN_TOKEN",
    "actors": [
      {"token_env": "CANDIDATE_ADMIN_TOKEN", "expected_user_env": "CANDIDATE_ADMIN_ID"},
      {"token_env": "CANDIDATE_SECOND_TOKEN", "expected_user_env": "CANDIDATE_SECOND_ID"}
    ],
    "server_log": "private-candidate.log",
    "expected": {"runtime": "native", "...": "all keys from that role's protocol-build.json"}
  }
}
```

`expected` is the receipt object merged with `runtime`, not the literal ellipsis.
The nonce is private control data, omitted from the final report along with tokens,
URLs, identities, paths, payloads, log lines and exception text. Do not upload the
input config, fixture responses or raw container logs.

```powershell
uv run -s tests/native/protocol_comparison/run.py compare --config <private-config.json> --output <sanitized-report.json> --rounds 12 --warmups 2
```

Use `--rounds 60` for 600 measured reads across both runtimes. The even rounds are
bounded at 100. Each width (1, then 4 concurrent requests) has its own warmup;
warmups and functional probes are excluded from measurement summaries. Every
paired block has identical tool arguments and target indices; order alternates
AB then BA. The same HTTP connection pool and client implementation serve both
roles, with no SDK/client cache. Every response gets a semantic validation outside
its request-duration measurement. Raw durations, status, media type, byte count,
bounded request traces and failures survive in JSON. The report also fingerprints
the harness sources/fixture/lock and the read schedule.
The summaries count response media types separately; a JSON-only observation is
not evidence that the SSE response path ran.

`complete: true` and exit 0 require successful protocol/functional checks, valid
measured reads, matching runtime receipts, restored policy, verified log capture
and no captured SSE/ASGI/connection failure or traceback. Latency has no pass/fail
threshold; the harness does not assert native is faster. `complete: false` is
evidence of an incomplete/failed qualification, even if some timings look good.

## Covered protocol and limitations

- Explicit MCP **2026-07-28** `server/discover`, paginated `tools/list`, and
  `tools/call`. Modern `_meta`, `Mcp-Protocol-Version`, `Mcp-Method`, and `Mcp-Name`
  follow the private SDK 2.3.0 source. The client never automatically downgrades.
- Both runtime receipts require private FastMCP **4.1.2**, MCP **2.3.0**, and
  mcp-types **2.3.0**. Core's global MCP 1.x distribution is not used as the client.
- Invalid version: HTTP 400 with `UNSUPPORTED_PROTOCOL_VERSION` (-32022).
  Separate bounded legacy probe: 2025-11-25 initialize, list and state call, for
  these stateless servers. It is not a full SDK 1.x interoperability campaign.
- Read annotations; service response preserves nested zero/false/empty values;
  observed actor and state mutation; read-only write denial while reads still
  work; benchmark reads leave the fixture unchanged.
- SSE is a response encoding of modern Streamable HTTP here, not old SSE
  transport. Complete correlated replies are required; truncation, wrong/duplicate
  IDs, oversized streams, empty EOF and embedded server requests fail explicitly.
- No MRTR, images, continuations, reconnect/resume, long-lived subscription or
  disconnect-cancellation qualification. No experimental JSON-only mode is
  enabled, and no production transport default is changed.
- Pair timing describes this fixture and machine. It does not isolate effects of
  two patches or qualify other architectures/Core versions. Runtime package hashes
  establish byte identity, while source origin and container image ID remain
  builder attestations. The launcher records actual container image IDs; build logs remain private and are deleted during cleanup.

## Local harness verification

From the worktree, using its existing Windows venv:

```powershell
.venv/Scripts/python.exe -m pytest tests/native/protocol_comparison/ --timeout-method=thread -q
.venv/Scripts/ruff.exe check tests/native/protocol_comparison
uv run --locked -s tests/native/protocol_comparison/launch.py --help
```

The socket test starts only a loopback fixture HTTP server. It validates the
client's emitted envelope and parser over a real socket; it does **not** qualify
the real HA server. Parser/scheduling tests deliberately use synthetic inputs and
do not claim to exercise the production EOF patch or native text cache.

`test_launch.py` exercises real continuity entry seeding and real child-process
lifetimes (success, CLI failure and failed bring-up), source admission, Docker
inspection refusal, credential isolation and cleanup failure. Docker commands and
comparison peers are test doubles in the launcher lifecycle tests. They establish
orchestration behavior, not a live Home Assistant or MCP pass.
