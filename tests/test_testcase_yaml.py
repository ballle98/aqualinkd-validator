from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from aqualinkd_validator.cli import main
from aqualinkd_validator.testcases import (
    ExerciseHeaterStep,
    RestoreOriginalStateStep,
    SetDeviceStep,
    TestcaseValidationError,
    WaitForStep,
    load_testcase,
)


class TestcaseYamlTests(unittest.TestCase):
    def test_cli_validates_without_starting_aqualinkd(self) -> None:
        path = (
            Path(__file__).parents[1]
            / "testcases"
            / "pda"
            / "filter-after-init.yaml"
        )
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as result:
            main(["validate-testcase", str(path)])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("pda.filter-after-init, 3 step(s)", output.getvalue())

    def test_loads_versioned_testcase_into_typed_steps(self) -> None:
        testcase = load_testcase(
            Path(__file__).parents[1]
            / "testcases"
            / "pda"
            / "filter-after-init.yaml"
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
            Path(__file__).parents[1]
            / "testcases"
            / "pda"
            / "pool-heater.yaml"
        )
        heater = testcase.steps[1]
        self.assertIsInstance(heater, ExerciseHeaterStep)
        assert isinstance(heater, ExerciseHeaterStep)
        self.assertTrue(heater.optional)
        self.assertEqual(heater.identifier, "Pool_Heater")

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

    def _load_error(self, contents: str) -> TestcaseValidationError:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "testcase.yaml"
            path.write_text(contents, encoding="utf-8")
            with self.assertRaises(TestcaseValidationError) as caught:
                load_testcase(path)
            return caught.exception


if __name__ == "__main__":
    unittest.main()
