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
                str(switches.get("voluntary", 0) + switches.get("nonvoluntary", 0)),
            ]
        )

    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]
    lines = [
        "  ".join(
            heading.ljust(widths[index]) for index, heading in enumerate(headings)
        ),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in rows
    )
    scenario_table = _format_scenario_timings(comparison["runs"])
    if scenario_table:
        lines.extend(["", "Scenario timings (ms):", *scenario_table])
    if comparison["warnings"]:
        lines.append("")
        lines.append("Comparability warnings:")
        lines.extend(f"- {warning}" for warning in comparison["warnings"])
    return "\n".join(lines)


def _format_scenario_timings(runs: list[dict[str, Any]]) -> list[str]:
    measurements = [_scenario_measurements(run) for run in runs]
    names = sorted({name for values in measurements for name in values})
    if not names:
        return []
    headings = ["measurement", *(run["label"] for run in runs)]
    rows = [
        [
            name,
            *[_format_number(values.get(name), 3) for values in measurements],
        ]
        for name in names
    ]
    widths = [
        max(len(headings[index]), *(len(row[index]) for row in rows))
        for index in range(len(headings))
    ]
    return [
        "  ".join(
            heading.ljust(widths[index]) for index, heading in enumerate(headings)
        ),
        "  ".join("-" * width for width in widths),
        *[
            "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
            for row in rows
        ],
    ]


def _scenario_measurements(run: dict[str, Any]) -> dict[str, float]:
    scenario = run["performance"].get("scenario")
    if not isinstance(scenario, dict):
        return {}
    values: dict[str, float] = {}
    measurements = scenario.get("measurements", [])
    if not isinstance(measurements, list):
        return values
    for measurement in measurements:
        if not isinstance(measurement, dict):
            continue
        name = measurement.get("name")
        if not isinstance(name, str):
            continue
        timing_fields = (
            ("activation_ms", "activation"),
            ("programmer_duration_ms", "programmer"),
            ("state_convergence_ms", "state"),
            ("duration_ms", "end_to_end"),
        )
        for field, suffix in timing_fields:
            duration = measurement.get(field)
            if isinstance(duration, (int, float)):
                values[f"{name}.{suffix}"] = float(duration)
    return values


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
        "suite": lambda run: (run["manifest"].get("suite") or {}).get("name"),
        "PDA execution role": lambda run: (run["manifest"].get("suite") or {}).get(
            "execution_phase"
        ),
        "PDA device restriction": lambda run: (
            run["manifest"].get("equipment_control", {}).get("pda_test_devices")
        ),
        "PDA resolved switches": lambda run: (
            (run["performance"].get("scenario") or {})
            .get("device_selection", {})
            .get("resolved")
        ),
        "PDA timeouts": lambda run: (
            run["manifest"].get("equipment_control", {}).get("timeouts_seconds")
        ),
        "panel time configuration": lambda run: (
            run["manifest"].get("equipment_control", {}).get("panel_time")
        ),
        "PDA panel type": _pda_panel_type,
        "PDA firmware": _pda_firmware,
        "AqualinkD reported version": lambda run: (
            (run["performance"].get("scenario") or {})
            .get("aqualinkd", {})
            .get("version")
        ),
    }
    for name, getter in fields.items():
        values = {json.dumps(getter(run), sort_keys=True) for run in runs}
        if len(values) > 1:
            warnings.append(f"{name} differs between runs")
    statuses = {run["result"].get("status") for run in runs}
    if statuses != {"passed"}:
        warnings.append("one or more runs did not pass")
    return warnings


def _pda_panel_type(run: dict[str, Any]) -> Any:
    init_screen = _pda_init_screen(run)
    return init_screen.get("panel_type") if init_screen is not None else None


def _pda_firmware(run: dict[str, Any]) -> Any:
    init_screen = _pda_init_screen(run)
    return init_screen.get("firmware") if init_screen is not None else None


def _pda_init_screen(run: dict[str, Any]) -> dict[str, Any] | None:
    scenario = run["performance"].get("scenario")
    if not isinstance(scenario, dict):
        return None
    panel = scenario.get("panel")
    if not isinstance(panel, dict):
        return None
    init_screen = panel.get("init_screen")
    return init_screen if isinstance(init_screen, dict) else None


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
