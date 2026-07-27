# AqualinkD Validator

`aqualinkd-validator` is a planned containerized Python validation harness for
[AqualinkD](https://github.com/aqualinkd/AqualinkD). It will exercise the
complete daemon through its process, logging, HTTP, and serial interfaces so
that timing-sensitive behavior can be reproduced and checked consistently.

> [!IMPORTANT]
> This project is in its initial design phase. The commands and scenario
> examples below describe the intended interface and are not implemented yet.

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

## Proposed operating modes

### Live-panel mode

The validator starts or attaches to AqualinkD connected to a real control
panel. A scenario can perform HTTP actions, wait for state changes, inspect
logs, and enforce timing expectations.

Live-panel tests will require an explicit opt-in and identify the serial device
being used. The validator must never silently select a real serial port.

### Jandy simulator mode

The validator supervises AqualinkD while it is connected to one of Jandy's
legacy Windows panel simulators through two RS485 adapters: one attached to
the Windows host or VM and one attached to the Linux host or container. The
simulator and serial link are initially treated as externally managed; the
validator drives AqualinkD through HTTP, evaluates state and timing, and
collects synchronized artifacts.

This mode is distinct from AqualinkD's browser-based AllButton, OneTouch, and
PDA interface simulators.

### Panel-free mode

The validator creates a pseudo-terminal (PTY) pair and configures AqualinkD to
use the slave PTY as its `serial_port`. A panel driver uses the master side to:

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
| Panel driver | Replay captures and implement stateful panel behavior |
| Scenario runner | Execute declarative steps with monotonic deadlines |
| Assertions | Check HTTP state, logs, serial packets, timing, and process status |
| Capture pipeline | Import legacy captures and write PCAPNG, JSONL, and provenance metadata |
| Artifact writer | Save capture bundles, generated config, logs, and reports |

The canonical supported runtime will be a container with a pinned Python
version and locked dependencies. It should run panel-free tests without broad
host privileges and write artifacts to a mounted directory. Live-panel and
Jandy simulator modes must map only explicitly selected serial devices rather
than requiring an unrestricted privileged container.

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

## Capture bundle and formats

Each validation run should produce a versioned capture bundle:

```text
run-<id>/
├── manifest.yaml
├── serial.pcapng
├── timeline.jsonl
├── stdout.log
├── stderr.log
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

The browser-based PDA and other interface simulators can remain active during
an operational capture. Their traffic uses AqualinkD's normal serial path and
will appear alongside the daemon's other packet activity. In panel-free mode,
simulator traffic also crosses the PTY and is captured automatically.

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
8. Define and validate the YAML scenario schema.
9. Complete the bounded panel-free feasibility test: a minimal probe/ACK
   exchange plus one HTTP action and expected serial response.
10. Add failure artifacts, including snapshots of any enabled AqualinkD
    protocol logs, and JUnit output suitable for CI.
11. Add an operational Jandy simulator scenario.
12. Add a Wireshark Lua dissector and legacy capture importers.
13. Add the first timing-sensitive PDA sleep/wake and menu scenario.

Stateful protocol drivers, passive capture from a dedicated live-bus adapter,
Wireshark extcap integration, timing fault injection, and broader protocol
coverage will follow after the basic runner is proven. PDA reliability is the
first protocol priority; the same capture infrastructure should also support
development for unavailable devices such as Jandy RS485 lights and Chem
readers.

## Relationship to AqualinkD tests and simulators

This project is intended to complement:

- C unit tests for pure functions, parsers, checksums, and packet processing;
- AqualinkD's browser-based AllButton, OneTouch, and PDA simulators;
- Jandy's legacy Windows panel simulators; and
- manual testing with real control panels.

The existing simulators emulate user interfaces that communicate with a
physical controller. Panel-free validator mode instead emulates enough of the
controller-facing serial conversation to run AqualinkD itself. The repository
history around AqualinkD's retired file-backed serial support should be
reviewed for framing and replay lessons before implementing legacy importers.

The upstream design discussion is
[aqualinkd/AqualinkD discussion #539](https://github.com/aqualinkd/AqualinkD/discussions/539).

## Development status

The repository currently contains the design only. The first implementation
will be tracked in GitHub issues, starting with the minimum end-to-end
foreground-process and PTY smoke test.

Contributions and examples of existing AqualinkD validation workflows are
welcome, especially reusable packet captures with documented panel models and
revisions.
