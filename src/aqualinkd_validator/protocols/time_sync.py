from __future__ import annotations

import asyncio
import os
import statistics
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from ..interfaces import AqualinkApi, ScenarioContext
from .pda.programmer import PdaProgrammerObserver


class PanelTimeSyncFailure(RuntimeError):
    """Raised when panel time synchronization cannot be measured or restored."""


def find_faketime_library() -> Path:
    configured = os.environ.get("LIBFAKETIME_PATH")
    candidates = (
        [Path(configured)]
        if configured
        else [
            path
            for root in (Path("/usr/lib"), Path("/usr/lib64"))
            for pattern in (
                "faketime/libfaketime.so.1",
                "*/faketime/libfaketime.so.1",
            )
            for path in root.glob(pattern)
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        "time validation requires libfaketime; install the libfaketime package "
        "or set LIBFAKETIME_PATH"
    )


class ProcessTimeController:
    """Change one child process's wall clock without changing the host clock."""

    def __init__(self, offset_seconds: int, library: Path) -> None:
        handle, name = tempfile.mkstemp(prefix="aqualinkd-faketime-", text=True)
        os.close(handle)
        self._path = Path(name)
        self._library = library
        self._offset_seconds = offset_seconds
        self._panel_requires_restore = offset_seconds != 0
        self.set_offset(offset_seconds)

    @property
    def offset_seconds(self) -> int:
        return self._offset_seconds

    @property
    def environment(self) -> dict[str, str]:
        return {
            "LD_PRELOAD": str(self._library),
            "FAKETIME_TIMESTAMP_FILE": str(self._path),
            "FAKETIME_NO_CACHE": "1",
            "FAKETIME_DONT_FAKE_MONOTONIC": "1",
        }

    @property
    def panel_requires_restore(self) -> bool:
        return self._panel_requires_restore

    def mark_panel_restored(self) -> None:
        self._panel_requires_restore = False

    def set_offset(self, seconds: int) -> None:
        replacement = self._path.with_suffix(".next")
        sign = "+" if seconds >= 0 else "-"
        replacement.write_text(f"{sign}{abs(seconds)}s\n", encoding="ascii")
        replacement.replace(self._path)
        self._offset_seconds = seconds

    def close(self) -> None:
        self._path.unlink(missing_ok=True)
        self._path.with_suffix(".next").unlink(missing_ok=True)


@dataclass(frozen=True)
class PanelTimeSyncConfig:
    protocol: str
    task_name: str
    active_marker: str
    finished_marker: str
    artifact_name: str
    timezone: str
    observation_seconds: float
    poll_seconds: float
    convergence_tolerance_seconds: float
    activation_timeout_seconds: float
    action_timeout_seconds: float


class PanelTimeSyncValidator:
    """Measure panel minute edges and exercise the force-time HTTP endpoint."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        controller: ProcessTimeController,
        programmer: PdaProgrammerObserver,
        config: PanelTimeSyncConfig,
    ) -> None:
        self._api = api
        self._controller = controller
        self._programmer = programmer
        self._config = config

    async def validate(self, context: ScenarioContext) -> dict[str, Any]:
        timezone = ZoneInfo(self._config.timezone)
        report: dict[str, Any] = {
            "schema_version": 1,
            "initial_offset_seconds": self._controller.offset_seconds,
            "observation_seconds": self._config.observation_seconds,
            "poll_seconds": self._config.poll_seconds,
            "transitions": [],
            "http_force_sync": None,
        }
        context.artifacts.write_json(self._config.artifact_name, report)
        await self._wait_for_time(
            offset_seconds=self._controller.offset_seconds,
        )
        print(
            f"[ WAIT ] Observing {self._config.protocol.upper()} minute "
            "transitions for "
            f"{self._config.observation_seconds:g}s",
            flush=True,
        )
        await self._observe_transitions(
            timezone,
            self._controller.offset_seconds,
            report,
            context,
        )
        errors = [
            transition["boundary_error_seconds"]
            for transition in report["transitions"]
        ]
        intervals = [
            report["transitions"][index]["observed_monotonic_seconds"]
            - report["transitions"][index - 1]["observed_monotonic_seconds"]
            for index in range(1, len(report["transitions"]))
        ]
        report["summary"] = {
            "transition_count": len(errors),
            "mean_boundary_error_seconds": round(statistics.fmean(errors), 6),
            "boundary_jitter_seconds": round(
                max(errors) - min(errors),
                6,
            ),
            "boundary_drift_seconds": round(errors[-1] - errors[0], 6),
            "mean_interval_seconds": (
                round(statistics.fmean(intervals), 6) if intervals else None
            ),
            "interval_jitter_seconds": (
                round(max(intervals) - min(intervals), 6) if intervals else None
            ),
            "interval_errors_seconds": [
                round(interval - 60.0, 6) for interval in intervals
            ],
        }
        context.artifacts.write_json(self._config.artifact_name, report)

        self._controller.set_offset(0)
        report["http_force_sync"] = await self._force_sync(context)
        self._controller.mark_panel_restored()
        context.artifacts.write_json(self._config.artifact_name, report)
        return report

    async def restore(self, context: ScenarioContext) -> None:
        self._controller.set_offset(0)
        if self._controller.panel_requires_restore:
            print("[CLEAN ] Restoring panel clock to real time", flush=True)
            await self._force_sync(context)
            self._controller.mark_panel_restored()

    async def _observe_transitions(
        self,
        timezone: ZoneInfo,
        offset_seconds: int,
        report: dict[str, Any],
        context: ScenarioContext,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._config.observation_seconds
        previous = self._panel_time(await self._api.status())
        transitions: list[dict[str, Any]] = report["transitions"]
        while loop.time() < deadline:
            await asyncio.sleep(
                min(self._config.poll_seconds, max(0.0, deadline - loop.time()))
            )
            panel_time = self._panel_time(await self._api.status())
            if panel_time == previous:
                continue
            observed = datetime.now(timezone)
            expected = observed + timedelta(seconds=offset_seconds)
            expected_display = expected.strftime("%-I:%M%p")
            boundary_error = self._boundary_error_seconds(panel_time, expected)
            transition = {
                "from": previous,
                "to": panel_time,
                "expected": expected_display,
                "observed_at": observed.isoformat(),
                "observed_monotonic_seconds": round(loop.time(), 6),
                "boundary_error_seconds": round(boundary_error, 6),
                "correlated_with_expected_boundary": abs(boundary_error)
                <= self._config.convergence_tolerance_seconds,
            }
            transitions.append(transition)
            context.artifacts.write_json(self._config.artifact_name, report)
            print(
                f"[ TIME ] {previous} -> {panel_time}; boundary error "
                f"{transition['boundary_error_seconds']:.3f}s",
                flush=True,
            )
            previous = panel_time

        minimum = max(1, int(self._config.observation_seconds // 60) - 1)
        if len(transitions) < minimum:
            raise PanelTimeSyncFailure(
                f"observed {len(transitions)} minute transitions; expected at "
                f"least {minimum} during {self._config.observation_seconds:g}s"
            )
        mismatches = [
            transition
            for transition in transitions
            if not transition["correlated_with_expected_boundary"]
        ]
        if mismatches:
            raise PanelTimeSyncFailure(
                f"{len(mismatches)} observed panel transitions were outside the "
                "expected clock tolerance"
            )

    async def _force_sync(self, context: ScenarioContext) -> dict[str, Any]:
        requested = context.timeline.offset_ns()
        cursor = context.monitor.cursor
        await self._api.request(
            "PUT",
            "/api/set_date_time",
            timeout_seconds=self._config.action_timeout_seconds,
        )
        active = await self._programmer.wait_for_active(
            context.monitor,
            context.timeline,
            task_name=self._config.task_name,
            marker=self._config.active_marker,
            after=cursor,
            requested_offset_ns=requested,
            timeout_seconds=self._config.activation_timeout_seconds,
        )
        completed = await self._programmer.wait_for_completion(
            context.monitor,
            context.timeline,
            task_name=self._config.task_name,
            marker=self._config.finished_marker,
            active=active,
            timeout_seconds=self._config.action_timeout_seconds,
        )
        difference = await self._wait_for_time(offset_seconds=0)
        return {
            "endpoint": "/api/set_date_time",
            "method": "PUT",
            "requested_offset_ns": requested,
            "active_offset_ns": active.offset_ns,
            "completed_offset_ns": completed.offset_ns,
            "panel_difference_seconds": difference,
        }

    async def _wait_for_time(self, *, offset_seconds: int) -> int:
        timezone = ZoneInfo(self._config.timezone)
        deadline = (
            asyncio.get_running_loop().time() + self._config.action_timeout_seconds
        )
        while True:
            difference = self._panel_time_difference(
                self._panel_time(await self._api.status()),
                datetime.now(timezone) + timedelta(seconds=offset_seconds),
            )
            if difference <= self._config.convergence_tolerance_seconds:
                return difference
            if asyncio.get_running_loop().time() >= deadline:
                raise PanelTimeSyncFailure(
                    "Panel clock did not converge to the expected process time; "
                    f"difference {difference}s"
                )
            await asyncio.sleep(self._config.poll_seconds)

    @staticmethod
    def _panel_time(status: dict[str, Any]) -> str:
        value = status.get("time")
        if not isinstance(value, str):
            raise PanelTimeSyncFailure("/api/status did not contain panel time")
        return value.strip()

    @staticmethod
    def _normalize_time(value: str) -> str:
        return value.replace(" ", "").upper().lstrip("0")

    @classmethod
    def _boundary_error_seconds(cls, panel_time: str, expected: datetime) -> float:
        try:
            panel = datetime.strptime(cls._normalize_time(panel_time), "%I:%M%p")
        except ValueError as error:
            raise PanelTimeSyncFailure(
                f"could not parse panel time {panel_time!r}"
            ) from error
        expected_minutes = expected.hour * 60 + expected.minute
        panel_minutes = panel.hour * 60 + panel.minute
        minute_delta = (panel_minutes - expected_minutes + 720) % 1440 - 720
        return (
            expected.second
            + expected.microsecond / 1_000_000
            - minute_delta * 60
        )

    @classmethod
    def _panel_time_difference(cls, panel_time: str, expected: datetime) -> int:
        boundary_error = cls._boundary_error_seconds(panel_time, expected)
        difference = round(abs(boundary_error))
        return min(difference, 24 * 3600 - difference)
