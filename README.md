# AqualinkD Validator

`aqualinkd-validator` is a planned Python validation harness for
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
| Artifact writer | Save merged timelines, raw streams, generated config, and reports |

## Scenario format

Scenarios are expected to be declarative YAML with explicit timeouts. A
possible PDA scenario might look like:

```yaml
name: pda-wakes-for-filter-pump-request
mode: replay
panel: pda
capture: captures/pda/idle.jsonl

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

## Capture format

The initial capture format is expected to be line-delimited JSON so captures
remain inspectable and diffable:

```json
{"offset_ns":0,"direction":"panel_to_aqualinkd","data":"1002600200621003"}
{"offset_ns":28000000,"direction":"aqualinkd_to_panel","data":"00100200010000131003"}
```

Each record should use a monotonic offset, an explicit direction, and the
unmodified bytes seen on the serial connection. Metadata such as panel model,
revision, AqualinkD version, configuration, and sanitization notes should live
in a separate capture manifest.

Real captures must be reviewed before publication. They must not include
credentials, private network details, serial-device paths, or unrelated
operational data.

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
2. Supervise `aqualinkd -d` and capture stdout and stderr concurrently.
3. Generate an isolated configuration and wait for HTTP readiness.
4. Add HTTP actions, status polling, log assertions, and deterministic
   timeouts.
5. Add PTY creation, raw serial capture, and packet expectations.
6. Define and validate the YAML scenario schema.
7. Replay a minimal probe/ACK exchange without a physical panel.
8. Add failure artifacts and JUnit output suitable for CI.
9. Add the first timing-sensitive PDA scenario.

Stateful protocol drivers, capture recording from live panels, timing fault
injection, and broader protocol coverage will follow after the basic runner is
proven.

## Relationship to AqualinkD tests and simulators

This project is intended to complement:

- C unit tests for pure functions, parsers, checksums, and packet processing;
- AqualinkD's browser-based AllButton, OneTouch, and PDA simulators; and
- manual testing with real control panels.

The existing simulators emulate user interfaces that communicate with a
physical controller. Panel-free validator mode instead emulates enough of the
controller-facing serial conversation to run AqualinkD itself.

The upstream design discussion is
[aqualinkd/AqualinkD discussion #539](https://github.com/aqualinkd/AqualinkD/discussions/539).

## Development status

The repository currently contains the design only. The first implementation
will be tracked in GitHub issues, starting with the minimum end-to-end
foreground-process and PTY smoke test.

Contributions and examples of existing AqualinkD validation workflows are
welcome, especially reusable packet captures with documented panel models and
revisions.
