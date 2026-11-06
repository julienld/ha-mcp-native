# Native runtime contracts and maintenance

The native component and the external server share public tool contracts.
Native calls use Core owners in process; the remote adapter retains Core's
network APIs. MCP and the LLM API enter the same native Engine.

This directory contains implementation guidance and the immutable public
contract references used by tests. Qualification runs belong in CI artifacts
and their pull requests, with their exact source revision. Historical run
counts are not evidence for a newer branch head.

## Guides

- [Porting tools](porting.md): shared methods, Core owners, admission,
  snapshots and operation outcomes.
- [Compatibility](compatibility.md): persisted identities, authentication,
  client behavior and explicit limitations.
- [Packaging and rollback](installation.md): paired releases and Core-managed
  requirements.
- [User installation guide](../in-process-server.md): setup, MCP, Assist,
  updates and switching an existing runtime.

## Regression references

The baseline is upstream revision
`2dc504fd97d8416f87b6ea7f2fd8ce0332615956`. It is deliberately not replaced
when current code gains a capability.

- `catalog-baseline.json` records decorated tool names, input/output schemas
  and annotations with the historical feature flags enabled.
- `catalog-extras-baseline.json` records the server-generated skill-guide tool.
- `compatibility.json` records retained aliases, identities and reviewed
  differences. Tests check its contracts, not a claim of complete provider parity.
- `inventory.json` is generated source metadata for locating bindings and
  shared code. A declaration is not proof that an operation worked on Core.

Tests reject lost baseline tools, changed existing inputs, newly required
arguments, incompatible output schemas and unexplained annotation changes.
They allow additive tools and optional parameters. Keep real behavioral tests
alongside these schema checks: aliases, search proxies, nested calls and Assist
must preserve the same restrictions and observable effects.

Reviewed upstream changes from `1c4b4edfc` remain explicit exceptions to
that historical capture: HACS categories accept the store's strings; Todo
status filters and removals also accept lists, and Core validates status
strings. `ha_manage_backup.inputSchema.properties.restore_database` now has
the upstream nullable boolean default `null`: an omitted value restores the
database only when the archive contains one. Explicit `false` still preserves
the running database. `test_native_catalog_contract.py` recognizes only these
reviewed schema paths and shapes, rejects other drift, and exercises the
public backup default with archives both with and without a database. Neither
catalog capture is regenerated for these changes.

`entity-backup-test-migration.json` maps every retired entity-registry adapter
scenario to the canonical owner tests. UUID history tests exercise the actual
legacy registry handler or `NativeEntityRegistry`; historical `entity` files
retain their separate StateMachine semantics. Obsolete transport warnings and
lock-hook assertions are identified separately from transferred safety and
identity assertions.

## Updating generated metadata

Regenerate only the current source inventory:

```text
uv run -s scripts/native_inventory.py
uv run -s scripts/native_inventory.py --check
```

The baseline catalog files are preservation fixtures. Do not regenerate them
from a changed tool surface to make a regression disappear. The capture
options are for deliberately establishing a separately reviewed baseline.

When adding a native tool binding, update the inventory source list and its
tests together. Shared tool signatures and annotations remain the source for
the generated website and tool documentation.

## Qualification

Each PR and main run resolves the current stable Core month and its predecessor,
subject to the implementation floor. The matrix includes the exact declared
minimum, current patches, amd64 and aarch64, and the Python supplied by Core.
The separate floor gate prevents publishing a release with stale compatibility
metadata.

Package installation, real Core calls, permissions, restart, update and rollback
are separate claims. Record which were executed. Stubbed tests, a successful
dependency resolution, or a static inventory cannot establish real HACS or
Marketplace installation behavior.
