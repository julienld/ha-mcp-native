# Paired modern protocol validation

This harness compares two **separate disposable embedded installations**:
the committed upstream application and its own legacy component, and a committed
native candidate and its own component. It uses the same HTTP client, fixture,
Core image, read arguments, and measurement schedule. It is not the old
8.7.0-versus-native benchmark and does not attribute a performance difference to
either the SSE EOF fix or the derived-text cache in isolation.

Local status on 2026-10-25: **25 harness tests pass on Windows**; **no live Core run has
been performed by this test agent**. The parent owns deployment and fork execution.
There is no workflow dispatch, release, production modification or remote service
in this directory. All fixture writes are confined to the disposable containers.

## Parent wiring: reuse continuity bring-up

Reuse the existing `tests/src/e2e/_conftest_testcontainer.py`,
`_conftest_embedded.py`, `_conftest_native_runtime.py`, and
`tests/qualification_artifacts.py` staging paths from native continuity. Keep two
fixture processes alive concurrently on one isolated fork runner. These hooks are
required before this CLI can run; it is not a second deployment engine.

1. Check out **complete immutable SHAs** into separate source directories.
   Upstream is the full SHA resolving `586bdfda`; candidate is the parent's future
   committed checkpoint. No default silently substitutes the current worktree.
   Build each wheel and component from its own clean application checkout. Keep
   `src`, `custom_components`, `pyproject.toml`, and `uv.lock` unchanged by harness
   overlays. For a source archive, create the receipt in a clean checkout at that
   commit before archiving; the receipt command intentionally requires Git proof.
2. Pull one pinned Core image and use its immutable image ID for **both** containers.
   Record the actual `.Image` from `docker inspect` for each container, not a mutable
   tag. Same CPU/memory configuration, architecture and runner. The harness rejects
   different Core, Python, architecture, image ID or private SDK versions.
3. Baseline: continuity embedded **legacy** path, upstream's own component and
   wheel. Candidate: continuity **native** path, paired native artifact staging.
   Do not point an upstream external client at the candidate component.
4. Copy `protocol_fixture/` to each config's
   `custom_components/protocol_fixture/`; append `protocol_fixture:` to its YAML.
   Copy each wheel to that config root and keep it there for byte attestation.
   Stage receipts below as `protocol-build.json` before Core startup.
5. Keep the standard continuity `.ha_mcp/feature_flags.json` location. Begin with
   read-only **off**, all three read tools and `ha_call_service` enabled, identical
   module selection, automatic backup off and mandatory BPS off in both fixtures.
   Disable developer edits during the run. The policy check persists read-only on,
   unloads/reloads the actual server entry, verifies rejection without an effect,
   then restores read-only off in `finally`. Dispose both configs after the run.
6. Provision two disposable active human admins with different tokens. Configure
   the native ingress to use HA authentication when checking per-caller identity;
   a webhook configured to run as its owner deliberately has owner attribution.
   The baseline actor expectation is its configured HA token owner. The two actor
   expectations are explicit inputs, not inferred from successful replies. Put
   tokens and IDs in process environment variables, never in uploaded artifacts.
7. Keep each container's stdout/stderr streaming into a separate **private local**
   log file for the whole comparison. The fixture writes
   `PROTOCOL_COMPARISON_CAPTURE` at observations; at least two markers per log
   window are required so an empty/stale file cannot qualify as error-free.
8. Build a private config with loopback MCP and fixture URLs. Only literal
   `http://127.0.0.1` / `http://[::1]` targets are accepted. Redirects, proxy env
   variables, LAN hosts and ambient Home Assistant environment variables are not
   used. The fixture nonce and all artifact identities must match before writes.
   Each MCP URL must share its fixture's Core origin, and the two roles must use
   different listener ports, preventing cross-wiring to the other component.
   Run the CLI, retain only its sanitized JSON, then stop both fixture processes
   and containers in the parent's unconditional cleanup.

The fixture's response service is real Core registration, not a mocked MCP
transport or tool. Its state and service records are inspected independently of
the public MCP response. No Home Assistant production instance is involved.

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
  builder attestations. Parent must preserve the build log and actual image ID.

## Local harness verification

From the worktree, using its existing Windows venv:

```powershell
.venv/Scripts/python.exe -m pytest tests/native/protocol_comparison/test_wire.py tests/native/protocol_comparison/test_measurements.py --timeout-method=thread -q
.venv/Scripts/ruff.exe check tests/native/protocol_comparison
uv run -s tests/native/protocol_comparison/run.py --help
```

The socket test starts only a loopback fixture HTTP server. It validates the
client's emitted envelope and parser over a real socket; it does **not** qualify
the real HA server. Parser/scheduling tests deliberately use synthetic inputs and
do not claim to exercise the production EOF patch or native text cache.
