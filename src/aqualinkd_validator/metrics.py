from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import fmean
from typing import Any


def summarize_metrics(path: Path) -> dict[str, Any]:
    samples = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if not samples:
        return {"sample_count": 0}

    first = samples[0]
    last = samples[-1]
    elapsed_seconds = max(0.0, (last["offset_ns"] - first["offset_ns"]) / 1_000_000_000)
    clock_ticks = os.sysconf("SC_CLK_TCK")
    user_seconds = _delta(first, last, "cpu_user_ticks") / clock_ticks
    system_seconds = _delta(first, last, "cpu_system_ticks") / clock_ticks
    cpu_seconds = user_seconds + system_seconds
    return {
        "sample_count": len(samples),
        "sample_window_seconds": elapsed_seconds,
        "cpu": {
            "user_seconds": user_seconds,
            "system_seconds": system_seconds,
            "total_seconds": cpu_seconds,
            "utilization_percent": (
                cpu_seconds / elapsed_seconds * 100 if elapsed_seconds > 0 else None
            ),
        },
        "rss_bytes": {
            "average": round(fmean(sample["rss_bytes"] for sample in samples)),
            "maximum": max(sample["rss_bytes"] for sample in samples),
            "minimum": min(sample["rss_bytes"] for sample in samples),
        },
        "threads": {
            "average": fmean(sample["threads"] for sample in samples),
            "maximum": max(sample["threads"] for sample in samples),
        },
        "context_switches": {
            "voluntary": _delta(first, last, "voluntary_context_switches"),
            "nonvoluntary": _delta(first, last, "nonvoluntary_context_switches"),
        },
        "io_bytes": {
            "read": _delta(first, last, "read_bytes"),
            "write": _delta(first, last, "write_bytes"),
        },
    }


def _delta(first: dict[str, int], last: dict[str, int], key: str) -> int:
    return max(0, last[key] - first[key])
