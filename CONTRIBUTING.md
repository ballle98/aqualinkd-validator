# Contributing to AqualinkD Validator

Thank you for helping make AqualinkD validation repeatable. This harness can
operate physical pool equipment, so readable tests, bounded waits, and reliable
cleanup are more important than minimizing code.

## Development setup

The package supports Python 3.11 or newer and uses pinned PyYAML for testcase
loading. Create an editable development environment from the repository root:

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
| `cli.py` | Options, resolved-target execution, safety authorization, and run artifacts |
| `run_targets.py` | Single registry that normalizes named suites and YAML paths before execution |
| `supervisor.py` | AqualinkD lifecycle, stdout/stderr fan-out, timing, and process metrics |
| `interfaces/events.py` | Typed ordered-log and monotonic-timeline boundaries |
| `engine/restoration.py` | Initial-state capture, touched-resource tracking, dependency-aware restoration ordering, and retry suppression |
| `engine/equipment_actions.py` | Measured device and setpoint mutations, PDA programmer correlation, API convergence, and restoration tracking |
| `engine/equipment_stability.py` | Typed transition interpretation, stable-state polling, observation artifacts, and timeout evidence |
| `protocols/pda/programmer.py` | PDA programmer activation, completion, error correlation, and timing |
| `protocols/pda/session.py` | PDA_INIT coordination, startup identity parsing, firmware-screen capture, and HTTP endpoint discovery |
| `protocols/pda/identity.py` | Configured/reported panel comparison, API identity capture, and bounded panel-clock validation |
| `protocols/pda/equipment_status.py` | Complete multi-page EQUIPMENT STATUS observation, parsing, API reconciliation, and failure evidence |
| `protocols/pda/sleep.py` | Natural sleep/wake duty-cycle observation, post-wake refresh timing, and STATUS-retry/probe transition validation |
| `protocols/pda/keywords.py` | Binding from typed declarative keywords to PDA initialization, actions, assertions, and restoration |
| `testcases/` | Strict schema-v1 YAML loading, typed steps, and protocol-independent keyword execution |
| `pda_scenario.py` | `PdaScenarioRuntime`, the remaining PDA lifecycle and keyword coordinator |
| `http_api.py` | Minimal asynchronous AqualinkD HTTP client |
| `aquapda_client.py` | Client and screen reconstruction for AqualinkD's AquaPDA WebSocket interface |
| `protocols/pda/aquapda.py` | AquaPDA transport validation and bounded read-only menu walking |

`pda_scenario.py` still contains too many responsibilities. Restoration policy,
equipment mutations, stable-state observation, PDA programmer correlation,
session startup, panel identity/clock validation, equipment-status
reconciliation, and sleep/wake observation now live behind focused engine and
protocol classes. New work should prefer extracting a cohesive parser, state
model, or case module rather than making that coordinator larger.

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

Public suite, mode, module, and client identifiers use the precise terms above;
the pre-refactor ambiguous `simulator` names do not retain compatibility
aliases.

## Adding or changing a test today

Ordinary cases and process suites are YAML:

1. Add or edit a testcase under `testcases/pda/` using existing typed
   keywords.
2. Add its relative path to an ordered suite under `testcases/suites/`.
3. If the behavior needs new protocol logic, add one bounded step model and
   loader entry, implement one typed keyword in `protocols/pda/keywords.py`,
   and keep the YAML limited to intent, parameters, and deadlines.
4. Add focused loader, keyword, failure, and timeout tests.
5. For a mutating case, prove restoration on success, assertion failure, and
   cancellation.
6. Run a physical-panel test only after unit tests pass and only with explicit
   `--panel-read-write` authorization.

`run_targets.py` is the only run-target registry. Do not add suite-name
branches to `cli.py`. Its small Python target definitions cover only AquaPDA
interface cases and the long suite's cross-process boundary; ordinary
physical-panel test intent belongs in YAML.

Every wait must have a deadline. Capture the output cursor before an action and
accept only log events after that cursor. Do not treat HTTP request completion
as panel completion: correlate the programmer activation/completion logs and
then verify converged API state.

Do not add a new command-line option for a test variant that can be expressed
as a case or suite. Suites run serially and may provide temporary AqualinkD
configuration overrides.

## Declarative testcases

The contributor-facing format is versioned YAML. Schema-v1 loading, complete
preflight validation, protocol-independent keyword sequencing, and the PDA
live-panel keyword adapter are implemented. Existing Python cases remain useful
for complex protocol exploration. Python continues to own
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
- `exercise_heater`
- `assert_device`
- `assert_log`
- `assert_no_log`
- `wait_for_stable_equipment`
- `restore_original_state`

`exercise_heater` is deliberately a higher-level PDA keyword: Python selects
the bounded ±1 setpoint values for Fahrenheit or Celsius, handles an optional
heater, correlates programmer activity, restores the original setpoint, and
round-trips the enabled state. YAML does not contain temperature arithmetic or
conditional control flow.

Protocol-specific behavior belongs behind typed Python keywords. Declarative
files must not contain arbitrary Python, shell commands, regular-expression
control flow, or unbounded loops. The loader should validate the complete file
before starting AqualinkD and reject unknown keys.

Validate one or more files without starting AqualinkD or touching hardware:

```console
.venv/bin/aqualinkd-validator validate-testcase \
  testcases/pda/filter-after-init.yaml
```

Run validated PDA testcases serially by giving their paths in place of suite
names. The access declaration is enforced against the panel authorization:

```console
sudo .venv/bin/aqualinkd-validator run --panel-read-write \
  testcases/pda/filter-after-init.yaml
```

The loader validates every selected file before AqualinkD starts. Each YAML
timeout is passed to the corresponding typed PDA operation. The executor always
runs `finally`, and the scenario lifecycle retains an additional safety
restoration pass after failure or cancellation.

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
