# Contributing to AqualinkD Validator

Thank you for helping make AqualinkD validation repeatable. This harness can
operate physical pool equipment, so readable tests, bounded waits, and reliable
cleanup are more important than minimizing code.

## Development setup

The package supports Python 3.11 or newer and has no third-party runtime
dependencies. Create an editable development environment from the repository
root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/aqualinkd-validator doctor
```

Before submitting a change, run:

```sh
.venv/bin/python -m unittest discover -s tests
.venv/bin/ruff check .
.venv/bin/mypy --strict src
git diff --check
```

Unit tests must not require a serial device, a running AqualinkD process, or
network access. Use the fake API, output monitor, and fake AquaPDA transport in
`tests/` to reproduce protocol and timing conditions.

## Architecture at a glance

The [high-level design](https://github.com/ballle98/aqualinkd-validator/wiki/High-Level-Design)
describes the process, HTTP, log, serial, and artifact paths. The principal
source modules are:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Options, safety authorization, suite selection, and run artifacts |
| `supervisor.py` | AqualinkD lifecycle, stdout/stderr fan-out, timing, and process metrics |
| `interfaces/events.py` | Typed ordered-log and monotonic-timeline boundaries |
| `engine/restoration.py` | Initial-state capture, touched-resource tracking, dependency-aware restoration ordering, and retry suppression |
| `protocols/pda/programmer.py` | PDA programmer activation, completion, error correlation, and timing |
| `pda/cases.py` | Stable case identifiers, names, and mutation policy |
| `pda/suites.py` | Declarative ordering of cases and configuration overrides |
| `pda_scenario.py` | Current PDA case coordination, protocol log correlation, state validation, and timing |
| `http_api.py` | Minimal asynchronous AqualinkD HTTP client |
| `pda_simulator.py` | Client for AqualinkD's AquaPDA interface-emulation WebSocket |

`pda_scenario.py` currently contains too many responsibilities. Restoration
policy has moved into `RestorationSession`; PDA-specific command/log
correlation is moving into `PdaProgrammerObserver`. New work should prefer
extracting a cohesive parser, action, state model, or case module rather than
making that class larger.

## Terminology

Do not use *simulator* by itself. Use the name that identifies which side of
AqualinkD is being represented:

| Preferred term | Meaning |
| --- | --- |
| **Jandy Power Center emulator** | The vendor Alwin32 `Pwrcntr.exe` program acting as the southbound RS485 master panel. Jandy's package calls it a simulator. |
| **AqualinkD interface emulator** | AqualinkD's northbound AquaPDA, AllButton, or OneTouch browser/WebSocket interface, representing a user interface attached to AqualinkD. |
| **RS485 panel emulator** | The planned validator-owned Python component that replays captures or implements stateful panel behavior through a PTY. |
| **physical panel** | A real AquaLink power-center controller connected over RS485. |
| **capture replay** | Feeding recorded frames and timing into a transport; it is not necessarily a stateful panel emulator. |

Existing public identifiers such as `--mode jandy-simulator`,
`pda-live-simulator`, and `pda_simulator.py` remain compatible names. In new
documentation and code comments, qualify them using the terms above.

## Adding or changing a test today

Cases and suites are intentionally separate:

1. Add a stable `PdaCaseId` and its mutation policy in `pda/cases.py`.
2. Implement the operation in the appropriate case module. Until the planned
   split is complete, PDA operations live in `pda_scenario.py`.
3. Map the case ID to the operation in `_case_operation()`.
4. Add the case to one or more ordered suites in `pda/suites.py`.
5. Add a focused unit test, including timeout and failure behavior.
6. For a mutating case, prove restoration on success, assertion failure, and
   cancellation.
7. Run a physical-panel test only after unit tests pass and only with explicit
   `--panel-read-write` authorization.

Every wait must have a deadline. Capture the output cursor before an action and
accept only log events after that cursor. Do not treat HTTP request completion
as panel completion: correlate the programmer activation/completion logs and
then verify converged API state.

Do not add a new command-line option for a test variant that can be expressed
as a case or suite. Suites run serially and may provide temporary AqualinkD
configuration overrides.

## Declarative testcases

The intended contributor-facing format is versioned YAML. It is not implemented
yet; current executable cases remain Python. Python should continue to own
transport, process supervision, monotonic timing, typed state interpretation,
and restoration. YAML should describe test intent by composing a small set of
reviewable keywords.

For example:

```yaml
schema: 1
id: pda.filter-after-init
description: Toggle the filter after PDA initialization
mode: physical-panel
access: read-write
requires:
  protocol: pda
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

The initial keyword set should remain deliberately small:

- `wait_for`
- `set_device`
- `set_setpoint`
- `assert_device`
- `assert_log`
- `assert_no_log`
- `wait_for_stable_equipment`
- `restore_original_state`

Protocol-specific behavior belongs behind typed Python keywords. Declarative
files must not contain arbitrary Python, shell commands, regular-expression
control flow, or unbounded loops. The loader should validate the complete file
before starting AqualinkD and reject unknown keys.

This design lets a contributor clone the repository and edit a testcase
without understanding `asyncio`, while keeping hazardous operations and cleanup
inside reviewed code. More complex protocol exploration can still be written
as a Python case.

## Live-panel safety review

A pull request that changes physical-panel behavior should state:

- which equipment may be operated;
- the original-state snapshot used for cleanup;
- the restoration order and timeout;
- behavior after interruption or AqualinkD exit;
- the physical panel model and firmware used for validation; and
- artifact paths or a sanitized summary demonstrating the result.

Never publish an AqualinkD configuration containing credentials or unrelated
network details. Captures must be reviewed and document whether direction,
framing, and timing are exact, inferred, approximate, reconstructed, or absent.

## Commit and issue references

Use concise commits that separate refactoring from behavior changes. Refer to
issues using the fully qualified repository name, for example
`Fixes ballle98/aqualinkd-validator#7`, so the reference remains unambiguous if
changes are discussed or copied elsewhere.
