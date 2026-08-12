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
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__
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
from .pda.cases import PdaCaseId
from .pda_scenario import PdaLivePanelScenario, PdaScenarioConfig
from .suites import SUITES, SuiteProfile, get_suite
from .supervisor import supervise
from .testcases import (
    TestcaseDefinition,
    TestcaseSuiteDefinition,
    load_testcase_document,
)

DEVICE_SELECTION_CASES = frozenset(
    {
        PdaCaseId.CONSECUTIVE_DEVICES,
        PdaCaseId.DEVICE_DURING_STATUS_RETRY,
        PdaCaseId.DEVICE_AFTER_PROBE,
    }
)
_BUILTIN_DECLARATIVE_SUITES = {
    "pda-live-fast": (
        Path(__file__).parents[2] / "testcases" / "suites" / "pda-live-fast.yaml"
    ),
    "pda-live-awake": (
        Path(__file__).parents[2] / "testcases" / "suites" / "pda-live-awake.yaml"
    ),
    "pda-live-sleep": (
        Path(__file__).parents[2] / "testcases" / "suites" / "pda-live-sleep.yaml"
    ),
}
_RUN_SUITE_NAMES = tuple((*_BUILTIN_DECLARATIVE_SUITES, *SUITES))


@dataclass(frozen=True)
class _RunTarget:
    legacy_suite_name: str | None = None
    testcase: TestcaseDefinition | None = None
    testcase_suite: TestcaseSuiteDefinition | None = None
    source: Path | None = None

    @property
    def identifier(self) -> str | None:
        if self.legacy_suite_name is not None:
            return self.legacy_suite_name
        if self.testcase is not None:
            return self.testcase.identifier
        if self.testcase_suite is not None:
            return self.testcase_suite.identifier
        return None


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
        choices=_RUN_SUITE_NAMES,
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


def _run(args: argparse.Namespace) -> int:
    positional_suites = list(args.suites)
    legacy_suites = list(args.legacy_suites or ())
    if positional_suites and legacy_suites:
        raise ConfigurationError(
            "Specify suites positionally; do not combine them with --suite"
        )
    target_names: list[str | None] = positional_suites or legacy_suites or [None]
    selected_names = [name for name in target_names if name is not None]
    if len(selected_names) != len(set(selected_names)):
        raise ConfigurationError("Each run target may be selected only once")
    targets: list[_RunTarget] = []
    document_sources: dict[str, Path] = {}
    for selected_name in target_names:
        document_path = (
            Path(selected_name).expanduser().resolve()
            if selected_name is not None and _is_testcase_target(selected_name)
            else (
                _BUILTIN_DECLARATIVE_SUITES.get(selected_name)
                if selected_name is not None
                else None
            )
        )
        document = load_testcase_document(document_path) if document_path else None
        testcase = document if isinstance(document, TestcaseDefinition) else None
        testcase_suite = (
            document if isinstance(document, TestcaseSuiteDefinition) else None
        )
        document_id = (
            testcase.identifier
            if testcase is not None
            else testcase_suite.identifier
            if testcase_suite is not None
            else None
        )
        if document_id is not None and document_path is not None:
            if document_id in document_sources:
                raise ConfigurationError(
                    f"Duplicate declarative target id {document_id!r}"
                )
            document_sources[document_id] = document_path
        suite = get_suite(selected_name) if document is None else None
        declarative_mode = (
            testcase.mode
            if testcase is not None
            else testcase_suite.mode
            if testcase_suite is not None
            else None
        )
        if declarative_mode is not None and declarative_mode != "physical-panel":
            raise ConfigurationError(
                f"{selected_name}: execution for testcase mode "
                f"{declarative_mode} is not implemented"
            )
        required_mode = (
            suite.mode
            if suite is not None
            else "live-panel"
            if declarative_mode == "physical-panel"
            else args.mode
        )
        if args.mode != required_mode:
            raise ConfigurationError(
                f"{selected_name} requires --mode {required_mode}, not {args.mode}"
            )
        targets.append(
            _RunTarget(
                legacy_suite_name=selected_name if suite is not None else None,
                testcase=testcase,
                testcase_suite=testcase_suite,
                source=document_path,
            )
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
    mutating_suites = [
        target.legacy_suite_name
        for target in targets
        if target.legacy_suite_name is not None
        and _suite_mutates_panel(target.legacy_suite_name)
    ]
    mutating_declarative = [
        identifier
        for target in targets
        for identifier in (target.identifier,)
        if (
            (target.testcase is not None and target.testcase.access == "read-write")
            or (
                target.testcase_suite is not None
                and target.testcase_suite.access == "read-write"
            )
        )
        and identifier is not None
    ]
    mutating_targets = [*mutating_suites, *mutating_declarative]
    if mutating_targets and not args.allow_equipment_control:
        raise ConfigurationError(
            "Selected targets change equipment and require --panel-read-write: "
            + ", ".join(mutating_targets)
        )
    if args.pda_test_device and not any(
        (
            target.testcase_suite is not None
            and target.testcase_suite.uses_selected_devices
        )
        or (
            target.legacy_suite_name is not None
            and any(
                _suite_contains_case(target.legacy_suite_name, case_id)
                for case_id in DEVICE_SELECTION_CASES
            )
        )
        for target in targets
    ):
        raise ConfigurationError(
            "--pda-test-device requires a suite containing device-focused validation"
        )
    if args.panel_timezone is None:
        args.panel_timezone = _local_timezone_name()

    original_label = args.label
    for target in targets:
        args.suite = target.legacy_suite_name
        args.testcase = target.testcase
        args.testcase_suite = target.testcase_suite
        args.testcase_path = target.source if target.testcase is not None else None
        args.testcase_suite_path = (
            target.source if target.testcase_suite is not None else None
        )
        target_label = target.identifier
        args.label = (
            f"{original_label}-{target_label}"
            if len(targets) > 1 and target_label is not None
            else original_label
        )
        current_exit_code = _run_one(args)
        if current_exit_code != 0:
            return current_exit_code
    return 0


def _run_one(args: argparse.Namespace) -> int:
    testcase_suite: TestcaseSuiteDefinition | None = getattr(
        args, "testcase_suite", None
    )
    if testcase_suite is not None:
        return _run_declarative_suite(args, testcase_suite)
    suite = get_suite(args.suite)
    if suite is not None and suite.is_composite:
        return _run_composite_suite(args)
    if suite is not None and suite.config_overrides:
        return _run_process_suite(args)
    return _run_process(args)


def _run_declarative_suite(
    args: argparse.Namespace,
    suite: TestcaseSuiteDefinition,
) -> int:
    source_config = args.config.expanduser().resolve(strict=True)
    overrides = suite.config.override_map()
    if not overrides:
        return _run_process(args)
    with tempfile.TemporaryDirectory(prefix="aqualinkd-validator-config-") as directory:
        derived_config = Path(directory) / f"{suite.identifier}.conf"
        write_config_with_overrides(source_config, derived_config, overrides)
        process_args = copy.copy(args)
        process_args.config = derived_config
        process_args.source_config = source_config
        process_args.config_overrides = overrides
        exit_code = _run_process(process_args)
        args.last_run_safe_to_continue = getattr(
            process_args, "last_run_safe_to_continue", False
        )
        args.last_artifact_dir = getattr(process_args, "last_artifact_dir", None)
        return exit_code


def _run_composite_suite(args: argparse.Namespace) -> int:
    composite = get_suite(args.suite)
    if composite is None or not composite.is_composite:
        raise ConfigurationError(f"Suite is not composite: {args.suite}")
    overall_exit_code = 0
    safe_to_continue = True
    original_label = args.label
    for member_name in composite.members:
        member_args = copy.copy(args)
        declarative_path = _BUILTIN_DECLARATIVE_SUITES.get(member_name)
        if declarative_path is not None:
            member_document = load_testcase_document(declarative_path)
            if not isinstance(member_document, TestcaseSuiteDefinition):
                raise ConfigurationError(
                    f"Composite member is not a suite: {member_name}"
                )
            member_args.suite = None
            member_args.testcase = None
            member_args.testcase_suite = member_document
            member_args.testcase_suite_path = declarative_path
            suffix = member_name.removeprefix("pda-live-")
        else:
            member = get_suite(member_name)
            if member is None:
                raise ConfigurationError(
                    f"Unknown composite member: {member_name}"
                )
            member_args.suite = member.name
            member_args.testcase = None
            member_args.testcase_suite = None
            suffix = member.artifact_suffix or member.name
        member_args.label = f"{original_label}-{suffix}"
        print(
            f"\n=== {composite.name} member: {member_name} ===",
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


def _run_process_suite(args: argparse.Namespace) -> int:
    suite = get_suite(args.suite)
    assert suite is not None and not suite.is_composite
    source_config = args.config.expanduser().resolve(strict=True)
    overrides = suite.override_map()
    with tempfile.TemporaryDirectory(prefix="aqualinkd-validator-config-") as directory:
        runtime_dir = Path(directory)
        derived_config = runtime_dir / f"{suite.name}.conf"
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


def _suite_contains_case(name: str, case_id: PdaCaseId) -> bool:
    declarative_path = _BUILTIN_DECLARATIVE_SUITES.get(name)
    if declarative_path is not None:
        document = load_testcase_document(declarative_path)
        assert isinstance(document, TestcaseSuiteDefinition)
        return case_id in DEVICE_SELECTION_CASES and document.uses_selected_devices
    suite = get_suite(name)
    assert suite is not None
    if case_id in suite.cases:
        return True
    return any(_suite_contains_case(member, case_id) for member in suite.members)


def _suite_mutates_panel(name: str) -> bool:
    declarative_path = _BUILTIN_DECLARATIVE_SUITES.get(name)
    if declarative_path is not None:
        document = load_testcase_document(declarative_path)
        assert isinstance(document, TestcaseSuiteDefinition)
        return document.mutates_panel
    suite = get_suite(name)
    assert suite is not None
    return suite.mutates_panel or any(
        _suite_mutates_panel(member) for member in suite.members
    )


def _run_process(args: argparse.Namespace) -> int:
    args.last_run_safe_to_continue = False
    binary = args.aqualinkd.expanduser().resolve(strict=True)
    config = args.config.expanduser().resolve(strict=True)
    source_config = getattr(args, "source_config", config)
    config_overrides: dict[str, str] = getattr(args, "config_overrides", {})
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
    suite = get_suite(args.suite)
    testcase: TestcaseDefinition | None = getattr(args, "testcase", None)
    testcase_suite: TestcaseSuiteDefinition | None = getattr(
        args, "testcase_suite", None
    )
    if suite is not None and args.mode != suite.mode:
        raise ConfigurationError(
            f"{suite.name} requires --mode {suite.mode}, not {args.mode}"
        )
    scenario: PdaLivePanelScenario | None = None
    api_base_url: str | None = None
    suite_test_devices: list[str] = []
    if suite is not None or testcase is not None or testcase_suite is not None:
        if suite is not None and suite.is_composite:
            raise ConfigurationError(
                f"Composite suite cannot run in one process: {suite.name}"
            )
        if suite is not None:
            target_name = suite.name
        elif testcase_suite is not None:
            target_name = testcase_suite.identifier
        else:
            assert testcase is not None
            target_name = testcase.identifier
        suite_test_devices = (
            args.pda_test_device
            if (
                suite is not None
                and any(case_id in suite.cases for case_id in DEVICE_SELECTION_CASES)
            )
            or (testcase_suite is not None and testcase_suite.uses_selected_devices)
            else []
        )
        api_base_url = (
            normalize_api_base_url(args.api_base_url)
            if args.api_base_url is not None
            else None
        )
        scenario = PdaLivePanelScenario(
            None,
            PdaScenarioConfig(
                suite_name=target_name,
                execution_phase=(
                    suite.execution_role
                    if suite is not None
                    else testcase_suite.config.execution_role
                    if testcase_suite is not None
                    else "single"
                ),
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
                case_ids=suite.cases if suite is not None else (),
            ),
            api_base_url_override=api_base_url,
            testcase=testcase,
            testcases=(
                tuple(member.testcase for member in testcase_suite.members)
                if testcase_suite is not None
                else ()
            ),
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
            execution_phase=(
                suite.execution_role
                if suite is not None
                else testcase_suite.config.execution_role
                if testcase_suite is not None
                else "single"
            ),
            disabled_button_numbers=disabled_button_numbers,
            serial_device=serial_device,
            source_tree=source_tree,
            workdir=workdir,
            suite=suite,
            testcase=testcase,
            testcase_suite=testcase_suite,
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
    execution_phase: Literal["single", "awake", "sleep"],
    disabled_button_numbers: tuple[int, ...],
    serial_device: Path,
    source_tree: Path | None,
    workdir: Path,
    suite: SuiteProfile | None,
    testcase: TestcaseDefinition | None,
    testcase_suite: TestcaseSuiteDefinition | None,
    scenario: PdaLivePanelScenario | None,
    api_base_url: str | None,
    suite_test_devices: list[str],
) -> int:
    command = build_aqualinkd_command(
        binary,
        config,
        args.suite,
        pda_testcase=testcase is not None or testcase_suite is not None,
    )
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
                "cases": [case_id.value for case_id in suite.cases],
                "execution_phase": execution_phase,
            }
            if suite is not None
            else {
                "name": testcase_suite.identifier,
                "description": testcase_suite.description,
                "mode": testcase_suite.mode,
                "aqualinkd_args": list(testcase_suite.config.aqualinkd_args),
                "testcases": [
                    member.testcase.identifier for member in testcase_suite.members
                ],
                "execution_phase": testcase_suite.config.execution_role,
                "source": {
                    "name": args.testcase_suite_path.name,
                    "sha256": sha256_file(args.testcase_suite_path),
                },
            }
            if testcase_suite is not None
            else None
        ),
        "testcase": (
            {
                "id": testcase.identifier,
                "description": testcase.description,
                "schema": testcase.schema,
                "mode": testcase.mode,
                "access": testcase.access,
                "source": {
                    "name": args.testcase_path.name,
                    "sha256": sha256_file(args.testcase_path),
                },
            }
            if testcase is not None
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
                "not_applicable"
                if (suite is None or PdaCaseId.CONSECUTIVE_DEVICES not in suite.cases)
                else ("restricted" if suite_test_devices else "all_discovered_switches")
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
    if config_overrides:
        formatted_overrides = ", ".join(
            f"{key}={value}" for key, value in config_overrides.items()
        )
        print(f"Config overrides: {formatted_overrides}", flush=True)
    print(f"Serial device: {serial_device}", flush=True)
    print(f"Mode: {args.mode}", flush=True)
    if testcase is not None:
        print(f"Testcase: {testcase.identifier}", flush=True)
    elif testcase_suite is not None:
        print(
            f"Suite: {testcase_suite.identifier} "
            f"({len(testcase_suite.members)} declarative testcases)",
            flush=True,
        )
    else:
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


def build_aqualinkd_command(
    binary: Path,
    config: Path,
    suite_name: str | None,
    *,
    pda_testcase: bool = False,
) -> list[str]:
    command = [str(binary), "-d", "-c", str(config)]
    suite = get_suite(suite_name)
    if suite is not None:
        command.extend(suite.aqualinkd_args)
    elif pda_testcase:
        command.append("-vv")
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


def _suite_name(value: str) -> str:
    if value not in _RUN_SUITE_NAMES:
        choices = ", ".join(_RUN_SUITE_NAMES)
        raise argparse.ArgumentTypeError(
            f"unknown suite {value!r}; choose from: {choices}"
        )
    return value


def _run_target(value: str) -> str:
    if value in _RUN_SUITE_NAMES or _is_testcase_target(value):
        return value
    choices = ", ".join(_RUN_SUITE_NAMES)
    raise argparse.ArgumentTypeError(
        f"unknown run target {value!r}; choose a suite ({choices}) or a .yaml file"
    )


def _is_testcase_target(value: str) -> bool:
    return Path(value).suffix.casefold() in {".yaml", ".yml"}
