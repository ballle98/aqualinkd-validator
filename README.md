# AqualinkD Validator

`aqualinkd-validator` is a Python integration and performance test harness for
[AqualinkD](https://github.com/aqualinkd/AqualinkD). It starts AqualinkD as a
supervised foreground process, streams its output, drives its HTTP and AquaPDA
interfaces, checks protocol state and timing, restores changed equipment, and
writes diagnostic artifacts.

The immediate priority is repeatable PDA validation against physical panels and
Jandy's Windows Power Center emulator. A bounded panel-free RS485 mode is also
implemented for tests that do not require pool hardware.

> [!CAUTION]
> Live-panel tests can operate pumps, valves, lights, and heaters. Review the
> selected suite and every device exposed by `/api/devices`. Do not run a
> read-write suite during maintenance, with unsafe valve positions or water
> levels, or when equipment operation could be hazardous.

## Current capabilities

<!-- markdownlint-disable MD013 -->

| Mode | What it tests | Status |
| --- | --- | --- |
| Physical panel | AqualinkD process, HTTP API, PDA programming, sleep/wake behavior, AquaPDA WebSocket, equipment state, cleanup, and timing | Implemented and used for live regression testing |
| Jandy Power Center emulator | The same PDA coverage with Alwin32 `Pwrcntr.exe` supplying the southbound panel conversation | Implemented; Wine helper automation and virtual/physical serial links are supported |
| RS485 panel emulator | Isolated AqualinkD process using a private PTY, generated configuration, scripted frames, and a minimal stateful AllButton driver | Implemented in automated tests; native AqualinkD verification remains |

<!-- markdownlint-enable MD013 -->

Every mode captures AqualinkD stdout and stderr. Panel-free mode additionally
captures both serial directions with monotonic timing and writes PCAPNG.
Operational live-panel RS485-log collection, capture replay, legacy importers,
a Wireshark dissector, and broader stateful panel models remain planned under
[issue #1](https://github.com/ballle98/aqualinkd-validator/issues/1).

Related documentation:

- [Contributing and adding testcases](CONTRIBUTING.md)
- [High-level design](https://github.com/ballle98/aqualinkd-validator/wiki/High-Level-Design)
- [Jandy Power Center emulator setup](https://github.com/ballle98/aqualinkd-validator/wiki/Jandy-Windows-Panel-Simulator)
- [Initial implementation plan](https://github.com/ballle98/aqualinkd-validator/issues/1)

## Quick start: installed AqualinkD on a Raspberry Pi

The recommended first run uses Docker on the same 64-bit Raspberry Pi as the
installed AqualinkD service. This keeps the validator runtime reproducible while
giving its supervised AqualinkD process direct access to the local serial
device.

### Prerequisites

- A 64-bit Linux host. Raspberry Pi OS/Debian 13 arm64 is the primary tested
  environment.
- Docker Engine, or Python 3.11 or newer for direct execution.
- An AqualinkD binary built for the host.
- A reviewed AqualinkD configuration whose `serial_port` and `web_directory`
  exist in the selected runtime.
- Exclusive access to the serial device. Stop the installed service before the
  validator starts another AqualinkD process.

For the examples below, the installed system uses:

```text
/usr/local/bin/aqualinkd
/etc/aqualinkd.conf
/dev/ttyUSB0
/var/www/aqualinkd/
```

Adjust the serial device and web directory to match `/etc/aqualinkd.conf`.

### Build the validator image

Run on the Pi:

```sh
git clone https://github.com/ballle98/aqualinkd-validator.git
cd aqualinkd-validator

VALIDATOR_COMMIT=$(git rev-parse HEAD)
sudo env BUILDX_GIT_INFO=0 docker build \
  --build-arg "VCS_REF=$VALIDATOR_COMMIT" \
  -t aqualinkd-validator:local .

sudo docker run --rm aqualinkd-validator:local doctor
```

`doctor` reports the validator version and runtime capabilities. It does not
start AqualinkD, access the serial device, or operate equipment.

The image contains Python, the validator package, its pinned runtime dependency,
and the shipped testcases. It does not contain AqualinkD, the AqualinkD
configuration, web files, or panel data; those are mounted at run time.

`BUILDX_GIT_INFO=0` avoids a harmless Buildx warning when root cannot apply the
Pi user's Git safe-ownership settings. `VCS_REF` still records the validator
revision in the image label.

### Run the fast PDA suite

The validator must supervise AqualinkD itself to correlate logs, process
metrics, API state, and cleanup. It does not attach to the systemd service.

<!-- markdownlint-disable MD013 -->

```sh
sudo systemctl stop aqualinkd
pgrep -a aqualinkd

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
validator_status=$?

sudo systemctl start aqualinkd
printf 'validator exit status: %s\n' "$validator_status"
```

<!-- markdownlint-enable MD013 -->

Do not continue if `pgrep` shows another AqualinkD process after the service is
stopped. Change `TZ`, the mapped serial device, and the web-directory mount for
the installation. `TZ` should normally match the Pi and panel timezone; omit it
only when the container's default timezone is appropriate.

AqualinkD needs its configured web directory because the child process sees the
container filesystem. The validator itself does not read those web files.

The host artifact directory is mapped to the validator's default
`/tmp/aqualinkd-validator-artifacts`. Docker creates the host directory if it
does not exist. Preserve a failed run's complete directory when reporting a
problem.

## Choose a PDA suite

`run` accepts one or more suite names or YAML testcase paths. Multiple targets
are executed serially and never compete for the serial bus.

<!-- markdownlint-disable MD013 -->

| Suite | Access | Coverage |
| --- | --- | --- |
| `pda-live-fast` | Read-write | PDA initialization, identity, clock, filter-pump round trip, and optional non-heating Pool Heater checks |
| `pda-live-awake` | Read-write | Fast coverage, safe Equipment Status reconciliation, and consecutive-device operations with PDA sleep disabled |
| `pda-live-sleep` | Read-write | Natural sleep/wake timing plus commands during STATUS retries and after probing resumes |
| `pda-live-long` | Read-write | Composite awake and sleep suites in separate AqualinkD processes |
| `pda-live-spa` | Read-write | Opt-in Pool fill, Spa mode, active Spa Heater, cooldown, and restoration test |
| `aquapda-websocket-transport` | Read-only | AquaPDA packet, checksum, ACK timing, and navigation-failure regression |
| `aquapda-live-panel-menu-walk` | Read-only | Recursive AquaPDA read-only menu traversal against a physical panel |
| `pda-power-center-fast` | Read-write | Fast PDA coverage against the Jandy Power Center emulator |
| `pda-power-center-awake` | Read-write | Awake equipment and status coverage against Power Center |
| `pda-power-center-sleep` | Read-write | Command-driven sleep transition coverage against Power Center |
| `pda-power-center-full` | Read-write | Serialized awake, sleep, and AquaPDA menu-walk coverage against Power Center |
| `pda-power-center-spa` | Read-write | Explicit Power Center Spa/heating coverage |
| `aquapda-power-center-menu-walk` | Read-only | Recursive AquaPDA menu traversal with Power Center as the southbound panel |

<!-- markdownlint-enable MD013 -->

Use `--panel` (also `--panel-read-only`) to authorize a read-only physical or
externally emulated panel test. Use `--panelw` (also
`--panel-read-write`) to authorize equipment changes. A read-write testcase is
rejected unless read-write access was explicitly granted.

For example:

```sh
# Read-only AquaPDA transport check
.venv/bin/aqualinkd-validator run \
  --panel \
  aquapda-websocket-transport

# Two independent suites, serialized
.venv/bin/aqualinkd-validator run \
  --panel-read-write \
  pda-live-fast pda-live-sleep
```

`pda-live-long` derives private temporary configurations with
`pda_sleep_mode=no` for the awake member and `pda_sleep_mode=yes` for the sleep
member. It never modifies the supplied AqualinkD configuration. Each member
gets a separate artifact directory.

The awake suite considers all eligible switches discovered through
`/api/devices`. Repeated `--pda-test-device ID` options restrict its dedicated
consecutive-device phase. The sleep suite defaults to the deepest eligible
auxiliary, or Filter Pump when no auxiliary is actionable.

Freeze-protection mutation and service-mode entry are excluded from physical
panel suites. The general fast, awake, sleep, and long suites avoid deliberate
active heating. Hydraulic routing, heat demand, and cooldown are isolated in
the explicit Spa suite.

## Pool-specific settings

Installation-specific physical delays belong in an optional site profile named
`aqualinkd-validator.yaml` beside the active `aqualinkd.conf`:

```sh
sudo cp examples/aqualinkd-validator.yaml /etc/aqualinkd-validator.yaml
sudo editor /etc/aqualinkd-validator.yaml
```

For example:

```yaml
schema: 1
spa:
  fill_time: 8m
```

`spa.fill_time` is required only by `pda-live-spa` and
`pda-power-center-spa`. It allows an installation to circulate or fill the Spa
in Pool mode before changing valve mode. The ordinary suites do not require a
site profile.

In Docker, mount the profile at the corresponding path:

```sh
--mount type=bind,source=/etc/aqualinkd-validator.yaml,target=/etc/aqualinkd-validator.yaml,readonly
```

Use `--site-config` only when the profile is not beside the selected AqualinkD
configuration.

## Test a development AqualinkD build

The validator image can run a binary and configuration staged under `/tmp`.
Rebuild the image only when validator code changes.

Stage the SUT on the Pi:

<!-- markdownlint-disable MD013 -->

```sh
sudo install -m 0755 ~/git/AqualinkD/release/aqualinkd /tmp/aqualinkd
sudo cp -n /etc/aqualinkd.conf /tmp/aqualinkd.conf
sudo mkdir -p /tmp/aqualinkd-web
sudo cp -a ~/git/AqualinkD/web/. /tmp/aqualinkd-web/
sudo sed -i -E \
  's|^[[:space:]]*web_directory[[:space:]]*=.*$|web_directory=/tmp/aqualinkd-web/|' \
  /tmp/aqualinkd.conf
```

<!-- markdownlint-enable MD013 -->

Review `/tmp/aqualinkd.conf`, especially its serial port, PDA settings, HTTP
listener, credentials, and external integrations. Then run:

<!-- markdownlint-disable MD013 -->

```sh
sudo systemctl stop aqualinkd
pgrep -a aqualinkd

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
validator_status=$?

sudo systemctl start aqualinkd
printf 'validator exit status: %s\n' "$validator_status"
```

<!-- markdownlint-enable MD013 -->

The staged inputs are mounted at the validator defaults, so additional path
options are unnecessary.

## Run directly without Docker

Direct execution avoids the Docker daemon and container filesystem on a Pi Zero
or another constrained system:

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

Direct mode uses the same defaults as the container:

| Input | Default |
| --- | --- |
| AqualinkD | `/usr/local/bin/aqualinkd` |
| AqualinkD configuration | `/etc/aqualinkd.conf` |
| Serial device | Read from `serial_port` in the configuration |
| Artifacts | `/tmp/aqualinkd-validator-artifacts` |
| Site profile | `aqualinkd-validator.yaml` beside the configuration |
| Panel timezone | Host's local IANA timezone |
| HTTP origin | Discovered from AqualinkD startup logs |

Use `--aqualinkd`, `--config`, `--serial-device`, `--artifacts`,
`--site-config`, `--panel-timezone`, or `--api-base-url` only when an override
is required. An explicit serial-device override must agree with the selected
configuration.

For PDA suites, AqualinkD is started in the foreground with `-vv`. Both the
current startup URL and the ballle98 2.3.7 `Starting web server on port <N>` log
form are understood, including ports such as 8080. Timestamped and
non-timestamped log forms are accepted.

## Jandy Power Center emulator

The `jandy-power-center` mode tests AqualinkD against Jandy's legacy Alwin32
`Pwrcntr.exe`, which supplies the southbound RS485 master-panel conversation.
It is different from AqualinkD's northbound AquaPDA, AllButton, and OneTouch
browser interfaces.

Follow the [Power Center emulator setup
guide](https://github.com/ballle98/aqualinkd-validator/wiki/Jandy-Windows-Panel-Simulator)
to:

1. Download and install the official Jandy package directly under Wine.
2. Connect its Windows COM port to AqualinkD using physical RS485 adapters or a
   virtual PTY link.
3. Build or download the open-source `pwrcntr-control.exe` helper.
4. Configure the Wine prefix, model, and COM port in the validator site profile.

Then run, for example:

```sh
.venv/bin/aqualinkd-validator run \
  --mode jandy-power-center \
  --panel-read-write \
  --site-config /tmp/aqualinkd-validator.yaml \
  pda-power-center-full
```

The validator can select the configured model and port and establish a
serial-verified off-to-on Power Center cycle before each composite member. Its
commands, helper checksum, Wine version, diagnostics, and observed power state
are written to `power-center.json`.

## Panel-free RS485 tests

Panel-free mode creates a private PTY and AqualinkD configuration, reserves an
unused loopback HTTP port, disables external integrations, starts AqualinkD,
and captures both serial directions. No physical panel authorization is used.

Validate YAML without starting AqualinkD:

```sh
.venv/bin/aqualinkd-validator validate-testcase \
  testcases/rs485/probe-ack.yaml \
  testcases/rs485/allbutton-filter.yaml
```

Run the scripted PDA probe/ACK testcase:

```sh
.venv/bin/aqualinkd-validator run-panel-free \
  --aqualinkd ~/git/AqualinkD/release/aqualinkd \
  --web-directory ~/git/AqualinkD/web \
  testcases/rs485/probe-ack.yaml
```

Run the minimal stateful AllButton HTTP-to-RS485 testcase:

```sh
.venv/bin/aqualinkd-validator run-panel-free \
  --aqualinkd ~/git/AqualinkD/release/aqualinkd \
  --web-directory ~/git/AqualinkD/web \
  testcases/rs485/allbutton-filter.yaml
```

The AllButton fixture performs probe/ACK and periodic STATUS exchanges, applies
the Filter Pump command to its next status packet, and verifies that the HTTP
request produced key command `0x02`. This is a bounded feasibility driver, not
yet a general replacement for a physical panel or Power Center.

## Declarative testcases

Ordinary PDA cases and suites are versioned YAML under `testcases/`. A testcase
declares its mode, access, requirements, bounded steps, and restoration intent:

```yaml
schema: 1
id: pda.filter-after-init
description: Toggle the filter after PDA initialization
mode: physical-panel
access: read-write
requires: {protocol: pda}
steps:
  - wait_for:
      condition: pda.initialized
      timeout: 180s
  - set_device:
      id: Filter_Pump
      state: opposite-of-original
      activation_timeout: 130s
      completion_timeout: 90s
  - assert_device:
      id: Filter_Pump
      state: requested
      timeout: 10s
finally:
  - restore_original_state: {}
```

Run a YAML file in the same position as a suite name:

```sh
.venv/bin/aqualinkd-validator run \
  --panel-read-write \
  testcases/pda/filter-after-init.yaml
```

The complete document is validated before AqualinkD starts. Mutating PDA
testcases must declare `restore_original_state`; the runtime also makes a final
safety-restoration attempt after failure or cancellation. See
[CONTRIBUTING.md](CONTRIBUTING.md) for keyword and extension rules.

## Console output and artifacts

Progress is flushed immediately through terminals, Docker, and SSH:

```text
[ RUN  ] PDA initialization, identity, and clock
[STATE ] Waiting on control-panel probe
[STATE ] Control-panel probe received
[ACTIVE] Init PDA became active after 0.608s
[ DONE ] Init PDA programmer completed in 17.800s
[ PASS ] PDA initialization, identity, and clock completed in 18.485s
```

AqualinkD warnings and errors are forwarded while the complete high-volume log
remains in the artifact directory. Each physical action records HTTP
acknowledgement, programmer activation wait, active runtime, API convergence,
and total elapsed time.

A live-panel run normally contains:

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

Automated Power Center runs add `power-center.json`. Panel-free runs add the
effective generated configuration, testcase copy, `http.jsonl`, and
`serial.pcapng`.

The configuration fingerprint is recorded, but a live AqualinkD configuration
is not copied into artifacts because it may contain credentials. Panel-free
generated configurations are safe and are retained for reproduction.

Compare two or more runs:

```sh
aqualinkd-validator compare \
  artifacts/20260727T120000Z-rel-2.3.7 \
  artifacts/20260727T121500Z-upstream-master \
  artifacts/20260727T123000Z-merge-pda-3.1.x
```

The comparison reports process CPU and memory measurements and per-action PDA
timings. It warns when relevant hardware, kernel, configuration, suite, panel,
or sampling metadata differs.

## RS485 capture status

Panel-free capture is implemented at the PTY master boundary. The validator
knows exact direction and framing and timestamps both directions with the same
monotonic clock used for HTTP, process, and scenario events. `serial.pcapng`
uses a small versioned `AQV1` pseudo-header with `LINKTYPE_USER0`; the original
RS485 frame bytes remain unchanged.

Live-panel capture is not yet integrated into `run`. The intended first source
is AqualinkD's operational `debug_RSProtocol_packets` log, which records decoded
packets without taking over the daemon. `debug_RSProtocol_bytes` is useful for
received-byte diagnostics but is not bidirectional. A second process must not
read AqualinkD's serial device because competing readers divide the bytes.

AQ Manager's **Run Serial Logger** / RS485 Monitor is a diagnostic takeover
mode and pauses normal packet processing, so it must not be used during a
timing-sensitive operational test. Dedicated passive adapters, capture replay,
legacy conversion, and Wireshark integration remain roadmap work.

## Install Docker on a 64-bit Raspberry Pi

Use Docker's [official Debian installation
instructions](https://docs.docker.com/engine/install/debian/) for a 64-bit
Raspberry Pi OS/Debian system. Confirm `aarch64`, `arm64`, and the expected
Debian codename before continuing:

```sh
uname -m
dpkg --print-architecture
. /etc/os-release
echo "$ID $VERSION_CODENAME"
```

Install the repository prerequisites and key:

```sh
sudo apt update
sudo apt install ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

Add Docker's repository:

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
sudo apt install \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
```

Verify the installation:

```sh
sudo systemctl status docker
sudo docker run hello-world
sudo docker compose version
```

Compose is a small client-side convenience and is not required by the current
single-container commands. Use `sudo docker` initially. Membership in the
`docker` group grants root-equivalent host control and should be an explicit
choice, not treated as ordinary unprivileged access. The validator needs only
the selected serial device; it does not require `--privileged`.

The reference image is based on pinned Python 3.12 slim Trixie and supports
`linux/arm64` and `linux/amd64`. Containers share the host kernel; the relevant
measurement overhead comes from the Python supervisor, log writes, and `/proc`
sampling rather than machine emulation. Direct execution avoids the Docker
daemon on constrained devices.

## Development

Create a development environment and run the checks:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check .
.venv/bin/mypy --strict src
git diff --check
```

Unit tests require no serial device, running AqualinkD process, or network.
Protocol and engine tests use typed interfaces and deterministic in-memory
adapters. New ordinary physical-panel cases should be expressed in YAML; add a
small typed Python keyword only when new protocol behavior is required.

See [CONTRIBUTING.md](CONTRIBUTING.md) for repository structure, terminology,
testcase authoring, safety review, and commit requirements. The
[high-level-design wiki](https://github.com/ballle98/aqualinkd-validator/wiki/High-Level-Design)
shows the process, HTTP, serial, and artifact boundaries.

The project is pre-1.0 and under active development. Contributions are welcome,
especially documented captures, additional panel models, and reproducible
AqualinkD failures.
