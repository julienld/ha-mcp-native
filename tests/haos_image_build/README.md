# HAOS Test Image Build

Builds the pre-baked HAOS qcow2 used by the HAOS E2E test tier (#1281).
The image bundles a configured HAOS install with the ha-mcp addon repository
registered, a v1 set of addons installed (Frigate, ESPHome, Node-RED,
Mosquitto, Zigbee2MQTT), and HACS bootstrapped.

## Local build

Requirements:
- Linux host with `/dev/kvm` accessible
- `qemu-system-x86`, `qemu-utils`, `ovmf`, `xz-utils`, `curl`
- ~10 GB free disk in the work directory

```bash
python3 tests/haos_image_build/build_image.py --verbose \
  --work-dir /tmp/haos-build \
  --output haos-test-image.qcow2.xz
```

First boot pulls the HAOS release (~530 MB compressed), expands the data
partition, then runs onboarding and addon installs. Total wall time on a
4-vCPU runner: ~15–25 minutes depending on addon Docker pulls.

## CI build

`build-haos-test-image.yml` runs the same script on `ubuntu-22.04`. On every
run it uploads the qcow2 as a workflow artifact (reviewer sanity-check). On
`master` (push / weekly cron / manual dispatch) it additionally primes the
shared Actions cache used by all six E2E lanes. They all live in
`haos-e2e-tests.yml` as the jobs `haos-e2e`, `haos-e2e-inaddon`,
`haos-e2e-inaddon-no-tools`, `haos-e2e-embedded`,
`haos-e2e-embedded-no-tools`, and `haos-e2e-stdio`; each restores the qcow2
from that cache and falls back to a local build on a miss.

## Native fork qualification

`haos-native.yml` runs existing E2E contracts against a native server on the
public `julienld/ha-mcp-native` fork, manually or on the dedicated
`qualify/haos-native` branch. Both events default to the same targeted set,
including the common legacy/native continuity contracts. It uses the same HAOS image and
QEMU lifecycle, with `HAOS_TEST_MODE=embedded`, `E2E_EMBEDDED_RUNTIME=native`
and `E2E_NATIVE_ARTIFACTS` pointing to paired artifacts built from the exact
checkout commit. The six upstream lanes retain their legacy transports;
they now stage that same paired dependency profile for the integration.

Before boot, `haos_candidate.py` verifies artifact SHA-256 values and copies
the wheel, component and provenance into each worker's own qcow2 overlay.
Only the disposable manifest URL is changed to the local wheel with its
SHA-256 fragment. Home Assistant installs the individual requirements under
its own constraints. No release or tag is created, and no upstream token is
required. This qualifies candidate bytes on HAOS; it does not replace the
published-URL HACS installation qualification.

The native entry explicitly selects the onboarded active human owner and
does not retain an internal admin token, pip override or legacy listener
settings. A small authenticated, administrator-only test integration attests
`NativeServerManager`, its running task on Core's event loop, the candidate
version/revision, and the absence of the legacy listener and server thread.
Fixture setup fails on any mismatch before MCP tests run. The existing
Supervisor assertion additionally verifies that the server reaches real
installed HAOS apps. Runtime proof, serial/Core logs and JUnit results are
retained in the workflow artifact. No skip ceiling or test assertion is
relaxed for this lane.

The runner checks `/dev/kvm` readability/writability and reports CPU, memory
and free disk. Public `ubuntu-22.04` runners use the existing two-worker
layout (two guests at 4 GiB each). The first fork run must build its image;
the fork cannot restore the upstream repository's cache.

## Version pinning

`build_image.py` has three Renovate-managed stable inputs:

- `STABLE_HAOS_VERSION`: the promoted operating-system release from
  `https://version.home-assistant.io/stable.json` (`hassos.ova`). The GitHub
  release object appears hours before its per-board assets finish uploading,
  so the release list is not the promotion signal.
- `STABLE_SUPERVISOR_VERSION`: the promoted Supervisor version from
  `https://version.home-assistant.io/stable.json`, used as a minimum.
- `STABLE_CORE_VERSION`: the exact Core version, updated together with the
  container E2E pins.

All three dependencies bypass Renovate's ordinary seven-day age gate and
Tuesday window. The scanner runs hourly. A builder-input change invalidates
the stable image cache; the PR's HAOS E2E lanes build on a miss. Merging the PR
also triggers the master image-cache prime. Supervisor can still self-update
past its recorded minimum within the selected channel.

Beta lanes independently read `beta.json`: `hassos.ova`, `supervisor`, and
`homeassistant.qemux86-64`. They set `HAOS_BUILD_OS_VERSION`,
`HAOS_BUILD_SUPERVISOR_CHANNEL=beta`, `HAOS_BUILD_SUPERVISOR_MIN_VERSION`, and
`HAOS_BUILD_CORE_VERSION`. These environment overrides also remain available
for explicit local builds. OS prerelease versions such as `18.2.rc1` select
the corresponding release qcow2 instead of the stable OS pin.

The beta lanes do not use the Actions cache: each bakes its own qcow2 whenever it
runs, so a ~4.9 GB image never competes for the repository cache budget with the
stable qcow2 and the Home Assistant container images. Automatic beta runs skip
only when all three channel versions equal stable, so an OS-only beta release
still runs. Manual dispatch always runs, and the beta canary checks the booted
VM's OS, Supervisor channel/minimum, and exact Core version.
