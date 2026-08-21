from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.run_targets import RUN_TARGETS


class RunTargetRegistryTests(unittest.TestCase):
    def test_named_declarative_suite_is_fully_normalized(self) -> None:
        target = RUN_TARGETS.resolve("pda-live-awake")

        self.assertEqual(target.identifier, "pda-live-awake")
        self.assertEqual(target.kind, "suite")
        self.assertEqual(target.mode, "live-panel")
        self.assertTrue(target.mutates_panel)
        self.assertTrue(target.uses_selected_devices)
        self.assertEqual(target.override_map(), {"pda_sleep_mode": "no"})
        self.assertEqual(len(target.testcases), 5)

    def test_composite_inherits_member_authorization_requirements(self) -> None:
        target = RUN_TARGETS.resolve("pda-live-long")

        self.assertTrue(target.is_composite)
        self.assertTrue(target.mutates_panel)
        self.assertTrue(target.uses_selected_devices)
        self.assertEqual(target.members, ("pda-live-awake", "pda-live-sleep"))

    def test_aquapda_python_suite_uses_same_resolved_model(self) -> None:
        target = RUN_TARGETS.resolve("aquapda-live-panel-menu-walk")

        self.assertEqual(target.kind, "python-suite")
        self.assertFalse(target.mutates_panel)
        self.assertEqual(target.aqualinkd_args, ("-vv",))
        self.assertEqual(len(target.case_ids), 3)

    def test_yaml_path_uses_same_resolution_path_as_builtin_name(self) -> None:
        builtin = RUN_TARGETS.resolve("pda-live-fast")
        assert builtin.source is not None

        explicit = RUN_TARGETS.resolve(str(builtin.source))

        self.assertEqual(explicit, builtin)

    def test_unsupported_declarative_mode_fails_during_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            testcase = Path(directory) / "emulator.yaml"
            testcase.write_text(
                """\
schema: 1
id: pda.emulator
description: Emulator-only case
mode: rs485-panel-emulator
access: read-only
requires:
  protocol: pda
steps:
  - assert_log:
      contains: ready
      timeout: 1s
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "is not implemented"):
                RUN_TARGETS.resolve(str(testcase))


if __name__ == "__main__":
    unittest.main()
