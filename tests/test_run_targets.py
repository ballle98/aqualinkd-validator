from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.run_targets import RUN_TARGETS, RuntimeCaseId


class RunTargetRegistryTests(unittest.TestCase):
    def test_pda_time_suite_uses_isolated_clock_and_disables_sleep(self) -> None:
        target = RUN_TARGETS.resolve("pda-live-time")

        self.assertEqual(
            target.case_ids,
            (RuntimeCaseId.INITIALIZATION, RuntimeCaseId.PDA_TIME_SYNC),
        )
        self.assertEqual(target.fake_time_offset_seconds, 7200)
        self.assertTrue(target.mutates_panel)
        self.assertEqual(
            target.override_map(),
            {
                "pda_sleep_mode": "no",
                "sync_panel_time": "yes",
                "sync_time_accurately": "yes",
            },
        )

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

    def test_power_center_suite_reuses_live_pda_testcases(self) -> None:
        live = RUN_TARGETS.resolve("pda-live-awake")
        power_center = RUN_TARGETS.resolve("pda-power-center-awake")

        self.assertEqual(power_center.identifier, "pda-power-center-awake")
        self.assertEqual(power_center.mode, "jandy-power-center")
        self.assertEqual(power_center.source, live.source)
        self.assertEqual(power_center.testcases, live.testcases)
        self.assertEqual(
            power_center.override_map(),
            {
                "pda_sleep_mode": "no",
                "panel_type": "PD-16 Combo",
                "device_id": "0x60",
                "rssa_device_id": "0x00",
                "extended_device_id": "0x00",
                "enable_iaqualink": "no",
                "sync_panel_time": "no",
            },
        )
        self.assertEqual(power_center.artifact_suffix, "awake")

    def test_power_center_sleep_omits_unsupported_natural_wake(self) -> None:
        target = RUN_TARGETS.resolve("pda-power-center-sleep")

        self.assertEqual(target.mode, "jandy-power-center")
        self.assertEqual(
            [testcase.identifier for testcase in target.testcases],
            [
                "pda.initialization",
                "pda.status-retry-command",
                "pda.probe-transition-command",
            ],
        )
        self.assertEqual(
            target.override_map(),
            {
                "pda_sleep_mode": "yes",
                "panel_type": "PD-16 Combo",
                "device_id": "0x60",
                "rssa_device_id": "0x00",
                "extended_device_id": "0x00",
                "enable_iaqualink": "no",
                "sync_panel_time": "no",
            },
        )
        self.assertEqual(target.execution_role, "sleep")

    def test_power_center_full_serializes_awake_sleep_and_menu_walk(self) -> None:
        target = RUN_TARGETS.resolve("pda-power-center-full")

        self.assertTrue(target.is_composite)
        self.assertEqual(target.mode, "jandy-power-center")
        self.assertTrue(target.mutates_panel)
        self.assertTrue(target.uses_selected_devices)
        self.assertEqual(
            target.members,
            (
                "pda-power-center-awake",
                "pda-power-center-sleep",
                "pda-power-center-service",
                "pda-power-center-freeze",
                "aquapda-power-center-menu-walk",
            ),
        )

    def test_aquapda_python_suite_uses_same_resolved_model(self) -> None:
        target = RUN_TARGETS.resolve("aquapda-live-panel-menu-walk")

        self.assertEqual(target.kind, "python-suite")
        self.assertFalse(target.mutates_panel)
        self.assertEqual(target.aqualinkd_args, ("-vv",))
        self.assertEqual(len(target.case_ids), 3)

        power_center = RUN_TARGETS.resolve("aquapda-power-center-menu-walk")
        self.assertEqual(
            power_center.override_map(),
            {
                "pda_sleep_mode": "no",
                "panel_type": "PD-16 Combo",
                "device_id": "0x60",
                "rssa_device_id": "0x00",
                "extended_device_id": "0x00",
                "enable_iaqualink": "no",
                "sync_panel_time": "no",
            },
        )

    def test_rs_suites_use_mode_as_execution_target(self) -> None:
        for suite in ("rs-fast", "rs-full"):
            with self.subTest(suite=suite):
                live = RUN_TARGETS.resolve(suite)
                power_center = RUN_TARGETS.resolve(
                    suite, mode="jandy-power-center"
                )

                self.assertEqual(live.mode, "live-panel")
                self.assertEqual(live.override_map(), {})
                self.assertEqual(power_center.mode, "jandy-power-center")
                self.assertEqual(power_center.protocol, "rs")
                self.assertTrue(power_center.mutates_panel)
                self.assertEqual(
                    power_center.override_map(),
                    {
                        "panel_type": "RS-16 Combo",
                        "device_id": "0x0b",
                        "rssa_device_id": "0x48",
                        "extended_device_id": "0x30",
                        "enable_iaqualink": "yes",
                    },
                )

        self.assertEqual(len(RUN_TARGETS.resolve("rs-fast").rs_case_ids), 3)
        self.assertEqual(len(RUN_TARGETS.resolve("rs-full").rs_case_ids), 3)

    def test_rs_time_suite_reuses_isolated_clock_in_power_center_mode(self) -> None:
        target = RUN_TARGETS.resolve("rs-time", mode="jandy-power-center")

        self.assertEqual(target.rs_case_ids, ("rs-initialization", "rs-time-sync"))
        self.assertEqual(target.fake_time_offset_seconds, 7200)
        self.assertEqual(
            target.override_map(),
            {
                "sync_panel_time": "yes",
                "sync_time_accurately": "yes",
                "rssa_device_id": "0x00",
                "extended_device_id": "0x00",
                "enable_iaqualink": "no",
                "panel_type": "RS-16 Combo",
                "device_id": "0x0b",
            },
        )

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
  protocol: rs485
fixture:
  panel_type: RS-4 Combo
  device_id: "0x0a"
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
