# Public MCP continuity scenarios

These six scenarios are an additional behavioral comparison of the original
embedded server and the native embedded server. They use the same test files and
public MCP requests on each version. They do not select a backend, issue raw
WebSocket commands, invoke a backend directly, patch implementation objects, or
accept runtime-specific answers.

The reference source is `2dc504fd97d8416f87b6ea7f2fd8ce0332615956`. The comparison
runner records the candidate source, exact test-file hashes, pinned Home Assistant
version, runtime setup, and each result separately. Static inspection establishes
that the calls below exist in the reference API; it is not a passing baseline
execution. Both executions must pass before claiming continuity.

## Scenarios and observable failures

The first four tests live in `tests/src/e2e/continuity/test_user_workflows.py`;
the final two live in `tests/src/e2e/continuity/test_flow_and_restore.py`.

| Test | Public operations | What fails if behavior regresses |
|---|---|---|
| `test_rejected_stale_automation_cannot_replace_or_run_the_accepted_edit` | Create helper and automation; read; transform with a valid hash; read; submit the genuinely older configuration/hash with `run_actions`; read; run accepted actions | A write returns a hash unrelated to persisted content; stale replacement changes stored config or executes actions despite refusal; executing the saved automation runs the old/rejected action |
| `test_service_response_keeps_payload_and_projected_mutation_together` | Create helper and response script; call the script with `return_response` and `result_fields`; read state | Nested response values disappear or change; projected changed states lose the actual mutation; a successful call does not perform its advertised effect |
| `test_concurrent_script_calls_keep_each_response_with_its_own_effect` | Two concurrent public calls to one parallel response script, each targeting its own helper with a distinct value; read both states | An invocation is lost, variables or responses cross between requests, or a response succeeds without its own state change |
| `test_label_reassignment_changes_service_targets_without_stale_members` | Create three helpers and a label; assign two; call by label; clear one assignment and assign the third; read registry; call by label again | Label targeting omits a member, includes a witness, retains a cleared member, or fails to pick up a newly assigned member |
| `test_template_options_edit_changes_output_and_preserves_unit_and_name` | Create a template sensor yielding 5 with a unit and name; read state and stored options; update only its expression through the options flow; observe 7 and read options | The options flow changes stored text without changing the sensor's output, or drops the neighboring unit or name |
| `test_script_snapshot_restores_saved_behavior_after_explicit_reload` | Capture a script through `ha_manage_backup(scope='edits')`; change its action; prove persisted change; restore; compare configuration and hash; explicitly reload scripts; execute once and read its target | Restore reports success without recovering the exact saved configuration, or the restored script executes the later action after reload |

Only generated object identifiers are used as handles. Configuration dictionaries,
opaque hash equality within one run, returned response payloads, projected state
records and final states are asserted directly. No cross-version golden response
is normalized. Dates, timestamps, random IDs and hash bytes are not compared
between installations.

`_support.py` uses `MCPAssertions` for successful and refused calls and
`wait_for_tool_result` for public-tool polling. It does not use
`wait_for_entity_state`, whose implementation also opens a WebSocket. There are
no skips, backend markers or fixed propagation sleeps. The short delay inside the
parallel script is the workload under test, not a readiness wait or timing oracle.
Both concurrent calls are joined before assertions and cleanup.

The script restore scenario intentionally calls public `script.reload` once
before its sole script execution. The original backup restore persists the
configuration before Core's automatic reload hook necessarily finishes. This
scenario establishes saved content and usable behavior after an explicit reload;
it does not claim automatic reload completion, nor retry a mutating call to hide
an unknown outcome. Explicit edit snapshot creation bypasses the automatic-backup
toggle in the original public contract, so this test needs no settings mutation.

Each scenario creates uniquely named objects. Owned objects are removed in reverse
order, including after failed assertions. Cleanup errors fail an otherwise passing
test and are logged without replacing an earlier failure. Automatic backup records
belong to the disposable runner's configuration directory and disappear with it.

## Existing coverage inspected before adding tests

| Existing location | Coverage already present | Additional property above |
|---|---|---|
| `tests/src/e2e/workflows/automation/test_lifecycle.py`, `test_python_transform.py` | CRUD, correct hash acceptance, fabricated mismatch rejection, transforms and validation errors | A real competing edit followed by refusal, unchanged persisted content and executable behavior; the rejected request also asks to run actions |
| `tests/src/e2e/workflows/scripts/test_python_transform.py`, `test_lifecycle.py` | Script transforms, stale hash rejection, execution modes, second run initiation | Concurrent MCP responses are correlated to distinct invocation variables and observed effects |
| `tests/src/e2e/workflows/services/test_call_service_return_response.py` | A real `calendar.get_events` response appears once at the top level | One service both mutates state and returns a nonempty nested response while result projection is active |
| `tests/src/e2e/workflows/core/test_service.py`, `test_bulk.py` | Light service calls, projections, comma-separated targets, bulk selectors and dispatch | Label membership changes are observed by the next service dispatch, including the entity that must now be spared |
| `tests/src/e2e/workflows/labels/test_label_operations.py`, `workflows/entities/test_entity_management.py` | Registry set/add/remove/clear responses and field readbacks | Registry edits change the actual target set acted on by Core services |
| `tests/src/e2e/workflows/config/test_config_entry_flow.py` | Template creation, persisted availability edits and registry icons | A state-expression-only options edit changes the live output while retaining the unit and name, on both versions |
| `tests/src/e2e/workflows/auto_backup/test_capture_and_restore.py` | Public capture/restore over numerous domains; the script loop checks restore success and safety capture | Exact script content/hash recovery and a distinguishing executable effect after explicit reload, without process-local settings patches |
| `tests/native/configuration_checks.py`, `registry_checks.py`, `bulk_checks.py` | Real native configuration execution, registry writes, restore and bulk behavior over the native qualification server | The new scenarios have no native fixture API or setup dependency and can run unchanged against the original MCP server |

`tests/native/run_container.py::Checks.call` really sends public MCP `tools/call`.
Its broader setup and many checks also use the native fixture control API or the
new component's settings routes. Those useful native proofs alone are not a
comparison with the original server.

## Paths not established by these six scenarios

This is a focused addition, not complete backend equivalence. Keep these limits
visible in the comparison report rather than replacing missing evidence with
skips:

- **Simultaneous writes to the same configuration:** an optimistic hash is not a
  transaction with native Home Assistant writers. The deterministic two-editor
  sequence above proves refusal after a completed competing write; it does not
  claim serializability of overlapping reads and saves.
- **Other edit snapshot domains and automatic reload completion:** the common
  script test proves exact persisted restoration and behavior after an explicit
  reload. It does not establish automatic reload timing or recovery of deleted
  helpers, devices, calendars and dashboards. Existing E2E and native checks of
  these paths remain separate evidence; process-local monkeypatches and new
  settings UI routes are not silently counted as an unchanged embedded comparison.
- **Other config entries and options-flow failure paths:** the template scenario
  covers a successful partial options edit with preserved unit and name. It does
  not cover cancellation, rejected updates, simultaneous options flows or config
  subentries. Existing `workflows/config/` tests remain relevant separate evidence.
- **Device removal, integration updates, radio, full HA backups and restart:**
  these need specific integrations, Supervisor or disruptive lifecycle setup.
  Existing native qualification checks and HAOS tests are separate evidence.
  They cannot be represented by fake success replies in this common suite.
- **Cross-client and cross-actor isolation:** the concurrent scenario shares one
  authenticated MCP session. It detects request correlation failures, not
  authorization leakage between Home Assistant users.

## Execution and provenance

The paired workflow also runs the four existing cases in
`workflows/core/test_statistics_units.py` unchanged. They exercise stored/display
unit conversion, reset timestamps and mixed metadata after a native regression
was discovered by the full upstream E2E suite. Core API calls supply recorder
fixtures; all assertions concern the public MCP tool response. Their file hash
is checked separately on both revisions along with the six new scenarios.

From `tests/`, with the comparison runner supplying the disposable environment:

```text
uv run pytest src/e2e/continuity/ -v --tb=short --maxfail=0
```

Contributor: Codex sub-agent `/root/native_behavior_contracts_astra`, 2026-10-22;
two flow/restore scenarios added 2026-10-23 after the Core/original/fork audit.
`Codex-Session: 01a166da-5ef1-71f0-884e-f62c98210c14`.
No live-pass claim is recorded here; execution evidence belongs to the paired
runner reports for their immutable source and test hashes.
