from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any


def load_comparison(artifact_dirs: list[Path]) -> dict[str, Any]:
    if len(artifact_dirs) < 2:
        raise ValueError("compare requires at least two artifact directories")

    runs = [_load_run(path.expanduser().resolve(strict=True)) for path in artifact_dirs]
    warnings = _comparability_warnings(runs)
    return {
        "schema_version": 1,
        "warnings": warnings,
        "runs": runs,
    }


def format_comparison(comparison: dict[str, Any]) -> str:
    headings = [
        "label",
        "commit",
        "samples",
        "CPU %",
        "CPU sec",
        "max RSS MiB",
        "avg threads",
        "ctx switches",
    ]
    rows: list[list[str]] = []
    for run in comparison["runs"]:
        process = run["performance"]["process"]
        cpu = process.get("cpu", {})
        rss = process.get("rss_bytes", {})
        threads = process.get("threads", {})
        switches = process.get("context_switches", {})
        utilization = cpu.get("utilization_percent")
        commit = (run.get("commit") or "unknown")[:12]
        rows.append(
            [
                run["label"],
                commit,
                str(process.get("sample_count", 0)),
                _format_number(utilization, 2),
                _format_number(cpu.get("total_seconds"), 3),
                _format_number(
                    rss.get("maximum", 0) / (1024 * 1024)
                    if rss.get("maximum") is not None
                    else None,
                    2,
                ),
                _format_number(threads.get("average"), 2),
                str(
                    switches.get("voluntary", 0)
                    + switches.get("nonvoluntary", 0)
                ),
            ]
        )

    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]
    lines = [
        "  ".join(
            heading.ljust(widths[index])
            for index, heading in enumerate(headings)
        ),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
    if comparison["warnings"]:
        lines.append("")
        lines.append("Comparability warnings:")
        lines.extend(f"- {warning}" for warning in comparison["warnings"])
    return "\n".join(lines)


def _load_run(path: Path) -> dict[str, Any]:
    manifest = _read_json(path / "manifest.yaml")
    performance = _read_json(path / "performance.json")
    result = _read_json(path / "result.json")
    source = manifest.get("source") or {}
    return {
        "artifact_dir": str(path),
        "label": manifest.get("label", path.name),
        "commit": source.get("commit"),
        "manifest": manifest,
        "performance": performance,
        "result": result,
    }


def _comparability_warnings(runs: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    fields: dict[str, Callable[[dict[str, Any]], Any]] = {
        "architecture": lambda run: run["manifest"]["host"].get("architecture"),
        "CPU model": lambda run: run["manifest"]["host"].get("cpu_model"),
        "kernel": lambda run: run["manifest"]["host"].get("kernel"),
        "config fingerprint": lambda run: run["manifest"]["config"].get("sha256"),
        "sample interval": lambda run: run["manifest"]["sampling"].get(
            "interval_seconds"
        ),
        "container runtime": lambda run: run["manifest"]["host"].get("container"),
    }
    for name, getter in fields.items():
        values = {json.dumps(getter(run), sort_keys=True) for run in runs}
        if len(values) > 1:
            warnings.append(f"{name} differs between runs")
    statuses = {run["result"].get("status") for run in runs}
    if statuses != {"passed"}:
        warnings.append("one or more runs did not pass")
    return warnings


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"missing artifact: {path}") from error
    if not isinstance(value, dict):
        raise ValueError(f"artifact must contain a JSON object: {path}")
    return value


def _format_number(value: float | int | None, places: int) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"
