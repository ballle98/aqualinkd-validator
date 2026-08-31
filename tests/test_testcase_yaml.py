from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from aqualinkd_validator.cli import main
from aqualinkd_validator.testcases import (
    ExerciseDiscoveredDevicesStep,
    ExerciseHeaterStep,
    ExerciseProbeTransitionStep,
    ExerciseStatusRetryStep,
    ExpectPanelCommandStep,
    ExpectSerialStep,
    HttpRequestStep,
    ObserveSleepCycleStep,
    RestoreOriginalStateStep,
    ReturnPdaHomeStep,
    SerialSendStep,
    SetDeviceStep,
    SetPowerCenterModeStep,
    VerifyEquipmentStatusStep,
    WaitForStep,
    WaitHttpJsonStep,
    load_testcase,
    load_testcase_suite,
)
from aqualinkd_validator.testcases import (
    TestcaseValidationError as ValidationError,
)


class TestcaseYamlTests(unittest.TestCase):
    def test_cli_validates_without_starting_aqualinkd(self) -> None:
        path = (
            Path(__file__).parents[1] / "testcases" / "pda" / "filter-after-init.yaml"
        )
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["validate-testcase", str(path)])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("pda.filter-after-init, 3 step(s)", output.getvalue())

    def test_cli_validates_complete_suite_without_starting_aqualinkd(self) -> None:
        path = Path(__file__).parents[1] / "testcases" / "suites" / "pda-live-fast.yaml"
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["validate-testcase", str(path)])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("pda-live-fast, 4 testcase(s)", output.getvalue())

    def test_loads_versioned_testcase_into_typed_steps(self) -> None:
        testcase = load_testcase(
            Path(__file__).parents[1] / "testcases" / "pda" / "filter-after-init.yaml"
        )

        self.assertEqual(testcase.schema, 1)
        self.assertEqual(testcase.identifier, "pda.filter-after-init")
        self.assertEqual(testcase.access, "read-write")
        self.assertIsInstance(testcase.steps[0], WaitForStep)
        action = testcase.steps[1]
        self.assertIsInstance(action, SetDeviceStep)
        assert isinstance(action, SetDeviceStep)
        self.assertEqual(action.activation_timeout_seconds, 130)
        self.assertEqual(action.convergence_timeout_seconds, 10)
        self.assertIsInstance(testcase.finally_steps[0], RestoreOriginalStateStep)

    def test_loads_optional_heater_policy_as_typed_step(self) -> None:
        testcase = load_testcase(
            Path(__file__).parents[1] / "testcases" / "pda" / "pool-heater.yaml"
        )
        heater = testcase.steps[1]
        self.assertIsInstance(heater, ExerciseHeaterStep)
        assert isinstance(heater, ExerciseHeaterStep)
        self.assertTrue(heater.optional)
        self.assertEqual(heater.identifier, "Pool_Heater")

    def test_loads_specialized_equipment_cases(self) -> None:
        root = Path(__file__).parents[1] / "testcases" / "pda"
        status = load_testcase(root / "equipment-status.yaml")
        consecutive = load_testcase(root / "consecutive-devices.yaml")

        self.assertIsInstance(status.steps[1], VerifyEquipmentStatusStep)
        self.assertIsInstance(consecutive.steps[1], ExerciseDiscoveredDevicesStep)
        self.assertEqual(status.steps[1].timeout_seconds, 600)
        self.assertEqual(status.finally_steps[0].timeout_seconds, 420)

    def test_loads_and_validates_complete_suite_graph(self) -> None:
        suite = load_testcase_suite(
            Path(__file__).parents[1] / "testcases" / "suites" / "pda-live-fast.yaml"
        )

        self.assertEqual(suite.identifier, "pda-live-fast")
        self.assertEqual(suite.config.execution_role, "awake")
        self.assertEqual(suite.config.override_map(), {"pda_sleep_mode": "no"})
        self.assertEqual(
            [member.testcase.identifier for member in suite.members],
            [
                "pda.initialization",
                "pda.filter-after-init",
                "pda.device-from-home",
                "pda.pool-heater",
            ],
        )
        home = suite.members[2].testcase.steps[1]
        self.assertIsInstance(home, ReturnPdaHomeStep)
        assert isinstance(home, ReturnPdaHomeStep)
        self.assertEqual(home.timeout_seconds, 30)
        self.assertTrue(suite.mutates_panel)

        awake = load_testcase_suite(
            Path(__file__).parents[1] / "testcases" / "suites" / "pda-live-awake.yaml"
        )
        self.assertEqual(len(awake.members), 5)
        self.assertTrue(awake.exercises_discovered_devices)

        sleep = load_testcase_suite(
            Path(__file__).parents[1] / "testcases" / "suites" / "pda-live-sleep.yaml"
        )
        self.assertEqual(len(sleep.members), 4)
        self.assertEqual(sleep.config.execution_role, "sleep")
        self.assertEqual(sleep.config.override_map(), {"pda_sleep_mode": "yes"})
        self.assertTrue(sleep.uses_selected_devices)

        power_center_sleep = load_testcase_suite(
            Path(__file__).parents[1]
            / "testcases"
            / "suites"
            / "pda-power-center-sleep.yaml"
        )
        self.assertEqual(len(power_center_sleep.members), 3)
        self.assertNotIn(
            "pda.sleep-cycle",
            [
                member.testcase.identifier
                for member in power_center_sleep.members
            ],
        )

        spa = load_testcase_suite(
            Path(__file__).parents[1] / "testcases" / "suites" / "pda-live-spa.yaml"
        )
        self.assertEqual(
            [member.testcase.identifier for member in spa.members],
            ["pda.initialization", "pda.spa-heating"],
        )
        self.assertEqual(spa.config.override_map(), {"pda_sleep_mode": "no"})

        service = load_testcase_suite(
            Path(__file__).parents[1]
            / "testcases"
            / "suites"
            / "pda-power-center-service.yaml"
        )
        self.assertEqual(
            [member.testcase.identifier for member in service.members],
            ["pda.initialization", "pda.service-mode"],
        )
        service_case = service.members[1].testcase
        self.assertIsInstance(service_case.steps[1], SetPowerCenterModeStep)
        self.assertEqual(service_case.steps[1].mode, "service")
        self.assertEqual(service_case.finally_steps[0].mode, "auto")

    def test_loads_specialized_sleep_cases(self) -> None:
        root = Path(__file__).parents[1] / "testcases" / "pda"
        sleep = load_testcase(root / "sleep-cycle.yaml")
        retry = load_testcase(root / "status-retry-command.yaml")
        probe = load_testcase(root / "probe-transition-command.yaml")

        self.assertIsInstance(sleep.steps[1], ObserveSleepCycleStep)
        self.assertIsInstance(retry.steps[1], ExerciseStatusRetryStep)
        self.assertIsInstance(probe.steps[1], ExerciseProbeTransitionStep)
        self.assertEqual(retry.finally_steps[0].timeout_seconds, 420)

    def test_loads_strict_panel_free_serial_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "probe.yaml"
            path.write_text(
                """
schema: 1
id: rs485.probe
description: Send a panel probe and expect an ACK
mode: rs485-panel-emulator
access: read-write
requires: {protocol: rs485}
fixture:
  panel_type: RS-4 Combo
  device_id: "0x0a"
steps:
  - serial_send:
      bytes: "10 02 60 00 72 10 03"
      timeout: 100ms
  - expect_serial:
      bytes: "100200010000131003"
      timeout: 2s
  - http_request:
      method: PUT
      path: /api/Filter_Pump/set
      value: 1
      timeout: 2s
  - wait_http_json:
      path: /api/status
      pointer: /leds/Filter_Pump
      equals: on
      timeout: 3s
      poll: 200ms
      request_timeout: 500ms
""",
                encoding="utf-8",
            )
            testcase = load_testcase(path)

        self.assertIsInstance(testcase.steps[0], SerialSendStep)
        self.assertIsInstance(testcase.steps[1], ExpectSerialStep)
        self.assertIsInstance(testcase.steps[2], HttpRequestStep)
        self.assertIsInstance(testcase.steps[3], WaitHttpJsonStep)
        assert isinstance(testcase.steps[0], SerialSendStep)
        assert isinstance(testcase.steps[1], ExpectSerialStep)
        self.assertEqual(testcase.steps[0].payload.hex(), "10026000721003")
        self.assertEqual(testcase.steps[0].timeout_seconds, 0.1)
        self.assertEqual(testcase.steps[1].timeout_seconds, 2)
        assert isinstance(testcase.steps[2], HttpRequestStep)
        self.assertEqual(testcase.steps[2].value, "1")
        poll = testcase.steps[3]
        assert isinstance(poll, WaitHttpJsonStep)
        self.assertEqual(poll.pointer, "/leds/Filter_Pump")
        self.assertEqual(poll.expected, "on")
        self.assertEqual(poll.poll_seconds, 0.2)
        self.assertEqual(poll.request_timeout_seconds, 0.5)
        self.assertIsNotNone(testcase.fixture)
        assert testcase.fixture is not None
        self.assertEqual(testcase.fixture.panel_type, "RS-4 Combo")

    def test_rejects_serial_steps_outside_rs485_runtime(self) -> None:
        error = self._load_error(
            """
schema: 1
id: pda.bad-serial
description: Wrong runtime
mode: physical-panel
access: read-write
requires: {protocol: pda}
steps:
  - serial_send: {bytes: "1002", timeout: 1s}
"""
        )
        self.assertIn("serial and HTTP steps require 'rs485'", str(error))

    def test_rejects_http_put_in_read_only_testcase(self) -> None:
        error = self._load_error(
            """
schema: 1
id: rs485.bad-http
description: Unsafe HTTP action
mode: rs485-panel-emulator
access: read-only
requires: {protocol: rs485}
fixture: {panel_type: RS-4 Combo, device_id: "0x0a"}
steps:
  - http_request:
      method: PUT
      path: /api/Filter_Pump/set
      value: 1
      timeout: 1s
"""
        )
        self.assertIn("HTTP PUT require read-write access", str(error))

    def test_loads_stateful_allbutton_panel_driver(self) -> None:
        testcase = load_testcase(
            Path(__file__).parents[1]
            / "testcases"
            / "rs485"
            / "allbutton-filter.yaml"
        )
        self.assertIsNotNone(testcase.fixture)
        assert testcase.fixture is not None
        self.assertEqual(testcase.fixture.driver, "allbutton")
        self.assertIsInstance(testcase.steps[1], ExpectPanelCommandStep)
        command = testcase.steps[1]
        assert isinstance(command, ExpectPanelCommandStep)
        self.assertEqual(command.command, 0x02)

    def test_suite_rejects_member_access_above_suite_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            testcase = root / "mutating.yaml"
            testcase.write_text(
                """
schema: 1
id: pda.mutating
description: Mutating member
mode: physical-panel
access: read-write
requires: {protocol: pda}
steps:
  - set_device:
      id: Filter_Pump
      state: on
      activation_timeout: 10s
      completion_timeout: 10s
finally:
  - restore_original_state: {}
""",
                encoding="utf-8",
            )
            suite = root / "suite.yaml"
            suite.write_text(
                """
schema: 1
kind: suite
id: pda.read-only
description: Unsafe suite access
mode: physical-panel
access: read-only
requires: {protocol: pda}
config: {}
testcases: [mutating.yaml]
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValidationError,
                "requires read-write suite access",
            ):
                load_testcase_suite(suite)

    def test_rejects_unknown_keys_with_location(self) -> None:
        error = self._load_error(
            """
schema: 1
id: pda.bad
description: Invalid test
mode: physical-panel
access: read-only
requires: {protocol: pda}
steps:
  - wait_for:
      condition: pda.initialized
      timeout: 10s
      typo: true
"""
        )
        self.assertIn("steps[0].wait_for: unknown key(s): typo", str(error))

    def test_rejects_mutation_without_cleanup(self) -> None:
        error = self._load_error(
            """
schema: 1
id: pda.unsafe
description: Missing cleanup
mode: physical-panel
access: read-write
requires: {protocol: pda}
steps:
  - set_device:
      id: Filter_Pump
      state: on
      activation_timeout: 10s
      completion_timeout: 10s
"""
        )
        self.assertIn("must restore_original_state", str(error))

    def test_rejects_mutation_declared_read_only(self) -> None:
        error = self._load_error(
            """
schema: 1
id: pda.unsafe
description: Wrong access
mode: physical-panel
access: read-only
requires: {protocol: pda}
steps:
  - set_device:
      id: Filter_Pump
      state: on
      activation_timeout: 10s
      completion_timeout: 10s
finally:
  - restore_original_state: {}
"""
        )
        self.assertIn("mutating steps require read-write", str(error))

    def test_rejects_unknown_keyword_and_unbounded_wait(self) -> None:
        unknown = self._load_error(
            """
schema: 1
id: pda.bad
description: Unknown action
mode: physical-panel
access: read-only
requires: {protocol: pda}
steps:
  - shell: {command: reboot}
"""
        )
        self.assertIn("unknown keyword 'shell'", str(unknown))

        missing_timeout = self._load_error(
            """
schema: 1
id: pda.bad
description: Unbounded wait
mode: physical-panel
access: read-only
requires: {protocol: pda}
steps:
  - wait_for: {condition: pda.initialized}
"""
        )
        self.assertIn("missing required key(s): timeout", str(missing_timeout))

    def test_rejects_duplicate_yaml_keys(self) -> None:
        error = self._load_error(
            """
schema: 1
id: pda.first
id: pda.second
description: Duplicate key
mode: physical-panel
access: read-only
requires: {protocol: pda}
steps: []
"""
        )
        self.assertIn("duplicate YAML key 'id'", str(error))

    def _load_error(self, contents: str) -> ValidationError:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testcase.yaml"
            path.write_text(contents, encoding="utf-8")
            with self.assertRaises(ValidationError) as caught:
                load_testcase(path)
            return caught.exception


if __name__ == "__main__":
    unittest.main()
