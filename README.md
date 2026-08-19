# AqualinkD Validator

`aqualinkd-validator` is a containerized Python validation harness for
[AqualinkD](https://github.com/aqualinkd/AqualinkD). It currently supervises
the complete daemon through its process, logging, and HTTP interfaces, and is
intended to add serial capture and replay so timing-sensitive behavior can be
reproduced and checked consistently.

> [!IMPORTANT]
> This project is under active development. The `doctor`, `run`, `compare`,
> and `validate-testcase` commands and the PDA live-panel hardware suites are
> implemented. The live
> suites supervise AqualinkD, perform HTTP actions,
> validate PDA state and timing, restore changed equipment state, and retain
> diagnostic and performance artifacts. Versioned YAML testcase loading,
> preflight validation, and PDA live-panel execution are available; isolated PTY
> panel emulation, PCAPNG serial capture, operational RS485-log collection,
> and legacy importers remain planned under
> [issue #1](https://github.com/ballle98/aqualinkd-validator/issues/1).

Developer setup, test-extension rules, safety expectations, and the
declarative testcase format are documented in [CONTRIBUTING.md](CONTRIBUTING.md).
The wiki contains the
[high-level design](https://github.com/ballle98/aqualinkd-validator/wiki/High-Level-Design).

## Installation and first test

### Prerequisites

To run a live-panel suite, prepare all of the following:

- A Linux host, currently tested on 64-bit Raspberry Pi OS/Debian 13.
- Docker Engine for the canonical container workflow, or Python 3.11 or newer
  for direct development use.
- A separately built AqualinkD binary compatible with the test host.
- A reviewed AqualinkD configuration for the connected panel. Its active
  `serial_port` must name the exact character device passed to the validator,
  and its `web_directory` must exist in the runtime environment.
- Exclusive access to that serial device. Stop the installed AqualinkD
  service before testing and arrange to restart it afterward.
- Knowledge of what every switch exposed by `/api/devices` physically
  controls. `--panel-read-write` authorizes the selected suite to operate the
  equipment covered by its cases. The awake and long suites operate all
  discovered switches unless an explicit consecutive-device restriction is
  used.

Live PDA suites operate real equipment. Do not use them while maintenance is
in progress, valves are in an unsafe position, water level is unsuitable, or
equipment operation would otherwise be hazardous. Freeze-protection changes
and service-mode entry are intentionally excluded.

The recommended first run uses Docker on the same Raspberry Pi as the
installed AqualinkD service. If Docker is not installed, follow
[Install Docker Engine on a 64-bit Raspberry Pi](#install-docker-engine-on-a-64-bit-raspberry-pi)
first.

### Build the validator image

Run these commands on the Pi:

```sh
git clone https://github.com/ballle98/aqualinkd-validator.git
cd aqualinkd-validator
VALIDATOR_COMMIT=$(git rev-parse HEAD)
sudo env BUILDX_GIT_INFO=0 docker build \
  --build-arg "VCS_REF=$VALIDATOR_COMMIT" \
  -t aqualinkd-validator:local .
sudo docker run --rm aqualinkd-validator:local doctor
```

`sudo docker build` runs Buildx as root. On a Pi-owned checkout, Git's safe
ownership check can prevent Buildx from detecting the commit and produce a
harmless `current commit information was not captured` warning. The command
above reads the commit as the checkout owner, passes it into the image's
revision label, and disables only Buildx's automatic Git provenance lookup.
See Docker's [`BUILDX_GIT_INFO` documentation](https://docs.docker.com/build/building/variables/#buildx_git_info).

The image contains the Python validator, not AqualinkD. The installed binary,
configuration, web directory, serial device, and artifact directory are
provided as runtime mounts. Building the image does not copy or reserve
anything under `/tmp`.

### First test: installed AqualinkD

This example assumes the normal Pi installation:

- `/usr/local/bin/aqualinkd`
- `/etc/aqualinkd.conf`
- `serial_port=/dev/ttyUSB0`
- `web_directory=/var/www/aqualinkd/`
- panel timezone `America/Chicago`

Adjust the device, web directory, and timezone if the installed configuration
differs. The validator reads `serial_port` from the mounted configuration and
verifies that it matches the mapped character device. AqualinkD needs its
configured web directory mounted because the child process sees only the
container filesystem.

Stop the service, run the fast suite, and restore the service:

```sh
# stop aqualinkd service
sudo systemctl stop aqualinkd

#run validator
sudo docker run --rm \
  --env TZ=America/Chicago \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  --mount type=bind,source=/usr/local/bin/aqualinkd,target=/usr/local/bin/aqualinkd,readonly \
  --mount type=bind,source=/etc/aqualinkd.conf,target=/etc/aqualinkd.conf,readonly \
  --mount type=bind,source=/var/www/aqualinkd,target=/var/www/aqualinkd \
  --volume /home/pi/aqualinkd-validator-artifacts:/tmp/aqualinkd-validator-artifacts \
  aqualinkd-validator:local run \
    --panel-read-write \
    pda-live-fast

# start aqualinkd service
sudo systemctl start aqualinkd
```

Do not continue if another AqualinkD process still owns the serial device
after the service is stopped. The validator supervises a new AqualinkD process
so it can capture stdout/stderr and metrics; it does not attach to the running
systemd process. It stops only its child and does not restart the service.

The validator defaults to `/tmp/aqualinkd-validator-artifacts` and creates it
when needed. The volume above maps that container path to the persistent host
directory `/home/pi/aqualinkd-validator-artifacts`; Docker creates the host
directory if it is absent. The host directory can be elsewhere without adding
a validator option—change only the left side of `--volume`.

### Testing an AqualinkD build staged under `/tmp`

The same validator image can test a development binary without installing it.
Rebuild the image only when validator code changes. When AqualinkD changes,
stage its binary, configuration, and web files on the Pi:

```sh
sudo install -m 0755 ~/git/AqualinkD/release/aqualinkd /tmp/aqualinkd
sudo cp -n /etc/aqualinkd.conf /tmp/aqualinkd.conf
sudo mkdir -p /tmp/aqualinkd-web
sudo cp -a ~/git/AqualinkD/web/. /tmp/aqualinkd-web/
sudo sed -i -E \
  's|^[[:space:]]*web_directory[[:space:]]*=.*$|web_directory=/tmp/aqualinkd-web/|' \
  /tmp/aqualinkd.conf
```

Review `/tmp/aqualinkd.conf` before using it. In particular, confirm its
`serial_port`, PDA settings, HTTP listener, and any credentials or external
integrations. Then stop the installed service and run the staged SUT:

```sh
# stop aqualinkd service
sudo systemctl stop aqualinkd

# run validator
sudo docker run --rm \
  --env TZ=America/Chicago \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  --mount type=bind,source=/tmp/aqualinkd,target=/usr/local/bin/aqualinkd,readonly \
  --mount type=bind,source=/tmp/aqualinkd.conf,target=/etc/aqualinkd.conf,readonly \
  --mount type=bind,source=/tmp/aqualinkd-web,target=/tmp/aqualinkd-web \
  --volume /tmp/aqualinkd-validator-artifacts:/tmp/aqualinkd-validator-artifacts \
  aqualinkd-validator:local run \
    --panel-read-write \
    pda-live-fast

#start aqualinkd service
sudo systemctl start aqualinkd
```

The staged binary and configuration are mounted at the validator defaults,
so no path options are needed. `/tmp` is only a convenient host staging area
for this SUT. Results use `/tmp/aqualinkd-validator-artifacts` on both the Pi
and in the container; move the host side of the volume to a persistent path
when results must survive a reboot.

### Running without Docker on a constrained Pi

On a Pi Zero or another resource-constrained system, run the validator
directly to avoid the container image and Docker daemon. The package has no
native runtime dependencies; pip installs the pinned YAML parser:

```sh
git clone https://github.com/ballle98/aqualinkd-validator.git
cd aqualinkd-validator
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/aqualinkd-validator doctor

sudo systemctl stop aqualinkd
pgrep -a aqualinkd
sudo .venv/bin/aqualinkd-validator run \
  --panel-read-write \
  pda-live-fast
validator_status=$?
sudo systemctl start aqualinkd
printf 'validator exit status: %s\n' "$validator_status"
```

Direct mode defaults to `/usr/local/bin/aqualinkd`,
`/etc/aqualinkd.conf`, that configuration's `serial_port`, and the system's
local IANA timezone. It creates artifacts under
`/tmp/aqualinkd-validator-artifacts`. Use `--aqualinkd`, `--config`,
`--serial-device`, `--panel-timezone`, or `--artifacts` only for overrides.
Because AqualinkD runs directly on the host, its configured web directory is
already visible and needs no mount.

Direct execution still adds the Python supervisor, log writing, and `/proc`
sampling overhead, but it does not require a Docker daemon or container
filesystem. Files created through `sudo` may need their ownership corrected
afterward.

For PDA suites, the validator reads either the current AqualinkD
`Starting web server on <URL>` startup log or the ballle98 2.3.7
`Starting web server on port <N>` form. It uses the reported port, including
non-default ports such as `8080`, without requiring `--api-base-url`. Both
timestamped and non-timestamped log forms are accepted. The effective
configuration is fingerprinted but not copied into artifacts, avoiding
accidental publication of credentials. These are live-panel
integration/regression tests rather than unit tests.

### Pool-specific validator settings

Installation-specific physical behavior belongs in a validator site profile,
not in reusable testcases or additional routine command-line options. By
default the validator looks for `aqualinkd-validator.yaml` beside the active
`aqualinkd.conf`. The file is optional for the normal fast, awake, sleep, and
long suites. Start with the supplied example when enabling spa tests:

```sh
sudo cp examples/aqualinkd-validator.yaml /etc/aqualinkd-validator.yaml
sudo editor /etc/aqualinkd-validator.yaml
```

```yaml
schema: 1
spa:
  fill_time: 8m
```

`spa.fill_time` is the time this installation needs to circulate in Pool mode
before switching to Spa mode. It is required only by the opt-in
`pda-live-spa` suite. An explicit `--site-config` path is available for an
unusual layout, but is not needed when the file is beside `aqualinkd.conf`.

For Docker, mount the profile at its default container path and run the
separate suite:

```sh
sudo docker run --rm \
  --env TZ=America/Chicago \
  --device /dev/ttyUSB0:/dev/ttyUSB0 \
  --mount type=bind,source=/usr/local/bin/aqualinkd,target=/usr/local/bin/aqualinkd,readonly \
  --mount type=bind,source=/etc/aqualinkd.conf,target=/etc/aqualinkd.conf,readonly \
  --mount type=bind,source=/etc/aqualinkd-validator.yaml,target=/etc/aqualinkd-validator.yaml,readonly \
  --mount type=bind,source=/var/www/aqualinkd,target=/var/www/aqualinkd \
  --volume /home/pi/aqualinkd-validator-artifacts:/tmp/aqualinkd-validator-artifacts \
  aqualinkd-validator:local run \
    --panel-read-write \
    pda-live-spa
```

The suite fills in Pool mode, enters Spa mode, raises the Spa Heater setpoint
only enough to demand heat, verifies active heating, turns heat off, validates
the delayed cooldown transition, and restores the original setpoint and
equipment state. It is intentionally excluded from `pda-live-long`, since it
can take many minutes and changes water routing. A pool-only panel reports the
case as skipped.

### PDA live-panel suites

The live-panel coverage is assembled from reusable cases, process suites, and
one composite suite:

| Suite | Intended use | Coverage |
| --- | --- | --- |
| `pda-live-fast` | Default development regression | Initialization, panel identity and clock, filter-pump round trip, and optional non-heating pool-heater control checks |
| `pda-live-awake` | Awake-state diagnostics or focused reruns | Fast cases plus maximum safe equipment-status reconciliation and consecutive-device operations, with PDA sleep disabled |
| `pda-live-sleep` | Sleep-state diagnostics or focused reruns | Initialization, one natural sleep/wake duty cycle, and switch round trips during STATUS retries and after probing begins |
| `pda-live-long` | Complete state-dependent regression | Composite suite that serially runs `pda-live-awake` and `pda-live-sleep` in separate AqualinkD processes |
| `pda-live-spa` | Explicit hydraulic/heating validation | Site-configured Pool-mode fill, Spa-mode entry, active Spa Heater demand, cooldown/lockout, and full restoration; not included in `pda-live-long` |
| `pda-live-simulator` | AquaPDA interface transport regression on a physical panel | Opens the same northbound AquaPDA WebSocket as `aquapda_sim.html`, observes at least 20 PDA packets, sends a read-only Back key, and fails on slow ACKs, bad checksums, BAD PACKET messages, or resulting navigation failures |
| `pda-live-simulator-menu-walk` | Extensive AquaPDA interface-emulator navigation against a physical panel | Runs the AquaPDA transport regression, reconstructs the PDA display, walks the full Equipment On/Off list, and recursively visits every read-only submenu advertised with `>` without selecting equipment or setting actions |
| `pda-powercenter-simulator-menu-walk` | Extensive validation against the Jandy Power Center emulator | Performs the same AquaPDA interface-emulator traversal with the Power Center emulator acting as AqualinkD's southbound panel |

Multiple positional suites are serialized. For example,
`run --panel-read-write pda-live-fast pda-live-long` completes the fast suite,
stops its supervised AqualinkD process, and writes its artifacts before
starting a new AqualinkD process for the long suite. Independently supplied
positional suites stop at the first failure, and suites never compete for the
serial bus.

YAML testcase paths use the same positional run-target mechanism and can be
mixed or serialized without Python registration. The included first testcase
performs the filter-pump portion of the fast suite:

```bash
.venv/bin/aqualinkd-validator run --panel-read-write \
  testcases/pda/filter-after-init.yaml
```

The optional pool-heater policy is also declarative:

```bash
.venv/bin/aqualinkd-validator run --panel-read-write \
  testcases/pda/pool-heater.yaml
```

The complete YAML file is validated before AqualinkD starts. Its declared
`read-only` or `read-write` access is enforced against `--panel` or `--panelw`,
and PDA testcases automatically enable AqualinkD serial debug logging with
`-vv`. A mutating testcase must declare `restore_original_state` in `finally`;
the runtime also makes a final safety-restoration attempt after failure or
cancellation.

`pda-live-long` itself uses two serialized AqualinkD processes because the
equipment-status and sleep cases require opposite `pda_sleep_mode` settings.
The validator copies the selected configuration to private temporary files,
appends `pda_sleep_mode = no` for `pda-live-awake` and
`pda_sleep_mode = yes` for `pda-live-sleep`, and passes each file through
`aqualinkd -c`. The supplied configuration remains unchanged. Temporary
configurations are removed after the command and are not copied into
artifacts, where they could expose credentials. Each manifest instead records
the source configuration fingerprint, effective derived fingerprint, and the
single applied override.

All process suites start AqualinkD in the foreground with `-vv`, enabling
`DEBUG_SERIAL` logging in addition to the normal validator arguments:

```text
aqualinkd -d -c /path/to/aqualinkd.conf -vv
```

The focused transport regression for
[ballle98/AqualinkD#94](https://github.com/ballle98/AqualinkD/issues/94) and
[ballle98/AqualinkD#95](https://github.com/ballle98/AqualinkD/issues/95) are
read-only and require only panel-access authorization:

```bash
.venv/bin/aqualinkd-validator run --panel pda-live-simulator
```

The test opens `simulator/aquapda` directly, without launching a browser. It
keeps the WebSocket connected while checking the supervised `-vv` log for
`Serial read bad Jandy checksum`, `BAD PACKET`, known follow-on navigation
failures, and any logged receive-to-send time over 10 ms. Packet counts and
timing samples are retained in `scenario.json`.

To extensively exercise AqualinkD's AquaPDA interface emulator against a
physical panel, run the read-only menu walk:

```bash
.venv/bin/aqualinkd-validator run \
  --panel \
  pda-live-simulator-menu-walk
```

For the corresponding southbound panel-emulation test, connect AqualinkD's
serial port to the Jandy **Power Center emulator**, then run:

```bash
.venv/bin/aqualinkd-validator run \
  --mode jandy-simulator \
  --panel \
  pda-powercenter-simulator-menu-walk
```

The AquaPDA WebSocket is the northbound key/display interface; the Jandy Power
Center emulator remains the southbound panel. The crawler reconstructs
Clear, Long Message, Highlight, and Shift Lines packets, enumerates paged
highlight choices, walks the paged `EQUIPMENT ON/OFF` list, enters structural
submenu rows ending in `>`, and returns with Back. It records each path and
screen in `scenario.json`. Leaf actions
such as equipment toggles, SAVE, START, and setpoint changes are deliberately
not selected, so action-oriented interface-emulator cases can add explicit
assertions without making the structural crawl unsafe on a real panel.

The container console reports each phase as it runs, for example:

```text
[ RUN  ] PDA initialization, identity, and clock
[STATE ] Waiting on control-panel probe
[STATE ] Control-panel probe received
[STATE ] Init PDA started
[STATE ] Init PDA complete
[INFO  ] AqualinkD version: v2.3.7 (rev dbfcb39)
[INFO  ] Configured panel: PDA-8 Combo Pool/Spa
[INFO  ] Panel reported: PDA-PS6 Combo; firmware PDA: 7.1.0
[ WARN ] Configured panel type does not match the physical panel: configured PDA-8 Combo Pool/Spa; reported PDA-PS6 Combo
[ PASS ] PDA initialization, identity, and clock completed in 18.427s
[ RUN  ] Filter pump after initialization
[ PASS ] Filter pump after initialization completed in 4.913s
```

Failures include the error after the elapsed time, optional cases emit
`[ SKIP ]`, and the suite finishes with an overall pass or fail line. Output
is flushed immediately so it remains visible through Docker, SSH, and the VS
Code task terminal. AqualinkD warning, error, critical, and fatal messages are
also forwarded immediately with an `[AQUALINKD ...]` prefix; any otherwise
unclassified stderr line is forwarded as `[AQUALINKD STDERR]`. Forwarding
does not remove diagnostics from the artifact logs or timeline.

The fast suite performs these phases:

1. Wait for the PDA programmer to become active after the panel probe and for
   `(Init PDA) finished`, recording activation wait and active runtime
   separately. Capture the AqualinkD version and configured panel type from
   startup logs, and the physical panel type and firmware directly from the
   PDA firmware-version screen. A normalized family/capacity mismatch is
   recorded and printed as a warning, while all raw identity strings are
   retained for comparisons.
2. Allow initialization-time clock synchronization to settle, then check that
   the panel clock is within 120 seconds of the system clock in the local
   timezone, or the explicit `--panel-timezone` override.
3. Toggle the filter pump after initialization and restore its original
   state.
4. If a pool heater is present and is not already actively heating, disable
   it if necessary, exercise adjacent setpoints at the supported minimum,
   enable it only at that non-heating setpoint, verify it remains enabled but
   inactive, then disable it and restore its original setpoint and enabled
   state. The case is skipped when the water temperature cannot prove those
   minimum setpoints are below the current water temperature.

The long suite composes the following two process suites:

Before operating a discovered switch, the validator cross-checks three
sources: the effective `button_??_label` assignments in `--config`, the
device name returned by `/api/devices`, and the panel size reported by the
physical PDA firmware screen. A switch is reported and skipped when its
configured or API name is `NONE`. `Aux_N` is also skipped when `N` is equal
to or greater than the reported panel size; for example, a PDA-PS6 permits
Aux 1 through Aux 5 and excludes Aux 6 and above. This protects against a
configuration declaring a larger panel than the connected hardware.

1. `pda-live-awake`, with `pda_sleep_mode = no`: run the fast checks, lower
   Pool and Spa Heater setpoints to their supported minimum, then enable the
   maximum safe set of configured controls while leaving Spa mode and Solar
   Heater unchanged. Verify that neither enabled heater becomes active, wait
   for the PDA to return home,
   and capture a complete multi-page `EQUIPMENT STATUS` loop. Verify every
   expected device appeared and remained on in `/api/devices` after AqualinkD
   reconciled the full loop. If an SWG is present, verify its status was
   captured and its reported percentage agrees with the API. Then exercise
   consecutive device operations and restore them in reverse order.
   Supplying repeated `--pda-test-device` options restricts the separate
   consecutive-device operation to those switch IDs.
   Spa hydraulics and deliberate active heating are isolated in
   `pda-live-spa`, so this general regression does not unexpectedly route
   water, fire a heater, or incur cooldown delays. Cleanup safety-disables a
   test-enabled heater before restoring its normal setpoint.
2. `pda-live-sleep`, with `pda_sleep_mode = yes`: restart AqualinkD, repeat PDA
   initialization and identity checks, wait for AqualinkD's PDA sleep marker,
   observe one complete natural sleep/wake cycle, then toggle a switch about
   one second into the panel's unanswered STATUS retries and again after the
   panel starts probing the PDA address. By default the highest-numbered
   eligible auxiliary is selected to exercise the deepest available
   equipment-menu navigation; Filter Pump is used only when no auxiliary is
   actionable. Use
   `--pda-test-device ID` to select a specific switch. The natural cycle records time
   asleep, the `PDA init after wake` equipment-status refresh, the delay from
   status completion until the PDA sleeps again, total cycle duration, and
   awake/sleep duty-cycle percentages.

Each mutating case attempts to restore the equipment it changed before the
next case begins. A case assertion failure is retained in the final result,
but later cases continue when restoration succeeds. If restoration cannot be
verified, the current process stops and a composite suite does not start its
next member. Consequently, `pda-live-long` can still collect sleep coverage
after an awake assertion failure when the panel was safely restored. Each
restart includes a separately measured PDA initialization because the panel
may need to exhaust acknowledgements and probe the emulated PDA again. Run
`pda-live-awake` or `pda-live-sleep` directly for focused diagnostics; no
phase-selection option is needed.

Freeze-protection mutation and service-mode entry are deliberately excluded
from both live-panel suites. They affect safety or maintenance behavior and
belong in future RS485 panel-emulator or capture-replay suites where no physical
equipment is connected. Read-only observation of those states may be added to
live suites without changing this policy.

Each physical action records separate timings for HTTP acknowledgement,
HTTP-request-to-programmer activation, active programmer runtime, API state
convergence after programmer completion, and total end-to-end time. PDA
initialization similarly separates the wait for a panel probe and task
activation from the active `Init PDA` runtime. Console `[ WAIT ]`, `[ACTIVE]`,
`[ DONE ]`, and `[STATE ]` lines expose these transitions while the test is
running. `scenario.json` contains the panel identity, clock check,
measurements, sleep-cycle duty-cycle summary, skipped optional cases, and
restoration report; the same data is embedded in `performance.json`.

The scenario snapshots every state it may change and performs best-effort
restoration after success, failure, timeout, or interruption. A restoration
failure fails the run. Cleanup removes heater demand first, restores ordinary
auxiliaries, restores SPA mode, and handles the filter pump last. This lets
panel-controlled heater and pump cooldown finish without violating equipment
dependencies. If the API reports an off request as `state=off` with a
flashing/pending status, cleanup waits instead of sending another PDA toggle.
It likewise never blindly resends a restoration toggle after a timeout; this
prevents stale API state from reversing a physical change that already
completed. The suite exits when its scenario passes or fails;
`--pda-init-timeout` (180 seconds by default) applies separately to
initialization activation and completion. `--pda-activation-timeout` defaults
to 130 seconds so it exceeds AqualinkD's 120-second programmer-queue wait,
`--pda-action-timeout` defaults to 90 seconds and starts when that task becomes
active, and `--pda-state-timeout` allows 10 seconds for API convergence after
it finishes. A matching PDA programmer error in the log fails the action
immediately instead of waiting for the state timeout.
`--pda-sleep-timeout` controls state waits. Delayed restoration and restoration
after cancellation use `--pda-cleanup-timeout`, which defaults to 300 seconds
to accommodate SPA/heater pump-delay cycles. On cancellation, the daemon is
kept alive for that interval so cleanup can finish before it is terminated.
Override the clock tolerance with
`--panel-time-tolerance` when the panel requires a wider bound.

The high-volume `-vv` diagnostics are captured in `stdout.log` and
`stderr.log`, and every line is timestamped against the monotonic run clock
in `timeline.jsonl`. The manifest records the selected suite, effective
AqualinkD command and reported version, configured panel identity, API origin,
execution role and config override, requested device restriction, and the
resolved set of discovered switches.

Every current run creates a unique artifact directory containing:

```text
<timestamp>-<label>/
├── manifest.yaml
├── metrics.jsonl
├── performance.json
├── result.json
├── scenario.json
├── stderr.log
├── stdout.log
├── summary.log
└── timeline.jsonl
```

`scenario.json` is present for a selected PDA suite. `manifest.yaml` currently
contains JSON-compatible structured data despite its filename. Preserve the
entire directory when reporting a failure.

`summary.log` is a concise copy of validator console output: suite and member
headers, state transitions, action timings, skips, warnings, errors, and the
final result. It excludes the routine high-volume AqualinkD stdout stream,
which remains available in `stdout.log`; daemon warnings and errors forwarded
to the console are retained in the summary.

`pda-live-long` creates two such directories with `-awake` and `-sleep` label
suffixes. Compare awake results only with other awake results, and sleep
results only with other sleep results; the comparison command warns when
execution roles differ.

Pure unit tests remain appropriate for isolated AqualinkD C functions.

## Raspberry Pi container

The reference image uses the official multi-architecture Python 3.12 Trixie
base and targets 64-bit Raspberry Pi OS (`linux/arm64`) as well as
`linux/amd64`. Using the same Debian generation as the tested Pi allows the
container to run AqualinkD binaries linked against the Pi's Trixie sysroot.

### Install Docker Engine on a 64-bit Raspberry Pi

The tested Pi is `aarch64` and runs Debian 13 (`trixie`). Docker officially
supports Debian 13 on arm64. Use the
[official Debian installation method](https://docs.docker.com/engine/install/debian/)
for 64-bit Raspberry Pi systems, not the separate 32-bit Raspberry Pi OS
instructions.

Confirm the architecture and Debian codename:

```sh
uname -m
dpkg --print-architecture
. /etc/os-release
echo "$ID $VERSION_CODENAME"
```

The expected values on the tested Pi are `aarch64`, `arm64`, and
`debian trixie`. Stop here if the architecture is 32-bit (`armhf`); current
Docker support and images differ for that platform.

Install the repository prerequisites and Docker's official signing key:

```sh
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Add Docker's Debian repository:

```sh
sudo tee /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: $(. /etc/os-release && echo "$VERSION_CODENAME")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt update
```

If Docker, Podman compatibility packages, `containerd`, or `runc` were
previously installed from another repository, review Docker's official
conflicting-package instructions before continuing. Do not remove packages
blindly on a host already running containers.

Install Docker Engine, Buildx, and the Compose plugin:

```sh
sudo apt install \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

Docker normally starts automatically on Debian. Verify its status as described
in Docker's installation instructions:

```sh
sudo systemctl status docker
```

If Docker is not running, start it with `sudo systemctl start docker`. Then
complete Docker's installation check and verify the installed Compose plugin.
Compose is convenient for future workflows but is not required by the current
single-container commands:

```sh
sudo docker run hello-world
sudo docker compose version
```

Use `sudo docker` initially. Adding a user to the `docker` group grants
root-equivalent control over the host; it is a convenience, not a normal
unprivileged permission. If that tradeoff is acceptable on a dedicated
development Pi, enable it explicitly and then log out and back in:

```sh
sudo usermod -aG docker "$USER"
```

Docker can change host firewall behavior, and published container ports may
bypass `ufw` or `firewalld` rules. Review Docker's
[firewall limitations](https://docs.docker.com/engine/install/debian/#firewall-limitations)
before exposing the Pi outside a trusted network. The validator maps only the
required RS485 device with `--device`; it does not require `--privileged`.

### Container behavior and overhead

The AqualinkD binary must match the Pi architecture. The binary,
configuration, configured web directory, serial character device, and artifact
directory are runtime inputs; they are not included in the validator image.
The recorded binary SHA-256 is the authoritative executable identity.

Containers share the host kernel, so CPU execution does not incur virtual
machine emulation overhead. The measurable overhead comes primarily from the
Python supervisor, log writes, and `/proc` sampling. Sampling defaults to once
per second and occurs outside AqualinkD. For fair comparisons, use the same
container, suite, per-phase timeouts, sampling interval, capture options, and
configuration for every revision.

### Parallel WSL and Raspberry Pi development

This subsection describes maintainer automation stored in a separate dotfiles
checkout; the helper and workspace file are not installed by this repository.
New users can run the complete public workflow with the Docker commands above.

The maintained multi-root VS Code workspace opens AqualinkD and
`aqualinkd-validator` together. Its **AqualinkD + Validator: pda-live-fast on
staged Pi** and **AqualinkD + Validator: pda-live-long on staged Pi** tasks
implement this staged-development loop:

1. Cross-build the current AqualinkD working tree for arm64.
2. Stage its binary, web files, and test configuration under `/tmp` on the Pi.
3. Synchronize the current validator working tree to
   `/tmp/aqualinkd-validator-src`.
4. Rebuild `aqualinkd-validator:dev` natively on the Pi. Docker reuses
   unchanged layers between runs.
5. Stop the installed `aqualinkd` service and refuse to continue if another
   AqualinkD process still owns the serial path.
6. Run `/tmp/aqualinkd -c /tmp/aqualinkd.conf` under the validator in one
   container with only the configured serial device mapped. The web directory
   is read from that staged configuration and mounted at the same path.
7. Write artifacts to `/tmp/aqualinkd-validator-artifacts`, remove the test
   container, and restore the installed service even when the test fails or
   is interrupted.

Choose the task for the desired suite. Both run until their scenario passes or
fails; there is no runtime prompt. The long task operates every switch
discovered through `/api/devices`; running it with `--panel-read-write`
explicitly grants that permission. A manual CLI invocation can use repeated
`--pda-test-device` options to restrict the consecutive-device phase. The
Pi's `/tmp` directory is also opened in the workspace, so completed artifacts
are available under `pi-tmp/aqualinkd-validator-artifacts`.

The **AqualinkD + Validator: pda-live-fast on installed Pi** task does not
cross-build or deploy AqualinkD. It stops the installed service and supervises
`/usr/local/bin/aqualinkd -c /etc/aqualinkd.conf` in the container. It reads
and mounts the installed configuration's `serial_port` and `web_directory`,
then restores the service afterward. This tests the installed system without
allowing two AqualinkD processes to use the panel simultaneously.

The Pi helper reads the host's IANA timezone from `timedatectl` and sets the
container's `TZ` environment. AqualinkD uses that local timezone, and the
validator infers the same value for its panel-clock check.

The deployment helper creates `/tmp/aqualinkd.conf` from the checkout only
when the remote test configuration does not already exist. It also preserves
the writable web `config.json`. This prevents normal code iterations from
overwriting Pi-specific test settings; update the staged configuration
deliberately when a test requires a configuration change.

The validator image contains the Python harness but not a fixed AqualinkD
build. The task bind-mounts the newly staged `/tmp/aqualinkd`, allowing the C
daemon and Python validator to be developed independently without rebuilding
an AqualinkD-specific image. Dirty working trees are staged for development;
the manifest records the source commit and authoritative binary hash, while
the task labels dirty builds explicitly.

This task changes a live remote system: it stops the installed service and
opens the configured RS485 device. Review the selected SSH host, configuration,
serial path, heater settings, and every switch exposed by `/api/devices`
before running it. The PDA suite performs HTTP actions and timing assertions.
Raw serial PCAPNG capture and packet-level assertions remain planned.

## Comparing AqualinkD revisions

The immediate comparison targets are the `rel/2.3.7`, `upstream/master`, and
`merge-pda-3.1.x` Git refs. Build each in a separate worktree on the same Pi
using identical compiler options. Run the same scenario multiple times,
rotate the revision order, and allow the panel and Pi temperature to return
to a comparable state between runs.

Each run records CPU model, governor, temperature when exposed through sysfs,
kernel, configuration hash, binary hash, source commit, CPU time, RSS, thread
count, context switches, and process I/O. Compare two or more completed runs:

```sh
aqualinkd-validator compare \
  artifacts/20260727T120000Z-rel-2.3.7 \
  artifacts/20260727T121500Z-upstream-master \
  artifacts/20260727T123000Z-merge-pda-3.1.x
```

The comparison warns when the architecture, CPU model, kernel, container
runtime, configuration fingerprint, suite, selected PDA devices, or sampling
interval differs. It also compares the panel type and firmware captured from
the PDA initialization screen. PDA suite comparisons include a per-action
timing table in milliseconds alongside process CPU and memory measurements.
Missing actions are shown as `n/a`, making skipped or incomplete phases
visible.

## Why this project exists

AqualinkD interacts with hardware whose behavior depends on protocol state,
message ordering, and timing. Many useful checks therefore require more than
isolated C unit tests. Examples include:

- waiting for a PDA interface to sleep before sending a command;
- verifying navigation through PDA or OneTouch menus;
- checking that an HTTP request produces the expected serial response;
- replaying a failure captured from a real control panel;
- detecting unexpected errors in AqualinkD's stdout or stderr; and
- confirming that reported HTTP state eventually matches panel state.

The validator is intended to make those checks repeatable while retaining the
logs and serial evidence needed to diagnose a failure.

## Operating modes and roadmap

### Live-panel mode

The implemented live-panel mode starts AqualinkD connected to a real control
panel. The PDA suites perform HTTP actions, wait for state changes, inspect
logs, and enforce timing expectations.

Live-panel tests require an explicit read-only or read-write panel-access
authorization. The validator reads the serial device from the selected
AqualinkD configuration and reports it before starting the daemon; an explicit
override must match that configuration.

### Jandy Power Center emulator mode

The implemented `jandy-simulator` compatibility mode supervises AqualinkD while
it is connected to Jandy's legacy Alwin32 Power Center emulator through an
externally managed virtual or physical serial link. The
`pda-powercenter-simulator-menu-walk` suite uses AqualinkD's AquaPDA WebSocket
to drive PDA keys and reconstruct the screen while the Windows application
supplies the controller-facing serial conversation. The separate
`pda-live-simulator-menu-walk` suite uses that same AqualinkD AquaPDA
interface-emulation protocol with a physical panel on the southbound serial
connection.

This mode is distinct from AqualinkD's northbound browser-based AllButton,
OneTouch, and AquaPDA interface emulators. The validator implements the
AquaPDA browser protocol directly, but it does not replace the southbound
Power Center emulator.

### Emulator terminology

The word *simulator* is otherwise too ambiguous for this project. Documentation
uses **Jandy Power Center emulator** for Alwin32 `Pwrcntr.exe`, **AqualinkD
interface emulator** for the AquaPDA, AllButton, and OneTouch browser/WebSocket
interfaces, and **RS485 panel emulator** for the planned Python component that
will replay captures or model a panel through a PTY. Existing CLI, suite, and
module names containing `simulator` remain compatibility identifiers.

### Panel-free mode

The planned panel-free mode creates a pseudo-terminal (PTY) pair and configures
AqualinkD to use the slave PTY as its `serial_port`. A panel driver uses the
master side to:

- inject captured or scripted RS485 packets;
- preserve, scale, or deliberately perturb packet timing;
- read ACKs and commands written by AqualinkD; and
- choose subsequent panel messages based on AqualinkD's responses.

This mode is interactive by design. A one-way byte replay is insufficient for
many scenarios because AqualinkD writes responses to the serial bus and later
panel messages may depend on them.

The first panel-free milestone is deliberately a bounded feasibility test: a
minimal probe/ACK exchange followed by one end-to-end HTTP action and expected
serial response. A complete stateful panel emulator will only be pursued if
that experiment proves useful.

## Intended architecture

The first implementation is expected to use Python 3.11 or newer and
`asyncio` to coordinate:

- the foreground `aqualinkd -d` subprocess;
- concurrent stdout and stderr readers;
- HTTP requests and status polling;
- PTY reads and writes;
- scenario deadlines and timing assertions; and
- artifact collection and process cleanup.

The main components are expected to be:

| Component | Responsibility |
| --- | --- |
| Process supervisor | Start AqualinkD, stream logs, detect early exit, and stop it cleanly |
| Config builder | Produce an isolated temporary configuration with a PTY and unused HTTP port |
| HTTP client | Perform actions and poll `/api/status` or `/api/devices` |
| Serial transport | Create PTYs and read or write raw RS485 traffic |
| RS485 panel emulator | Replay captures and implement stateful panel behavior |
| Scenario runner | Execute declarative steps with monotonic deadlines |
| Assertions | Check HTTP state, logs, serial packets, timing, and process status |
| Capture pipeline | Import legacy captures and write PCAPNG, JSONL, and provenance metadata |
| Artifact writer | Save capture bundles, generated config, logs, and reports |

The canonical supported runtime will be a container with a pinned Python
version and locked dependencies. It should run panel-free tests without broad
host privileges and write artifacts to a mounted directory. Live-panel and
Jandy Power Center emulator modes must map only explicitly selected serial
devices rather than requiring an unrestricted privileged container.

A local Python virtual environment may be documented as a development
convenience, but it is not the reproducibility boundary: it isolates Python
packages, not the interpreter, native libraries, processes, devices, or
AqualinkD runtime dependencies.

## Scenario format

Scenarios are expected to be declarative YAML with explicit timeouts. A
possible PDA scenario might look like:

```yaml
name: pda-wakes-for-filter-pump-request
mode: replay
panel: pda
capture: captures/pda/idle/serial.pcapng

steps:
  - wait_http_ready:
      timeout: 10s

  - wait_log:
      contains: "Got probe"
      timeout: 10s

  - wait:
      duration: 31s
      reason: Allow the PDA interface to enter sleep mode

  - http:
      method: PUT
      path: /api/Filter_Pump/set
      value: 1

  - expect_serial:
      command: PDA_KEY
      key: SELECT
      timeout: 5s

  - wait_status:
      path: Filter_Pump
      equals: "on"
      timeout: 15s

  - assert_no_log:
      level: error
```

The schema will evolve as real scenarios expose which primitives are useful.
Initial support should favor a small set of composable steps rather than
protocol-specific behavior embedded directly in the scenario runner.

## Planned capture bundle and formats

Future capture-enabled validation runs should produce a versioned bundle:

```text
run-<id>/
├── manifest.yaml
├── serial.pcapng
├── timeline.jsonl
├── stdout.log
├── stderr.log
├── scenario.json
├── http.jsonl
├── effective-aqualinkd.conf
└── result.json
```

`serial.pcapng` is the canonical serial-analysis artifact. Each complete
RS485 frame is represented as a timestamped packet with a small versioned
pseudo-header containing direction, capture point, and framing/checksum
status. PCAPNG provides a path to Wireshark inspection while retaining the
unmodified RS485 frame as packet data.

`timeline.jsonl` remains the inspectable and diffable view that combines
serial traffic with HTTP requests, process output, and scenario events:

```json
{"offset_ns":0,"direction":"panel_to_aqualinkd","data":"1002600200621003"}
{"offset_ns":28000000,"direction":"aqualinkd_to_panel","data":"00100200010000131003"}
```

Capture timestamps should originate from a monotonic clock. Replay uses
timestamp deltas rather than wall-clock values. The manifest records the panel
model and revision, AqualinkD version/commit, configuration, capture source,
sanitization notes, and fidelity of framing, direction, and timing.

Legacy importers must preserve what their source contains without fabricating
missing information:

| Source | Framing | Direction | Timing |
| --- | --- | --- | --- |
| `/tmp/RS485.log` | Decoded packets | Read/write | Unavailable |
| `/tmp/RS485raw.log` | Must be reconstructed | Received only | Unavailable |
| `rs485mon -t` output | Decoded packets | Observed/inferred | Approximate milliseconds |
| Legacy binary dumps | Format-dependent | Format-dependent | Format-dependent |
| Validator PTY capture | Exact | Exact | Monotonic, high resolution |
| Dedicated passive adapter | Exact when framing succeeds | Observed/inferred | Monotonic, high resolution |

Import metadata should explicitly classify each property as exact, inferred,
approximate, reconstructed, or unavailable. Captures without timing remain
useful as protocol examples and test vectors, but cannot be treated as
faithful timing-sensitive replay inputs.

Real captures must be reviewed before publication. They must not include
credentials, private network details, serial-device paths, or unrelated
operational data.

An initial Wireshark Lua dissector should expose Jandy/Pentair protocol,
source/destination IDs, commands, messages, checksum status, and direction as
filterable fields. A later Wireshark extcap adapter may expose the validator's
recorder as a live capture interface; neither a compiled Wireshark plugin nor
live extcap integration is required for the first end-to-end milestone.

## Capturing RS485 traffic during a test

The validator should capture serial traffic without stopping the behavior
being tested. The capture source depends on the operating mode:

- **Panel-free tests:** record at the PTY master boundary. This is the
  authoritative capture because the validator sees every byte injected into
  AqualinkD and every byte AqualinkD writes, can label the direction exactly,
  and can timestamp both directions with the same monotonic clock used for
  logs and scenario events.
- **Live-panel tests:** enable AqualinkD's normal
  `debug_RSProtocol_packets` logging while the daemon remains operational,
  then copy `/tmp/RS485.log` into the run's artifact directory. This log
  contains decoded packets read and written by AqualinkD.
- **Raw live-panel diagnostics:** AqualinkD's
  `debug_RSProtocol_bytes` option writes received bus bytes to
  `/tmp/RS485raw.log`. This is useful for framing or corruption problems, but
  it contains received bytes only and is not a bidirectional capture.
- **Independent bus capture:** a separate receive-only RS485 adapter may be
  used when a complete passive view of the physical bus is required. A second
  process must not read AqualinkD's serial device because the readers would
  compete for bytes.

AqualinkD's existing AQ Manager **Run Serial Logger** / RS485 Monitor is a
diagnostic takeover mode: it runs synchronously in the daemon's main serial
loop and pauses normal packet processing until the monitor exits. The
validator must therefore not invoke it during an operational or
timing-sensitive test.

The browser-based PDA and other AqualinkD interface emulators can remain active
during an operational capture. Their traffic uses AqualinkD's normal serial
path and will appear alongside the daemon's other packet activity. In
panel-free mode, interface-emulator traffic also crosses the PTY and is
captured automatically.

The current AqualinkD log paths are fixed and their files are truncated when a
new logger starts. The validator should initially serialize tests that collect
these files, snapshot them into the run artifacts before cleanup, and record
which logging options were enabled. A future AqualinkD enhancement could make
the log paths configurable per run.

## Safety principles

- Never use a real serial device unless the user explicitly enables
  live-panel mode.
- Generate isolated configurations and choose an unused non-privileged HTTP
  port.
- Disable MQTT, schedulers, and other external integrations by default.
- Display the selected AqualinkD binary, configuration, serial endpoint, and
  HTTP address before a run.
- Give every wait and I/O operation a deadline.
- Terminate only the AqualinkD process started by the validator.
- Preserve diagnostic artifacts after failure.
- Require an additional explicit flag for scenarios that can change physical
  equipment state.

## Initial implementation milestones

1. Establish the Python package, CLI, linting, type checking, and unit tests.
2. Add the reference container, pinned Python runtime, dependency lock, and
   mounted artifact directory; retain a local virtual environment as an
   optional development path.
3. Supervise `aqualinkd -d` and capture stdout and stderr concurrently.
4. Generate an isolated configuration and wait for HTTP readiness.
5. Add HTTP actions, status polling, log assertions, and deterministic
   timeouts.
6. Add PTY creation, bidirectional raw serial capture with monotonic
   timestamps, and packet expectations.
7. Write the versioned capture bundle, including serial PCAPNG, the combined
   JSONL timeline, and a provenance/fidelity manifest.
8. Define and validate the YAML scenario schema. **Implemented:** the schema-v1
   typed model, strict loader, protocol-independent keyword executor, PDA
   live-panel binding, example, and hardware-free validation command.
9. Complete the bounded panel-free feasibility test: a minimal probe/ACK
   exchange plus one HTTP action and expected serial response.
10. Add failure artifacts, including snapshots of any enabled AqualinkD
    protocol logs, and JUnit output suitable for CI.
11. Add an operational Jandy Power Center emulator scenario.
12. Add a Wireshark Lua dissector and legacy capture importers.
13. Add the first timing-sensitive PDA sleep/wake and menu scenario.

Stateful protocol drivers, passive capture from a dedicated live-bus adapter,
Wireshark extcap integration, timing fault injection, and broader protocol
coverage will follow after the basic runner is proven. PDA reliability is the
first protocol priority; the same capture infrastructure should also support
development for unavailable devices such as Jandy RS485 lights and Chem
readers.

## Relationship to AqualinkD tests and emulators

This project is intended to complement:

- C unit tests for pure functions, parsers, checksums, and packet processing;
- AqualinkD's browser-based AllButton, OneTouch, and AquaPDA interface emulators;
- Jandy's legacy Windows Power Center emulator; and
- manual testing with real control panels.

The AqualinkD interface emulators represent user interfaces that communicate
through AqualinkD with a physical controller. Panel-free validator mode will
instead emulate enough of the controller-facing serial conversation to run
AqualinkD itself. The repository
history around AqualinkD's retired file-backed serial support should be
reviewed for framing and replay lessons before implementing legacy importers.

The upstream design discussion is
[aqualinkd/AqualinkD discussion #539](https://github.com/aqualinkd/AqualinkD/discussions/539).

## Development status

The repository contains the Python package, reference container, process
supervisor, live-panel safety gates, HTTP and AquaPDA WebSocket clients,
performance comparison, PDA screen reconstruction and read-only menu walking,
and timing-sensitive PDA live-panel suites described above. These features are
usable but remain pre-1.0 and should be treated as active development,
especially when operating physical equipment.

PDA organization now resolves through one execution path:

- `testcases/pda/` contains versioned YAML cases and `testcases/suites/`
  provides their serialized ordering and AqualinkD configuration overrides.
- `run_targets.py` is the single registry and normalized target model for
  built-in suite names, explicit YAML paths, the cross-process long suite, and
  the remaining interface-emulator suites.
- `protocols/pda/keywords.py` binds small declarative steps to typed Python
  behavior, while `pda_scenario.py` still contains shared PDA session,
  protocol-log, status, sleep, and interface-emulator coordination awaiting
  further cohesive extraction.

The old general-purpose suite compatibility layer has been removed. The
remaining `pda/cases.py` and `pda/suites.py` catalog entries are limited to the
AqualinkD interface-emulator tests and the long suite's required process
boundary; ordinary physical-panel coverage is authored in YAML.

[GitHub issue #1](https://github.com/ballle98/aqualinkd-validator/issues/1)
tracks the initial panel-free end-to-end milestone. Its isolated configuration
builder, PTY transport, bidirectional serial timeline and PCAPNG writer,
generic YAML scenario runner, HTTP history, and probe/ACK smoke scenario are
not implemented yet. The PDA live-panel work was developed ahead of that
panel-free milestone and does not close the issue.

Contributions and examples of existing AqualinkD validation workflows are
welcome, especially reusable packet captures with documented panel models and
revisions.
