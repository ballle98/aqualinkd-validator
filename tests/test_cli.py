from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from aqualinkd_validator.cli import build_aqualinkd_command, build_parser, main
from aqualinkd_validator.config import (
    ConfigurationError,
    validate_live_serial_device,
)


class CliTests(unittest.TestCase):
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

    def test_pda_suites_add_serial_debug_argument(self) -> None:
        for suite in ("pda-live-fast", "pda-live-long"):
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
                "--pda-test-device requires the pda-live-long suite",
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


if __name__ == "__main__":
    unittest.main()
