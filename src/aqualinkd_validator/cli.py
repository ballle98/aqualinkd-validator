from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .comparison import format_comparison, load_comparison
from .config import (
    ConfigurationError,
    normalize_api_base_url,
    sha256_file,
    validate_live_serial_device,
)
from .metadata import (
    collect_binary_metadata,
    collect_host_metadata,
    collect_source_metadata,
)
from .metrics import summarize_metrics
from .pda_scenario import PdaLivePanelScenario, PdaScenarioConfig
from .suites import SUITES, get_suite
from .supervisor import supervise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqualinkd-validator")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor", help="Report runtime capabilities without accessing hardware"
    )
    doctor.add_argument("--json", action="store_true", dest="as_json")

    compare = subparsers.add_parser(
        "compare", help="Compare performance artifacts from two or more runs"
    )
    compare.add_argument("artifact_dirs", nargs="+", type=Path)
    compare.add_argument("--json", action="store_true", dest="as_json")

    run = subparsers.add_parser(
        "run",
        help="Supervise AqualinkD and collect logs and process metrics",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    run.add_argument(
        "--aqualinkd",
        type=Path,
        default=Path("/usr/local/bin/aqualinkd"),
    )
    run.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/aqualinkd.conf"),
    )
    run.add_argument(
        "--artifacts",
        type=Path,
        default=Path("/tmp/aqualinkd-validator-artifacts"),
    )
    run.add_argument("--label", default="live-panel")
    run.add_argument(
        "--suite",
        choices=tuple(SUITES),
        action="append",
        dest="legacy_suites",
        help=argparse.SUPPRESS,
    )
    run.add_argument("--source-tree", type=Path)
    run.add_argument("--source-commit")
    run.add_argument("--source-branch")
    run.add_argument("--workdir", type=Path)
    run.add_argument("--duration", type=_positive_float)
    run.add_argument("--sample-interval", type=_positive_float, default=1.0)
    run.add_argument("--terminate-grace", type=_positive_float, default=10.0)
    run.add_argument(
        "--mode",
        choices=("live-panel", "jandy-simulator"),
        default="live-panel",
    )
    run.add_argument(
        "--serial-device",
        type=Path,
        help="Override and verify the serial_port read from the configuration",
    )
    panel_access = run.add_mutually_exclusive_group()
    panel_access.add_argument(
        "--panel",
        "--panel-read-only",
        action="store_const",
        const="read-only",
        dest="panel_access",
        help="Authorize access to a live panel without equipment changes",
    )
    panel_access.add_argument(
        "--panelw",
        "--panel-read-write",
        action="store_const",
        const="read-write",
        dest="panel_access",
        help="Authorize live-panel access and equipment changes",
    )
    run.add_argument(
        "--allow-live-panel",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--allow-equipment-control",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--api-base-url",
        help=(
            "Override the AqualinkD HTTP origin; PDA suites otherwise "
            "discover it from startup logs"
        ),
    )
    run.add_argument(
        "--pda-test-device",
        action="append",
        default=[],
        help=(
            "Restrict the long suite's consecutive-device phase to this "
            "switch ID; repeat for more than one (default: every discovered "
            "switch)"
        ),
    )
    run.add_argument(
        "--pda-activation-timeout",
        type=_positive_float,
        default=130.0,
        help="Maximum wait for a queued PDA programmer task to become active",
    )
    run.add_argument(
        "--pda-action-timeout",
        type=_positive_float,
        default=90.0,
        help="Maximum runtime after a PDA programmer task becomes active",
    )
    run.add_argument(
        "--pda-state-timeout",
        type=_positive_float,
        default=10.0,
        help="Maximum API state-convergence wait after programmer completion",
    )
    run.add_argument(
        "--pda-init-timeout",
        type=_positive_float,
        default=180.0,
    )
    run.add_argument(
        "--pda-sleep-timeout",
        type=_positive_float,
        default=120.0,
    )
    run.add_argument(
        "--pda-cleanup-timeout",
        type=_positive_float,
        default=180.0,
        help="Maximum time allowed for restoration after cancellation",
    )
    run.add_argument(
        "--panel-timezone",
        help=(
            "Override the local IANA timezone used to validate the panel clock"
        ),
    )
    run.add_argument(
        "--panel-time-tolerance",
        type=_positive_float,
        default=120.0,
        help="Maximum panel clock difference in seconds",
    )
    run.add_argument(
        "suites",
        nargs="*",
        choices=tuple(SUITES),
        help="Validation suites to run sequentially",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            exit_code = _doctor(args.as_json)
        elif args.command == "compare":
            exit_code = _compare(args.artifact_dirs, args.as_json)
        elif args.command == "run":
            exit_code = _run(args)
        else:
            parser.error(f"unknown command: {args.command}")
    except (
        ConfigurationError,
        FileNotFoundError,
        PermissionError,
        ValueError,
    ) as error:
        parser.exit(2, f"error: {error}\n")
    raise SystemExit(exit_code)


def _doctor(as_json: bool) -> int:
    report: dict[str, Any] = {
        "validator_version": __version__,
        "host": collect_host_metadata(),
        "capabilities": {
            "linux_procfs": Path("/proc/self/stat").exists(),
            "pty": hasattr(os, "openpty"),
            "running_as_root": os.geteuid() == 0,
        },
    }
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"AqualinkD Validator {__version__}")
        print(f"Platform: {report['host']['platform']}")
        print(f"Architecture: {report['host']['architecture']}")
        print(f"Container: {report['host']['container'] or 'no'}")
        for name, available in report["capabilities"].items():
            print(f"{name}: {'yes' if available else 'no'}")
    return 0


def _compare(artifact_dirs: list[Path], as_json: bool) -> int:
    comparison = load_comparison(artifact_dirs)
    if as_json:
        print(json.dumps(comparison, indent=2, sort_keys=True))
    else:
        print(format_comparison(comparison))
    return 0


def _run(args: argparse.Namespace) -> int:
    positional_suites = list(args.suites)
    legacy_suites = list(args.legacy_suites or ())
    if positional_suites and legacy_suites:
        raise ConfigurationError(
            "Specify suites positionally; do not combine them with --suite"
        )
    suite_names: list[str | None] = positional_suites or legacy_suites or [None]
    selected_names = [name for name in suite_names if name is not None]
    if len(selected_names) != len(set(selected_names)):
        raise ConfigurationError("Each suite may be selected only once per run")
    for selected_name in selected_names:
        suite = get_suite(selected_name)
        assert suite is not None
        if args.mode != suite.mode:
            raise ConfigurationError(
                f"{suite.name} requires --mode {suite.mode}, not {args.mode}"
            )

    if args.panel_access == "read-only" and args.allow_equipment_control:
        raise ConfigurationError(
            "--panel-read-only conflicts with --allow-equipment-control"
        )
    args.allow_equipment_control = bool(
        args.allow_equipment_control or args.panel_access == "read-write"
    )
    args.allow_live_panel = bool(
        args.allow_live_panel
        or args.allow_equipment_control
        or args.panel_access is not None
    )
    if not args.allow_live_panel:
        raise ConfigurationError(
            f"{args.mode} mode requires --panel-read-only or "
            "--panel-read-write because it opens a real or externally "
            "managed serial bus"
        )
    if selected_names and not args.allow_equipment_control:
        raise ConfigurationError(
            "Selected live-panel suites change physical equipment and require "
            "--panel-read-write"
        )
    if args.pda_test_device and "pda-live-long" not in selected_names:
        raise ConfigurationError(
            "--pda-test-device requires the pda-live-long suite"
        )
    if args.panel_timezone is None:
        args.panel_timezone = _local_timezone_name()

    original_label = args.label
    for suite_name in suite_names:
        args.suite = suite_name
        args.label = (
            f"{original_label}-{suite_name}"
            if len(suite_names) > 1 and suite_name is not None
            else original_label
        )
        current_exit_code = _run_one(args)
        if current_exit_code != 0:
            return current_exit_code
    return 0


def _run_one(args: argparse.Namespace) -> int:

    binary = args.aqualinkd.expanduser().resolve(strict=True)
    config = args.config.expanduser().resolve(strict=True)
    serial_device = validate_live_serial_device(config, args.serial_device)
    source_tree = (
        args.source_tree.expanduser().resolve(strict=True)
        if args.source_tree is not None
        else None
    )
    workdir = (
        args.workdir.expanduser().resolve(strict=True)
        if args.workdir is not None
        else binary.parent
    )
    suite = get_suite(args.suite)
    if suite is not None and args.mode != suite.mode:
        raise ConfigurationError(
            f"{suite.name} requires --mode {suite.mode}, not {args.mode}"
        )
    scenario: PdaLivePanelScenario | None = None
    api_base_url: str | None = None
    suite_test_devices: list[str] = []
    if suite is not None:
        suite_test_devices = (
            args.pda_test_device if suite.include_state_waits else []
        )
        api_base_url = (
            normalize_api_base_url(args.api_base_url)
            if args.api_base_url is not None
            else None
        )
        scenario = PdaLivePanelScenario(
            None,
            PdaScenarioConfig(
                suite_name=suite.name,
                include_state_waits=suite.include_state_waits,
                activation_timeout_seconds=args.pda_activation_timeout,
                action_timeout_seconds=args.pda_action_timeout,
                state_timeout_seconds=args.pda_state_timeout,
                init_timeout_seconds=args.pda_init_timeout,
                sleep_timeout_seconds=args.pda_sleep_timeout,
                test_devices=tuple(suite_test_devices),
                panel_timezone=args.panel_timezone,
                panel_time_tolerance_seconds=args.panel_time_tolerance,
            ),
            api_base_url_override=api_base_url,
        )
    artifact_dir = _new_artifact_dir(args.artifacts, args.label)
    command = build_aqualinkd_command(binary, config, args.suite)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "validator_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "mode": args.mode,
        "suite": (
            {
                "name": suite.name,
                "description": suite.description,
                "mode": suite.mode,
                "aqualinkd_args": list(suite.aqualinkd_args),
                "include_state_waits": suite.include_state_waits,
            }
            if suite is not None
            else None
        ),
        "command": command,
        "equipment_control": {
            "explicitly_authorized": args.allow_equipment_control,
            "api_base_url": api_base_url,
            "api_endpoint_source": (
                "explicit_override"
                if api_base_url is not None
                else "aqualinkd_startup_log"
            ),
            "pda_test_devices": suite_test_devices,
            "pda_device_selection": (
                "not_applicable"
                if suite is None or not suite.include_state_waits
                else (
                    "restricted"
                    if suite_test_devices
                    else "all_discovered_switches"
                )
            ),
            "panel_time": {
                "timezone": args.panel_timezone,
                "tolerance_seconds": args.panel_time_tolerance,
            },
            "timeouts_seconds": {
                "activation": args.pda_activation_timeout,
                "action": args.pda_action_timeout,
                "cleanup": args.pda_cleanup_timeout,
                "init": args.pda_init_timeout,
                "sleep": args.pda_sleep_timeout,
                "state": args.pda_state_timeout,
            },
        },
        "host": collect_host_metadata(),
        "aqualinkd": collect_binary_metadata(binary),
        "source": collect_source_metadata(
            source_tree,
            source_commit=args.source_commit,
            source_branch=args.source_branch,
        ),
        "config": {
            "name": config.name,
            "sha256": sha256_file(config),
        },
        "serial": {
            "device": str(serial_device),
            "source": (
                "explicit_override"
                if args.serial_device is not None
                else "aqualinkd_config"
            ),
        },
        "sampling": {
            "interval_seconds": args.sample_interval,
            "clock": "CLOCK_MONOTONIC",
        },
    }
    _write_json(artifact_dir / "manifest.yaml", manifest)
    print(f"Artifacts: {artifact_dir}", flush=True)
    print(f"AqualinkD: {binary}", flush=True)
    print(f"Config fingerprint: {manifest['config']['sha256']}", flush=True)
    print(f"Serial device: {serial_device}", flush=True)
    print(f"Mode: {args.mode}", flush=True)
    print(f"Suite: {suite.name if suite is not None else 'none'}", flush=True)

    try:
        result = asyncio.run(
            supervise(
                command,
                artifact_dir,
                cwd=workdir,
                duration_seconds=args.duration,
                sample_interval_seconds=args.sample_interval,
                terminate_grace_seconds=args.terminate_grace,
                scenario=scenario,
                scenario_cleanup_seconds=args.pda_cleanup_timeout,
            )
        )
    except KeyboardInterrupt:
        result_data = {
            "status": "failed",
            "reason": "keyboard_interrupt",
            "finished_at": datetime.now(UTC).isoformat(),
        }
        _write_json(artifact_dir / "result.json", result_data)
        print("Interrupted; AqualinkD cleanup requested.", file=sys.stderr)
        return 130

    result_data = {
        **asdict(result),
        "finished_at": datetime.now(UTC).isoformat(),
    }
    performance = {
        "schema_version": 1,
        "label": args.label,
        "process": summarize_metrics(artifact_dir / "metrics.jsonl"),
        "environment_after": collect_host_metadata(),
    }
    scenario_path = artifact_dir / "scenario.json"
    if scenario_path.exists():
        scenario_data = json.loads(
            scenario_path.read_text(encoding="utf-8")
        )
        performance["scenario"] = scenario_data
        manifest["equipment_control"]["api_base_url"] = scenario_data.get(
            "api_base_url"
        )
        manifest["equipment_control"]["api_endpoint_source"] = (
            scenario_data.get("api_endpoint_source")
        )
        _write_json(artifact_dir / "manifest.yaml", manifest)
    _write_json(artifact_dir / "performance.json", performance)
    _write_json(artifact_dir / "result.json", result_data)
    print(
        f"Result: {result.status} ({result.reason}), "
        f"child return code {result.child_returncode}",
        flush=True,
    )
    return 0 if result.status == "passed" else 1


def build_aqualinkd_command(
    binary: Path,
    config: Path,
    suite_name: str | None,
) -> list[str]:
    command = [str(binary), "-d", "-c", str(config)]
    suite = get_suite(suite_name)
    if suite is not None:
        command.extend(suite.aqualinkd_args)
    return command


def _new_artifact_dir(root: Path, label: str) -> Path:
    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", label).strip("-") or "run"
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    for suffix in range(1000):
        name = f"{timestamp}-{safe_label}"
        if suffix:
            name = f"{name}-{suffix}"
        candidate = root / name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError("unable to allocate a unique artifact directory")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _local_timezone_name() -> str:
    candidates: list[str] = []
    environment_timezone = os.environ.get("TZ")
    if environment_timezone:
        candidates.append(environment_timezone.lstrip(":"))

    timezone_file = Path("/etc/timezone")
    with contextlib.suppress(OSError):
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())

    try:
        localtime_target = Path("/etc/localtime").resolve(strict=True)
    except OSError:
        pass
    else:
        marker = "/usr/share/zoneinfo/"
        target_text = str(localtime_target)
        if marker in target_text:
            candidates.append(target_text.partition(marker)[2])

    candidates.append("UTC")
    for candidate in candidates:
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except ZoneInfoNotFoundError:
            continue
        return candidate
    return "UTC"


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed
