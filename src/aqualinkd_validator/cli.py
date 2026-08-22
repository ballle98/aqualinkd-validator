from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
from .adapters import LocalProcessRunner
from .comparison import format_comparison, load_comparison
from .config import (
    ConfigurationError,
    normalize_api_base_url,
    read_disabled_button_numbers,
    sha256_file,
    validate_live_serial_device,
    write_config_with_overrides,
)
from .metadata import (
    collect_binary_metadata,
    collect_host_metadata,
    collect_source_metadata,
)
from .metrics import summarize_metrics
from .panel_free import run_panel_free_testcase
from .pda_scenario import PdaScenarioConfig, PdaScenarioRuntime
from .run_targets import RUN_TARGETS, ResolvedRunTarget
from .site_config import SiteConfig, load_site_config
from .testcases import (
    TestcaseSuiteDefinition,
    load_testcase_document,
)

_RUN_SUITE_NAMES = RUN_TARGETS.names


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

    validate = subparsers.add_parser(
        "validate-testcase",
        help="Validate declarative testcase YAML without starting AqualinkD",
    )
    validate.add_argument("paths", nargs="+", type=Path, metavar="TESTCASE")

    panel_free = subparsers.add_parser(
        "run-panel-free",
        help="Run one RS485 YAML testcase against an isolated AqualinkD process",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    panel_free.add_argument("testcase", type=Path)
    panel_free.add_argument(
        "--aqualinkd", type=Path, default=Path("/usr/local/bin/aqualinkd")
    )
    panel_free.add_argument(
        "--web-directory", type=Path, default=Path("/var/www/aqualinkd")
    )
    panel_free.add_argument(
        "--artifacts",
        type=Path,
        default=Path("/tmp/aqualinkd-validator-artifacts"),
    )
    panel_free.add_argument("--label", default="panel-free")
    panel_free.add_argument("--duration", type=_positive_float, default=60.0)
    panel_free.add_argument(
        "--http-ready-timeout", type=_positive_float, default=10.0
    )
    panel_free.add_argument("--sample-interval", type=_positive_float, default=1.0)
    panel_free.add_argument("--terminate-grace", type=_positive_float, default=10.0)

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
        "--site-config",
        type=Path,
        help=(
            "Installation-specific validator settings; defaults to "
            "aqualinkd-validator.yaml beside the AqualinkD config"
        ),
    )
    run.add_argument(
        "--artifacts",
        type=Path,
        default=Path("/tmp/aqualinkd-validator-artifacts"),
    )
    run.add_argument("--label", default="live-panel")
    run.add_argument("--source-tree", type=Path)
    run.add_argument("--source-commit")
    run.add_argument("--source-branch")
    run.add_argument("--workdir", type=Path)
    run.add_argument("--duration", type=_positive_float)
    run.add_argument("--sample-interval", type=_positive_float, default=1.0)
    run.add_argument("--terminate-grace", type=_positive_float, default=10.0)
    run.add_argument(
        "--mode",
        choices=("live-panel", "jandy-power-center"),
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
            "Restrict device-focused validation to this switch ID; repeat "
            "for more than one (default: all discovered switches for the "
            "awake suite and the highest eligible auxiliary for the sleep "
            "suite)"
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
        "--pda-status-timeout",
        type=_positive_float,
        default=180.0,
        help="Maximum wait for the complete PDA equipment-status loop",
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
        default=300.0,
        help=(
            "Maximum wait for delayed equipment restoration and restoration "
            "after cancellation"
        ),
    )
    run.add_argument(
        "--panel-timezone",
        help=("Override the local IANA timezone used to validate the panel clock"),
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
        type=_run_target,
        metavar="SUITE_OR_TESTCASE",
        help="Validation suite names or YAML testcase paths to run sequentially",
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
        elif args.command == "validate-testcase":
            exit_code = _validate_testcases(args.paths)
        elif args.command == "run-panel-free":
            exit_code = _run_panel_free(args)
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


def _validate_testcases(paths: list[Path]) -> int:
    for path in paths:
        document = load_testcase_document(path)
        if isinstance(document, TestcaseSuiteDefinition):
            print(
                f"Valid: {path} ({document.identifier}, "
                f"{len(document.members)} testcase(s))"
            )
        else:
            print(
                f"Valid: {path} ({document.identifier}, {len(document.steps)} step(s))"
            )
    return 0


def _run_panel_free(args: argparse.Namespace) -> int:
    document = load_testcase_document(args.testcase)
    if isinstance(document, TestcaseSuiteDefinition):
        raise ConfigurationError("run-panel-free currently requires one testcase")
    artifact_dir = _new_artifact_dir(args.artifacts, args.label)
    (artifact_dir / "testcase.yaml").write_text(
        args.testcase.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    print(f"Artifacts: {artifact_dir}", flush=True)
    print(f"AqualinkD: {args.aqualinkd}", flush=True)
    print(f"Testcase: {document.identifier}", flush=True)
    result = asyncio.run(
        run_panel_free_testcase(
            testcase=document,
            aqualinkd=args.aqualinkd,
            web_directory=args.web_directory,
            artifact_dir=artifact_dir,
            duration_seconds=args.duration,
            http_ready_timeout_seconds=args.http_ready_timeout,
            sample_interval_seconds=args.sample_interval,
            terminate_grace_seconds=args.terminate_grace,
        )
    )
    _write_json(
        artifact_dir / "result.json",
        {**asdict(result), "finished_at": datetime.now(UTC).isoformat()},
    )
    print(
        f"Result: {result.status} ({result.reason}), "
        f"child return code {result.child_returncode}",
        flush=True,
    )
    return 0 if result.status == "passed" else 1


def _run(args: argparse.Namespace) -> int:
    target_names: list[str] = list(args.suites)
    selected_names = list(target_names)
    if len(selected_names) != len(set(selected_names)):
        raise ConfigurationError("Each run target may be selected only once")
    targets: list[ResolvedRunTarget] = []
    document_sources: dict[str, Path] = {}
    for selected_name in target_names:
        target = RUN_TARGETS.resolve(selected_name)
        if target.source is not None:
            if target.identifier in document_sources:
                raise ConfigurationError(
                    f"Duplicate declarative target id {target.identifier!r}"
                )
            document_sources[target.identifier] = target.source
        if args.mode != target.mode:
            raise ConfigurationError(
                f"{selected_name} requires --mode {target.mode}, not {args.mode}"
            )
        targets.append(target)

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
    mutating_targets = [target.identifier for target in targets if target.mutates_panel]
    if mutating_targets and not args.allow_equipment_control:
        raise ConfigurationError(
            "Selected targets change equipment and require --panel-read-write: "
            + ", ".join(mutating_targets)
        )
    if args.pda_test_device and not any(
        target.uses_selected_devices for target in targets
    ):
        raise ConfigurationError(
            "--pda-test-device requires a suite containing device-focused validation"
        )
    if args.panel_timezone is None:
        args.panel_timezone = _local_timezone_name()

    if not targets:
        args.run_target = None
        return _run_one(args)

    original_label = args.label
    for target in targets:
        args.run_target = target
        args.label = (
            f"{original_label}-{target.identifier}"
            if len(targets) > 1
            else original_label
        )
        current_exit_code = _run_one(args)
        if current_exit_code != 0:
            return current_exit_code
    return 0


def _run_one(args: argparse.Namespace) -> int:
    target: ResolvedRunTarget | None = getattr(args, "run_target", None)
    if target is not None and target.is_composite:
        return _run_composite_suite(args)
    if target is not None and target.config_overrides:
        return _run_configured_target(args, target)
    return _run_process(args)


def _run_composite_suite(args: argparse.Namespace) -> int:
    composite: ResolvedRunTarget | None = getattr(args, "run_target", None)
    if composite is None or not composite.is_composite:
        raise ConfigurationError("Run target is not a composite suite")
    overall_exit_code = 0
    safe_to_continue = True
    original_label = args.label
    for member_name in composite.members:
        member_args = copy.copy(args)
        member = RUN_TARGETS.resolve(member_name)
        member_args.run_target = member
        suffix = member.artifact_suffix or member_name.removeprefix("pda-live-")
        member_args.label = f"{original_label}-{suffix}"
        print(
            f"\n=== {composite.identifier} member: {member_name} ===",
            flush=True,
        )
        exit_code = _run_one(member_args)
        args.last_artifact_dir = getattr(member_args, "last_artifact_dir", None)
        member_safe = getattr(
            member_args,
            "last_run_safe_to_continue",
            exit_code == 0,
        )
        safe_to_continue = safe_to_continue and member_safe
        if exit_code == 0:
            continue
        overall_exit_code = exit_code
        if not member_safe:
            print(
                f"[ STOP ] {member_name} did not restore a verified safe "
                "state; remaining composite members will not run",
                flush=True,
            )
            args.last_run_safe_to_continue = False
            return exit_code
        print(
            f"[ CONT ] {member_name} failed assertions but restored the "
            "panel; continuing composite validation",
            flush=True,
        )
    args.last_run_safe_to_continue = safe_to_continue
    return overall_exit_code


def _run_configured_target(
    args: argparse.Namespace,
    target: ResolvedRunTarget,
) -> int:
    source_config = args.config.expanduser().resolve(strict=True)
    overrides = target.override_map()
    with tempfile.TemporaryDirectory(prefix="aqualinkd-validator-config-") as directory:
        runtime_dir = Path(directory)
        derived_config = runtime_dir / f"{target.identifier}.conf"
        write_config_with_overrides(
            source_config,
            derived_config,
            overrides,
        )
        process_args = copy.copy(args)
        process_args.config = derived_config
        process_args.source_config = source_config
        process_args.config_overrides = overrides
        exit_code = _run_process(process_args)
        args.last_run_safe_to_continue = getattr(
            process_args,
            "last_run_safe_to_continue",
            False,
        )
        args.last_artifact_dir = getattr(
            process_args,
            "last_artifact_dir",
            None,
        )
        return exit_code


def _run_process(args: argparse.Namespace) -> int:
    args.last_run_safe_to_continue = False
    binary = args.aqualinkd.expanduser().resolve(strict=True)
    config = args.config.expanduser().resolve(strict=True)
    source_config = getattr(args, "source_config", config)
    config_overrides: dict[str, str] = getattr(args, "config_overrides", {})
    site_config = load_site_config(source_config, args.site_config)
    disabled_button_numbers = read_disabled_button_numbers(source_config)
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
    target: ResolvedRunTarget | None = getattr(args, "run_target", None)
    scenario: PdaScenarioRuntime | None = None
    api_base_url: str | None = None
    suite_test_devices: list[str] = []
    if target is not None:
        if target.is_composite:
            raise ConfigurationError(
                f"Composite suite cannot run in one process: {target.identifier}"
            )
        suite_test_devices = (
            args.pda_test_device if target.uses_selected_devices else []
        )
        api_base_url = (
            normalize_api_base_url(args.api_base_url)
            if args.api_base_url is not None
            else None
        )
        scenario = PdaScenarioRuntime(
            None,
            PdaScenarioConfig(
                suite_name=target.identifier,
                execution_phase=target.execution_role,
                activation_timeout_seconds=args.pda_activation_timeout,
                action_timeout_seconds=args.pda_action_timeout,
                status_timeout_seconds=args.pda_status_timeout,
                state_timeout_seconds=args.pda_state_timeout,
                restoration_timeout_seconds=args.pda_cleanup_timeout,
                init_timeout_seconds=args.pda_init_timeout,
                sleep_timeout_seconds=args.pda_sleep_timeout,
                test_devices=tuple(suite_test_devices),
                disabled_button_numbers=disabled_button_numbers,
                panel_timezone=args.panel_timezone,
                panel_time_tolerance_seconds=args.panel_time_tolerance,
                spa_fill_seconds=site_config.spa.fill_seconds,
                case_ids=target.case_ids,
            ),
            api_base_url_override=api_base_url,
            testcase=target.testcase,
            testcases=target.testcases,
        )
    artifact_dir = _new_artifact_dir(args.artifacts, args.label)
    args.last_artifact_dir = artifact_dir
    with (
        (artifact_dir / "summary.log").open(
            "w",
            encoding="utf-8",
        ) as summary_handle,
        contextlib.redirect_stdout(_TeeTextIO(sys.stdout, summary_handle)),
    ):
        return _run_in_artifact(
            args,
            artifact_dir=artifact_dir,
            binary=binary,
            config=config,
            source_config=source_config,
            config_overrides=config_overrides,
            site_config=site_config,
            execution_phase=(target.execution_role if target is not None else "single"),
            disabled_button_numbers=disabled_button_numbers,
            serial_device=serial_device,
            source_tree=source_tree,
            workdir=workdir,
            target=target,
            scenario=scenario,
            api_base_url=api_base_url,
            suite_test_devices=suite_test_devices,
        )


def _run_in_artifact(
    args: argparse.Namespace,
    *,
    artifact_dir: Path,
    binary: Path,
    config: Path,
    source_config: Path,
    config_overrides: dict[str, str],
    site_config: SiteConfig,
    execution_phase: Literal["single", "awake", "sleep"],
    disabled_button_numbers: tuple[int, ...],
    serial_device: Path,
    source_tree: Path | None,
    workdir: Path,
    target: ResolvedRunTarget | None,
    scenario: PdaScenarioRuntime | None,
    api_base_url: str | None,
    suite_test_devices: list[str],
) -> int:
    command = build_aqualinkd_command(
        binary,
        config,
        target.aqualinkd_args if target is not None else (),
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "validator_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "mode": args.mode,
        "suite": _suite_manifest(target, execution_phase),
        "testcase": (
            {
                "id": target.testcase.identifier,
                "description": target.testcase.description,
                "schema": target.testcase.schema,
                "mode": target.testcase.mode,
                "access": target.testcase.access,
                "source": {
                    "name": target.source.name,
                    "sha256": sha256_file(target.source),
                },
            }
            if target is not None
            and target.testcase is not None
            and target.source is not None
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
            "configured_none_buttons": list(disabled_button_numbers),
            "pda_device_selection": (
                ("restricted" if suite_test_devices else "all_discovered_switches")
                if target is not None and target.uses_selected_devices
                else "not_applicable"
            ),
            "panel_time": {
                "timezone": args.panel_timezone,
                "tolerance_seconds": args.panel_time_tolerance,
            },
            "timeouts_seconds": {
                "activation": args.pda_activation_timeout,
                "action": args.pda_action_timeout,
                "status": args.pda_status_timeout,
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
            "name": source_config.name,
            "sha256": sha256_file(source_config),
            "effective_sha256": sha256_file(config),
            "overrides": config_overrides,
        },
        "site_config": (
            {
                "name": site_config.source.name,
                "sha256": sha256_file(site_config.source),
                "spa": {"fill_seconds": site_config.spa.fill_seconds},
            }
            if site_config.source is not None
            else None
        ),
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
    if site_config.source is not None:
        print(f"Site config: {site_config.source}", flush=True)
    if config_overrides:
        formatted_overrides = ", ".join(
            f"{key}={value}" for key, value in config_overrides.items()
        )
        print(f"Config overrides: {formatted_overrides}", flush=True)
    print(f"Serial device: {serial_device}", flush=True)
    print(f"Mode: {args.mode}", flush=True)
    if target is not None and target.is_testcase:
        print(f"Testcase: {target.identifier}", flush=True)
    elif target is not None and target.is_declarative_suite:
        print(
            f"Suite: {target.identifier} "
            f"({len(target.testcases)} declarative testcases)",
            flush=True,
        )
    else:
        print(
            f"Suite: {target.identifier if target is not None else 'none'}",
            flush=True,
        )

    try:
        result = asyncio.run(
            LocalProcessRunner().run(
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
        scenario_data = json.loads(scenario_path.read_text(encoding="utf-8"))
        args.last_run_safe_to_continue = bool(
            scenario_data.get("safe_to_continue", False)
        )
        performance["scenario"] = scenario_data
        scenario_aqualinkd = scenario_data.get("aqualinkd")
        if isinstance(scenario_aqualinkd, dict):
            manifest["aqualinkd"]["reported_version"] = scenario_aqualinkd.get(
                "version"
            )
            manifest["aqualinkd"]["configured_panel_type"] = scenario_aqualinkd.get(
                "configured_panel_type"
            )
        manifest["equipment_control"]["api_base_url"] = scenario_data.get(
            "api_base_url"
        )
        manifest["equipment_control"]["api_endpoint_source"] = scenario_data.get(
            "api_endpoint_source"
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


def _suite_manifest(
    target: ResolvedRunTarget | None,
    execution_phase: Literal["single", "awake", "sleep"],
) -> dict[str, Any] | None:
    if target is None or target.is_testcase:
        return None
    manifest: dict[str, Any] = {
        "name": target.identifier,
        "description": target.description,
        "mode": "physical-panel" if target.source is not None else target.mode,
        "aqualinkd_args": list(target.aqualinkd_args),
        "execution_phase": execution_phase,
    }
    if target.case_ids:
        manifest["cases"] = [case_id.value for case_id in target.case_ids]
    if target.testcases:
        manifest["testcases"] = [testcase.identifier for testcase in target.testcases]
    if target.source is not None:
        manifest["source"] = {
            "name": target.source.name,
            "sha256": sha256_file(target.source),
        }
    return manifest


def build_aqualinkd_command(
    binary: Path,
    config: Path,
    aqualinkd_args: Sequence[str] = (),
) -> list[str]:
    command = [str(binary), "-d", "-c", str(config)]
    command.extend(aqualinkd_args)
    return command


class _TeeTextIO(io.TextIOBase):
    def __init__(self, primary: TextIO, summary: TextIO) -> None:
        self._primary = primary
        self._summary = summary

    def write(self, text: str) -> int:
        self._primary.write(text)
        self._summary.write(text)
        return len(text)

    def flush(self) -> None:
        self._primary.flush()
        self._summary.flush()

    def isatty(self) -> bool:
        return self._primary.isatty()


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


def _run_target(value: str) -> str:
    if value in _RUN_SUITE_NAMES or _is_testcase_target(value):
        return value
    choices = ", ".join(_RUN_SUITE_NAMES)
    raise argparse.ArgumentTypeError(
        f"unknown run target {value!r}; choose a suite ({choices}) or a .yaml file"
    )


def _is_testcase_target(value: str) -> bool:
    return Path(value).suffix.casefold() in {".yaml", ".yml"}
