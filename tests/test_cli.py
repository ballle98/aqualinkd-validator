from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from aqualinkd_validator.cli import (
    _run_composite_suite,
    _run_process_suite,
    build_aqualinkd_command,
    build_parser,
    main,
)
from aqualinkd_validator.config import (
    ConfigurationError,
    read_config_value,
    validate_live_serial_device,
)


class CliTests(unittest.TestCase):
    def test_long_suite_runs_awake_then_sleep_with_derived_configs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\n"
                "pda_sleep_mode=yes\n"
                "mqtt_password=do-not-record\n",
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "run",
                    "--panel-read-write",
                    "--config",
                    str(config),
                    "--label",
                    "baseline",
                    "pda-live-long",
                ]
            )
            args.suite = "pda-live-long"
            observed: list[tuple[str, str, str | None, Path, dict[str, str]]] = []
            derived_paths: list[Path] = []

            def record_phase(phase_args: argparse.Namespace) -> int:
                derived_paths.append(phase_args.config)
                phase_args.last_run_safe_to_continue = True
                observed.append(
                    (
                        phase_args.suite,
                        phase_args.label,
                        read_config_value(
                            phase_args.config,
                            "pda_sleep_mode",
                        ),
                        phase_args.source_config,
                        phase_args.config_overrides,
                    )
                )
                return 0

            with patch(
                "aqualinkd_validator.cli._run_process",
                side_effect=record_phase,
            ):
                self.assertEqual(_run_composite_suite(args), 0)

            self.assertEqual(
                observed,
                [
                    (
                        "pda-live-awake",
                        "baseline-awake",
                        "no",
                        config,
                        {"pda_sleep_mode": "no"},
                    ),
                    (
                        "pda-live-sleep",
                        "baseline-sleep",
                        "yes",
                        config,
                        {"pda_sleep_mode": "yes"},
                    ),
                ],
            )
            self.assertTrue(all(not path.exists() for path in derived_paths))
            self.assertEqual(
                read_config_value(config, "pda_sleep_mode"),
                "yes",
            )

    def test_long_suite_continues_after_restored_awake_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\npda_sleep_mode=yes\n",
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "run",
                    "--panel-read-write",
                    "--config",
                    str(config),
                    "pda-live-long",
                ]
            )
            args.suite = "pda-live-long"
            observed: list[str] = []

            def fail_awake(phase_args: argparse.Namespace) -> int:
                observed.append(phase_args.suite)
                phase_args.last_run_safe_to_continue = True
                return 1 if phase_args.suite == "pda-live-awake" else 0

            with patch(
                "aqualinkd_validator.cli._run_process",
                side_effect=fail_awake,
            ):
                self.assertEqual(_run_composite_suite(args), 1)

            self.assertEqual(
                observed,
                ["pda-live-awake", "pda-live-sleep"],
            )

    def test_long_suite_stops_when_awake_cannot_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\npda_sleep_mode=yes\n",
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "run",
                    "--panel-read-write",
                    "--config",
                    str(config),
                    "pda-live-long",
                ]
            )
            args.suite = "pda-live-long"
            observed: list[str] = []

            def fail_unsafely(phase_args: argparse.Namespace) -> int:
                observed.append(phase_args.suite)
                phase_args.last_run_safe_to_continue = False
                return 1

            with patch(
                "aqualinkd_validator.cli._run_process",
                side_effect=fail_unsafely,
            ):
                self.assertEqual(_run_composite_suite(args), 1)

            self.assertEqual(observed, ["pda-live-awake"])

    def test_sleep_suite_can_run_independently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\npda_sleep_mode=no\n",
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                [
                    "run",
                    "--panel-read-write",
                    "--config",
                    str(config),
                    "pda-live-sleep",
                ]
            )
            args.suite = "pda-live-sleep"
            observed: list[tuple[str, str | None]] = []

            def record_phase(phase_args: argparse.Namespace) -> int:
                phase_args.last_run_safe_to_continue = True
                observed.append(
                    (
                        phase_args.suite,
                        read_config_value(
                            phase_args.config,
                            "pda_sleep_mode",
                        ),
                    )
                )
                return 0

            with patch(
                "aqualinkd_validator.cli._run_process",
                side_effect=record_phase,
            ):
                self.assertEqual(_run_process_suite(args), 0)

            self.assertEqual(observed, [("pda-live-sleep", "yes")])
            self.assertEqual(read_config_value(config, "pda_sleep_mode"), "no")

    def test_run_uses_installed_paths_and_tmp_artifacts_by_default(self) -> None:
        args = build_parser().parse_args(
            ["run", "--panel-read-write", "pda-live-fast"]
        )
        self.assertEqual(args.aqualinkd, Path("/usr/local/bin/aqualinkd"))
        self.assertEqual(args.config, Path("/etc/aqualinkd.conf"))
        self.assertIsNone(args.serial_device)
        self.assertEqual(
            args.artifacts,
            Path("/tmp/aqualinkd-validator-artifacts"),
        )
        self.assertEqual(args.pda_cleanup_timeout, 300.0)

    def test_run_rejects_unknown_positional_suite(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as context:
            build_parser().parse_args(
                ["run", "--panel-read-write", "not-a-suite"]
            )

        self.assertEqual(context.exception.code, 2)
        self.assertIn("unknown suite 'not-a-suite'", stderr.getvalue())

    def test_pda_suites_add_serial_debug_argument(self) -> None:
        for suite in (
            "pda-live-fast",
            "pda-live-awake",
            "pda-live-sleep",
        ):
            with self.subTest(suite=suite):
                command = build_aqualinkd_command(
                    Path("/opt/aqualinkd"),
                    Path("/etc/aqualinkd.conf"),
                    suite,
                )
                self.assertEqual(
                    command,
                    [
                        "/opt/aqualinkd",
                        "-d",
                        "-c",
                        "/etc/aqualinkd.conf",
                        "-vv",
                    ],
                )

    def test_pda_live_panel_suite_rejects_simulator_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mock-aqualinkd"
            mock.write_text("#!/bin/sh\n", encoding="utf-8")
            mock.chmod(0o755)
            config = root / "aqualinkd.conf"
            config.write_text("serial_port=/dev/null\n", encoding="utf-8")

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as exit_context:
                main(
                    [
                        "run",
                        "--mode",
                        "jandy-simulator",
                        "--panel-read-write",
                        "--serial-device",
                        "/dev/null",
                        "--aqualinkd",
                        str(mock),
                        "--config",
                        str(config),
                        "pda-live-long",
                    ]
                )
            self.assertEqual(exit_context.exception.code, 2)
            self.assertIn("requires --mode live-panel", stderr.getvalue())

    def test_run_requires_matching_explicit_character_device(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "aqualinkd.conf"
            config.write_text("serial_port=/dev/null\n", encoding="utf-8")
            self.assertEqual(
                validate_live_serial_device(config),
                Path("/dev/null"),
            )
            self.assertEqual(
                validate_live_serial_device(config, Path("/dev/null")),
                Path("/dev/null"),
            )
            with self.assertRaisesRegex(ConfigurationError, "does not match"):
                validate_live_serial_device(config, Path("/dev/zero"))

    def test_pda_suite_requires_equipment_control_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mock-aqualinkd"
            mock.write_text("#!/bin/sh\n", encoding="utf-8")
            mock.chmod(0o755)
            config = root / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\n"
                "listen_address=http://0.0.0.0:80\n",
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as exit_context:
                main(
                    [
                        "run",
                        "--panel-read-only",
                        "--serial-device",
                        "/dev/null",
                        "--aqualinkd",
                        str(mock),
                        "--config",
                        str(config),
                        "pda-live-fast",
                    ]
                )
            self.assertEqual(exit_context.exception.code, 2)
            self.assertIn("--panel-read-write", stderr.getvalue())

    def test_pda_suite_defaults_to_local_timezone(self) -> None:
        observed: list[str] = []

        def record_timezone(args: argparse.Namespace) -> int:
            observed.append(args.panel_timezone)
            return 0

        with (
            patch.dict("os.environ", {"TZ": "America/Chicago"}),
            patch(
                "aqualinkd_validator.cli._run_one",
                side_effect=record_timezone,
            ),
            self.assertRaises(SystemExit) as exit_context,
        ):
            main(["run", "--panel-read-write", "pda-live-fast"])
        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(observed, ["America/Chicago"])

    def test_multiple_positional_suites_run_sequentially(self) -> None:
        observed: list[tuple[str | None, str]] = []

        def record_suite(args: argparse.Namespace) -> int:
            observed.append((args.suite, args.label))
            return 0

        with (
            patch(
                "aqualinkd_validator.cli._run_one",
                side_effect=record_suite,
            ),
            self.assertRaises(SystemExit) as exit_context,
        ):
            main(
                [
                    "run",
                    "--panel-read-write",
                    "pda-live-fast",
                    "pda-live-long",
                ]
            )
        self.assertEqual(exit_context.exception.code, 0)
        self.assertEqual(
            observed,
            [
                ("pda-live-fast", "live-panel-pda-live-fast"),
                ("pda-live-long", "live-panel-pda-live-long"),
            ],
        )

    def test_multiple_suites_stop_after_failure(self) -> None:
        observed: list[str | None] = []

        def fail_suite(args: argparse.Namespace) -> int:
            observed.append(args.suite)
            return 1

        with (
            patch(
                "aqualinkd_validator.cli._run_one",
                side_effect=fail_suite,
            ),
            self.assertRaises(SystemExit) as exit_context,
        ):
            main(
                [
                    "run",
                    "--panel-read-write",
                    "pda-live-fast",
                    "pda-live-long",
                ]
            )
        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(observed, ["pda-live-fast"])

    def test_fast_pda_suite_rejects_consecutive_devices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mock-aqualinkd"
            mock.write_text("#!/bin/sh\n", encoding="utf-8")
            mock.chmod(0o755)
            config = root / "aqualinkd.conf"
            config.write_text(
                "serial_port=/dev/null\n"
                "listen_address=http://0.0.0.0:80\n",
                encoding="utf-8",
            )

            stderr = io.StringIO()
            with redirect_stderr(stderr), self.assertRaises(
                SystemExit
            ) as exit_context:
                main(
                    [
                        "run",
                        "--panel-read-write",
                        "--pda-test-device",
                        "Aux_1",
                        "--panel-timezone",
                        "UTC",
                        "--serial-device",
                        "/dev/null",
                        "--aqualinkd",
                        str(mock),
                        "--config",
                        str(config),
                        "pda-live-fast",
                    ]
                )
            self.assertEqual(exit_context.exception.code, 2)
            self.assertIn(
                "--pda-test-device requires a suite containing "
                "device-focused validation",
                stderr.getvalue(),
            )

    def test_run_creates_performance_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mock = root / "mock-aqualinkd"
            mock.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "import time\n"
                "print('mock started', flush=True)\n"
                "print('arguments: ' + ' '.join(sys.argv[1:]), flush=True)\n"
                "print('mock warning', file=sys.stderr, flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            mock.chmod(0o755)
            config = root / "aqualinkd.conf"
            config.write_text("serial_port=/dev/null\n", encoding="utf-8")
            artifacts = root / "artifacts"

            with self.assertRaises(SystemExit) as exit_context:
                main(
                    [
                        "run",
                        "--mode",
                        "live-panel",
                        "--panel-read-only",
                        "--aqualinkd",
                        str(mock),
                        "--config",
                        str(config),
                        "--duration",
                        "0.12",
                        "--sample-interval",
                        "0.02",
                        "--terminate-grace",
                        "1",
                        "--label",
                        "mock",
                        "--artifacts",
                        str(artifacts),
                    ]
                )
            self.assertEqual(exit_context.exception.code, 0)
            run_dirs = list(artifacts.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            result = json.loads((run_dir / "result.json").read_text())
            performance = json.loads((run_dir / "performance.json").read_text())
            manifest = json.loads((run_dir / "manifest.yaml").read_text())
            self.assertEqual(result["status"], "passed")
            self.assertGreater(performance["process"]["sample_count"], 1)
            self.assertEqual(manifest["config"]["name"], "aqualinkd.conf")
            self.assertIsNone(manifest["suite"])
            stdout = (run_dir / "stdout.log").read_text()
            self.assertIn("mock started", stdout)
            self.assertIn("arguments: -d -c", stdout)
            summary = (run_dir / "summary.log").read_text()
            self.assertIn(f"Artifacts: {run_dir}", summary)
            self.assertIn("[AQUALINKD STDERR] mock warning", summary)
            self.assertIn("Result: passed (duration_elapsed)", summary)
            self.assertNotIn("mock started", summary)


if __name__ == "__main__":
    unittest.main()
