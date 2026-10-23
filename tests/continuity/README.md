# Public MCP continuity scenarios

These four scenarios are an additional behavioral comparison of the original
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

| Test in `tests/src/e2e/continuity/test_user_workflows.py` | Public operations | What fails if behavior regresses |
|---|---|---|
| `test_rejected_stale_automation_cannot_replace_or_run_the_accepted_edit` | Create helper and automation; read; transform with a valid hash; read; submit the genuinely older configuration/hash with `run_actions`; read; run accepted actions | A write returns a hash unrelated to persisted content; stale replacement changes stored config or executes actions despite refusal; executing the saved automation runs the old/rejected action |
| `test_service_response_keeps_payload_and_projected_mutation_together` | Create helper and response script; call the script with `return_response` and `result_fields`; read state | Nested response values disappear or change; projected changed states lose the actual mutation; a successful call does not perform its advertised effect |
| `test_concurrent_script_calls_keep_each_response_with_its_own_effect` | Two concurrent public calls to one parallel response script, each targeting its own helper with a distinct value; read both states | An invocation is lost, variables or responses cross between requests, or a response succeeds without its own state change |
| `test_label_reassignment_changes_service_targets_without_stale_members` | Create three helpers and a label; assign two; call by label; clear one assignment and assign the third; read registry; call by label again | Label targeting omits a member, includes a witness, retains a cleared member, or fails to pick up a newly assigned member |

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
| `tests/native/configuration_checks.py`, `registry_checks.py`, `bulk_checks.py` | Real native configuration execution, registry writes, restore and bulk behavior over the native qualification server | The new scenarios have no native fixture API or setup dependency and can run unchanged against the original MCP server |

`tests/native/run_container.py::Checks.call` really sends public MCP `tools/call`.
Its broader setup and many checks also use the native fixture control API or the
new component's settings routes. Those useful native proofs alone are not a
comparison with the original server.

## Paths not established by these four scenarios

This is a focused addition, not complete backend equivalence. Keep these limits
visible in the comparison report rather than replacing missing evidence with
skips:

- **Simultaneous writes to the same configuration:** an optimistic hash is not a
  transaction with native Home Assistant writers. The deterministic two-editor
  sequence above proves refusal after a completed competing write; it does not
  claim serializability of overlapping reads and saves.
- **Edit snapshots and restore:** existing E2E full-loop tests under
  `workflows/auto_backup/` cover numerous domains, but many enable backup settings
  through test-process monkeypatching and carry `external_only`. Native checks
  exercise the new component's settings UI routes. Neither is silently counted as
  an unchanged original/native embedded comparison. A relevant next common
  scenario is public `ha_manage_backup(scope='edits')` capture, mutate, restore,
  execute, with the same explicit backup policy in both runner installations.
- **Config entries and options flows:** existing
  `workflows/config/test_config_entry_flow.py` and `test_config_subentries.py`
  exercise real Core flows. This addition does not cover cancellation, rejected
  updates, or simultaneous options flows. The useful extension is to change a
  template helper through its public options flow and prove its output changes
  without losing unrelated options, with baseline-supported flow inputs.
- **Device removal, integration updates, radio, full HA backups and restart:**
  these need specific integrations, Supervisor or disruptive lifecycle setup.
  Existing native qualification checks and HAOS tests are separate evidence.
  They cannot be represented by fake success replies in this common suite.
- **Cross-client and cross-actor isolation:** the concurrent scenario shares one
  authenticated MCP session. It detects request correlation failures, not
  authorization leakage between Home Assistant users.

## Execution and provenance

From `tests/`, with the comparison runner supplying the disposable environment:

```text
uv run pytest src/e2e/continuity/ -v --tb=short --maxfail=0
```

Contributor: Codex sub-agent `/root/native_behavior_contracts_astra`, 2026-10-22.
`Codex-Session: 01a166da-5ef1-71f0-884e-f62c98210c14`.
No live-pass claim is recorded here; execution evidence belongs to the paired
runner reports for their immutable source and test hashes.
