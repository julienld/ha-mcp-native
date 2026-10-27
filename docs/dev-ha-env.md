# Live dev environment

`.github/workflows/dev-ha-env.yml` runs a throwaway Home Assistant on a GitHub
runner and exposes it, plus your branch's ha-mcp server, through temporary
Cloudflare tunnels. Use it to drive a branch against a real HA without Docker
and without touching your own instance. It boots HA with the E2E suite's own
bring-up and config (user `mcp` / `mcp`, token `TEST_TOKEN` in
`tests/test_constants.py`). The E2E seed gives `mcp` a French interface; after
logging in, change it under your profile (your name at the bottom of the
sidebar) → **General** → **Language**. It is separate from the test suite:
only an explicit manual request starts it, and its runner script
(`.github/dev-ha-env/hold.py`) is copied into the tracked branch's E2E directory
only for the run.

| `platform` | `server` | What runs |
|---|---|---|
| `docker` | `standalone` | HA container; your branch's server runs on the runner |
| `docker` | `embedded` | HA container; the server runs inside the component |
| `haos` | `standalone` | HAOS VM; your branch's server runs on the runner |
| `haos` | `embedded` | HAOS VM; the server runs inside the component |
| `haos` | `app` | HAOS VM; the server runs as the app (add-on) |
| `docker` | `oauth` | HA container; your branch's server runs on the runner as `ha-mcp-oauth`, with the `MCP:` URL's origin as its public URL |

Embedded runs default to `embedded_runtime=legacy`. Select
`embedded_runtime=native` with `server=embedded` to stage the paired Native
artifacts and configure the entry before HA starts, using the existing
`E2E_EMBEDDED_RUNTIME` fixture contract. A source revision without the real
Native host and staging fixture is refused before boot. Confirm the effective
runtime through MCP after startup; a requested mode or `ready` flag alone is
not runtime evidence. This avoids reconfiguring the MCP server's own entry,
which `ha_set_integration` intentionally refuses.

Test on `server=embedded` with the File & YAML Tools entry in place first (the
defaults): the server running in-process inside the component, with the
filesystem tools beside it, is ha-mcp's main install. Use `standalone` or `app`
for a change specific to that mode. `oauth` runs the standalone server through
the `ha-mcp-oauth` entrypoint: `MCP:` answers 401 until a client completes the
consent flow against that URL's origin, so drive the flow first and pass the
access token as a Bearer. The server keeps the origin it started with, so if
the `MCP:` tunnel dies the run reports it as `down` rather than publishing a
new URL; start a new run.

`no_component=true` removes the component's File & YAML Tools entry, like the
no-tools E2E lanes. `strict_bps` (default on, as agents meet it) sets strict
best-practices mode for the standalone and embedded servers. `ha_image`
overrides the Docker image (default: `ghcr.io/home-assistant/home-assistant:stable`).
Pass an explicit version tag to test an older Core release, or `:beta` to test
the current beta. This dev default is independent of the branch's E2E pin. `minutes`
defaults to 330 and must be an integer from 1 to 330. On HAOS, `cpu_max` caps the
VM's CPU once Home Assistant is up (cgroup `cpu.max`, e.g. `25000 100000` for a quarter of a host
core), to reproduce a report from a slow board. A HAOS run builds the HAOS image the first
time the image build files, the test config or the proxy app change, and saves
it to the fork's cache for later runs. A branch whose component differs from
every cached image reuses one with the same base and applies its own component
at boot with a Core restart.

Run it **from your fork only**. The job refuses to run in
`homeassistant-ai/ha-mcp`, whose runners are shared CI capacity.

1. Enable Actions on your fork. If `dev-ha-env.yml` is not registered for
   dispatch, use the [native qualification caller](#calling-through-native-qualification)
   below when it is already registered. Do not change a historical fork base
   just to register the dev workflow.
2. Make a throwaway key pair for this run. The tunnel URLs are published
   only encrypted to its public key, so nothing secret goes to GitHub and
   any machine can start a run:
   ```bash
   openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out devenv-key.pem
   ```
3. Start it against any branch of your fork, passing the public key:
   ```bash
   gh workflow run dev-ha-env.yml -R <you>/ha-mcp -f track_ref=<branch> \
     -f platform=docker -f server=embedded \
     -f public_key="$(openssl pkey -in devenv-key.pem -pubout | openssl base64 -A)"
   ```
4. Once the run reaches "Keep running", download and decrypt the URLs:
   ```bash
   gh run download <run-id> -R <you>/ha-mcp -n dev-ha-env-urls
   openssl pkeyutl -decrypt -inkey devenv-key.pem -pkeyopt rsa_padding_mode:oaep -in dev-ha-env-urls.enc -out dev-ha-env-urls.txt
   cat dev-ha-env-urls.txt
   ```
   `HA:` is the HA UI and API. `MCP:` is your branch's server (streamable
   HTTP, path included), ready to add to an MCP client.

   A tunnel that fails to start or stops later is restarted, which gives it a
   new URL, and the run uploads `dev-ha-env-urls` again: download it into an
   empty directory and decrypt it again. A tunnel still being retried shows
   as `down`. After three restarts, newer URLs are only in the job log, as a
   `DEVENV_URLS` line holding the same encrypted file in base64. The API
   serves a job's log only once the run ends, so copy the latest value from
   the live log in the browser and decode it before decrypting as above:
   ```bash
   printf '%s' '<value>' | openssl base64 -d -A -out dev-ha-env-urls.enc
   ```

   Test through `MCP:`, the way an agent uses the server: add it to your
   client as a regular MCP server, or, when the agent's client cannot load a
   new server mid-session, drive it one call at a time through a shim that
   sends `tools/list` and `tools/call` to that URL.
   [`.github/dev-ha-env/mcpshim.py`](../.github/dev-ha-env/mcpshim.py) is an
   example; agents can modify it as needed:
   ```bash
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt tools
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt describe ha_search
   python3 .github/dev-ha-env/mcpshim.py dev-ha-env-urls.txt call ha_search '{"query": "kitchen"}'
   ```
   Do the setup and the checks with the tools as well. Creating or verifying
   state through `HA:`'s REST API routes around the tools under test, so a
   gap in them goes unnoticed.
5. Iterate the way you would update ha-mcp on any Home Assistant. The run
   stays on the commit it started from; the standalone and embedded servers
   run in developer mode:
   - Embedded server: push, then call
     `ha_dev_manage_server(action="update_source",
     pip_spec="ha-mcp @ git+https://github.com/<you>/ha-mcp@<commit>")`.
     The server reinstalls and comes back within a few minutes;
     `ha_dev_manage_server(action="info")` shows the spec it runs. The run
     starts from a wheel built from your branch's checkout.
   - Component: add your fork to HACS once with
     `ha_manage_hacs(action="add_repository", repository="<you>/ha-mcp",
     category="integration")`, redownload your branch with
     `ha_manage_hacs(action="download", repository_id=<id>, version="<branch>")`,
     then restart Core with `ha_restart`. Right after a push, HACS can still
     fetch the previous commit: wait a minute and read a changed `.py` file
     with `ha_read_file` before restarting. The component keeps its pending
     version number across dev builds, so HACS's `installed_version` (the
     branch) is what names the build.
   - Standalone server or app: start a new run on the new commit.

   The job log's `STATUS` lines show the commit the run started from and any
   boot error. If the embedded server fails at boot, Home Assistant stays up
   but the server's own tools are gone: log in to `HA:` as `mcp` / `mcp`,
   open the HA-MCP Custom Component integration's server entry under
   Settings → Devices & services, and put the fixed spec in Configure →
   "Developer: ha-mcp package override".

   The instance is a normal HA you can change. On HAOS, change the Core
   version from Settings → System → Updates, or run
   `ha core update --version 2026.10.0` in the Advanced SSH app's terminal;
   apps, integrations and the component install and uninstall as usual. Docker
   HA has no update mechanism, so start the same run again with another
   `ha_image`; the new run replaces the old one.
6. **Cancel the run when you're done.** It does not stop on its own until
   `minutes` runs out, and it holds a runner the whole time:
   ```bash
   gh run cancel <run-id> -R <you>/ha-mcp
   ```

Anyone who has a tunnel URL has admin on that instance through the public test
credentials. Don't share the URLs, and don't put anything real in it.

## Calling through native qualification

`dev-ha-env.yml` also accepts `workflow_call`, with the same inputs and defaults
as direct dispatch (choice inputs become strings and are validated before
checkout). The caller must originate from a manual dispatch on a fork. It needs
only `contents: read`, with no `secrets: inherit` or write permissions.

On a fork where `native-migration.yml` is already registered in Actions but
`dev-ha-env.yml` dispatch returns 404, push the caller and its referenced reusable
workflows (including `native-protocol-comparison.yml`) to the working branch and
dispatch `native-migration.yml` on that branch. The local reusable
workflow is loaded from the caller's commit; it does not need a separate dispatch
registration. GitHub documents the [same-commit local reference](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#calling-a-reusable-workflow)
and the [caller event payload](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_call).
No fork default-branch update is needed for this route.

PowerShell example, after the operator has prepared the throwaway RSA-4096 key
and saved **only its base64 PEM public key** in `devenv-public-key.b64`:

```powershell
$workflowBranch = '<branch-with-the-routing-changes>'
$installRef = '<SHA-or-branch-present-in-your-fork>'
$publicKey = (Get-Content -Raw devenv-public-key.b64).Trim()
gh workflow run native-migration.yml -R julienld/ha-mcp-native --ref $workflowBranch `
  -f target=live -f "track_ref=$installRef" -f "public_key=$publicKey" `
  -f minutes=60 -f platform=docker -f server=embedded -f embedded_runtime=native
```

`--ref` selects the workflow and trusted holder/tunnel helpers; `track_ref`
selects the server, component and fixture code installed from that same fork.
Prefer an exact SHA for reproducible PR qualification. These are independent
revisions. The caller exposes `embedded_runtime` alongside `target` and `published_tag`;
live defaults are Docker, embedded Legacy server and 60 minutes. Other dev-environment
settings use the reusable workflow defaults: File & YAML Tools present, strict
best practices enabled, current stable HA, no HAOS CPU cap.

The selector requires `track_ref` and `public_key`, rejects `published_tag` for
`live` and `protocol`, and bounds live sessions to 1–330 minutes. Passing live
revision/key inputs to another target is an error. Platform/server combinations
are checked before checkout or boot. The public key is not a secret; the private
key stays with the operator and is never sent to the runner. Keep the encrypted
`dev-ha-env-urls` download/decryption and cancellation steps above. Never paste
decrypted endpoint URLs in public logs, comments or reports. The existing job
timeout, holder teardown and disposable runner lifetime still apply.

For the paired protocol bench instead:

```powershell
gh workflow run native-migration.yml -R julienld/ha-mcp-native --ref $workflowBranch -f target=protocol
```

Each target selects only its own workload: `protocol` calls the paired protocol
workflow, `live` calls this environment, and neither starts the native build/unit
jobs or HACS owner qualification. A protocol/SDK bench is not an LLM conversation:
PRs #2858/#2859 still require a separate isolated live LLM session against the
selected revision, with observations and cleanup recorded by the operator.
The routing tests provide local evidence only; they do not establish that an
endpoint booted, a tunnel was reachable, or those PR scenarios passed.
