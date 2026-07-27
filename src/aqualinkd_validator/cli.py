from __future__ import annotations

import argparse
import asyncio
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

from . import __version__
from .comparison import format_comparison, load_comparison
from .config import (
    ConfigurationError,
    sha256_file,
    validate_live_serial_device,
)
from .metadata import (
    collect_binary_metadata,
    collect_host_metadata,
    collect_source_metadata,
)
from .metrics import summarize_metrics
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
        "run", help="Supervise AqualinkD and collect logs and process metrics"
    )
    run.add_argument("--aqualinkd", type=Path, required=True)
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--artifacts", type=Path, default=Path("artifacts"))
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
        choices=("live-panel", "jandy-simulator"),
        default="live-panel",
    )
    run.add_argument("--serial-device", type=Path, required=True)
    run.add_argument(
        "--allow-live-panel",
        action="store_true",
        help="Confirm intentional access to the configured live serial bus",
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
    if not args.allow_live_panel:
        raise ConfigurationError(
            f"{args.mode} mode requires --allow-live-panel because it opens a "
            "real or externally managed serial bus"
        )

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
    artifact_dir = _new_artifact_dir(args.artifacts, args.label)
    command = [str(binary), "-d", "-c", str(config)]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "validator_version": __version__,
        "created_at": datetime.now(UTC).isoformat(),
        "label": args.label,
        "mode": args.mode,
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
            "explicitly_authorized": True,
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

    try:
        result = asyncio.run(
            supervise(
                command,
                artifact_dir,
                cwd=workdir,
                duration_seconds=args.duration,
                sample_interval_seconds=args.sample_interval,
                terminate_grace_seconds=args.terminate_grace,
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
    _write_json(artifact_dir / "performance.json", performance)
    _write_json(artifact_dir / "result.json", result_data)
    print(
        f"Result: {result.status} ({result.reason}), "
        f"child return code {result.child_returncode}",
        flush=True,
    )
    return 0 if result.status == "passed" else 1


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


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed
