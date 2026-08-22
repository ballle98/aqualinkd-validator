from __future__ import annotations

import time
from typing import Any

from ..interfaces import ArtifactStore


class ScenarioRecorder:
    """Own scenario artifact serialization, measurements, and console progress."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report

    @staticmethod
    def progress_started(name: str) -> float:
        print(f"[ RUN  ] {name}", flush=True)
        return time.monotonic()

    @staticmethod
    def progress_finished(
        name: str,
        started: float,
        *,
        passed: bool,
        detail: str | None = None,
    ) -> None:
        duration = time.monotonic() - started
        state = "PASS" if passed else "FAIL"
        suffix = f" — {detail}" if detail else ""
        print(
            f"[{state:^6}] {name} completed in {duration:.3f}s{suffix}",
            flush=True,
        )

    @classmethod
    def format_exception(cls, error: BaseException) -> str:
        if isinstance(error, BaseExceptionGroup):
            details = [cls.format_exception(nested) for nested in error.exceptions]
            if len(details) == 1:
                return details[0]
            return "; ".join(details)
        return f"{type(error).__name__}: {error}"

    def write(self, artifacts: ArtifactStore) -> None:
        artifacts.write_json("scenario.json", self.report)

    def skip(self, name: str, reason: str) -> None:
        self.report["skipped"].append({"name": name, "reason": reason})
        print(f"[ SKIP ] {name} — {reason}", flush=True)

    def append_measurement(
        self,
        *,
        name: str,
        category: str,
        phase: str,
        target: str,
        requested_value: Any,
        start_offset_ns: int,
        api_ack_offset_ns: int | None,
        log_completion_offset_ns: int | None,
        state_observed_offset_ns: int | None,
        task_active_offset_ns: int | None = None,
        status: str = "passed",
    ) -> None:
        completion_offsets = [
            offset
            for offset in (
                task_active_offset_ns,
                log_completion_offset_ns,
                state_observed_offset_ns,
                api_ack_offset_ns,
            )
            if offset is not None
        ]
        completed = max(completion_offsets, default=start_offset_ns)
        measurement = {
            "name": name,
            "category": category,
            "status": status,
            "phase": phase,
            "target": target,
            "requested_value": requested_value,
            "start_offset_ns": start_offset_ns,
            "api_ack_offset_ns": api_ack_offset_ns,
            "task_active_offset_ns": task_active_offset_ns,
            "log_completion_offset_ns": log_completion_offset_ns,
            "state_observed_offset_ns": state_observed_offset_ns,
            "completed_offset_ns": completed,
            "duration_ms": round((completed - start_offset_ns) / 1_000_000, 3),
            "api_ack_ms": (
                round((api_ack_offset_ns - start_offset_ns) / 1_000_000, 3)
                if api_ack_offset_ns is not None
                else None
            ),
            "activation_ms": (
                round((task_active_offset_ns - start_offset_ns) / 1_000_000, 3)
                if task_active_offset_ns is not None
                else None
            ),
            "programmer_duration_ms": (
                round(
                    (log_completion_offset_ns - task_active_offset_ns) / 1_000_000,
                    3,
                )
                if (
                    task_active_offset_ns is not None
                    and log_completion_offset_ns is not None
                )
                else None
            ),
            "state_convergence_ms": (
                round(
                    (state_observed_offset_ns - log_completion_offset_ns) / 1_000_000,
                    3,
                )
                if (
                    log_completion_offset_ns is not None
                    and state_observed_offset_ns is not None
                )
                else None
            ),
        }
        self.report["measurements"].append(measurement)
