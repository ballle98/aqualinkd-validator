from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...interfaces import AqualinkApi

PanelSignature = tuple[str | None, int | None, bool | None]
ProgressSink = Callable[[str], None]


@dataclass(frozen=True)
class PdaPanelIdentityConfig:
    timezone: str
    time_tolerance_seconds: float
    timeout_seconds: float
    poll_seconds: float = 0.25


@dataclass(frozen=True)
class PdaPanelIdentityResult:
    panel: dict[str, Any]
    checks: tuple[dict[str, Any], ...]
    reported_panel_size: int | None
    reported_panel_combo: bool | None


class PdaPanelIdentityFailure(RuntimeError):
    """Raised when panel identity or clock validation cannot complete."""

    def __init__(self, message: str, result: PdaPanelIdentityResult) -> None:
        super().__init__(message)
        self.result = result


class PdaPanelIdentityValidator:
    """Validate the initialized PDA panel identity and clock through the API."""

    def __init__(
        self,
        *,
        api: AqualinkApi,
        config: PdaPanelIdentityConfig,
        progress: ProgressSink,
        now: Callable[[ZoneInfo], datetime] | None = None,
    ) -> None:
        self._api = api
        self._config = config
        self._progress = progress
        self._now = now or datetime.now

    async def validate(
        self,
        *,
        init_screen: dict[str, str],
        configured_panel: str | None,
    ) -> PdaPanelIdentityResult:
        status = await self._api.status()
        initial_identity = self.api_identity(status)
        panel_report = {
            "init_screen": init_screen,
            "api_status_after_init": initial_identity,
        }
        reported_panel = init_screen["panel_type"]
        reported_signature = self.panel_signature(reported_panel)
        type_check = self.panel_type_check(configured_panel, reported_panel)
        checks = [type_check]
        self._progress(
            f"[INFO  ] Panel reported: {reported_panel}; "
            f"firmware {init_screen['firmware']}"
        )
        if type_check["status"] == "warning":
            self._progress(
                "[ WARN ] Configured panel type does not match the "
                f"physical panel: configured {configured_panel}; "
                f"reported {reported_panel}"
            )

        partial = self._result(panel_report, checks, reported_signature)
        try:
            timezone = ZoneInfo(self._config.timezone)
        except ZoneInfoNotFoundError as error:
            raise PdaPanelIdentityFailure(
                f"Unknown panel timezone: {self._config.timezone}", partial
            ) from error

        deadline = asyncio.get_running_loop().time() + self._config.timeout_seconds
        wait_started = time.monotonic()
        announced_wait = False
        while True:
            try:
                panel_time, now, difference = self.panel_time_difference(
                    status,
                    timezone,
                    now=self._now,
                )
            except ValueError as error:
                raise PdaPanelIdentityFailure(str(error), partial) from error
            if difference <= self._config.time_tolerance_seconds:
                passed = True
                break
            if not announced_wait:
                self._progress(
                    "[ WAIT ] Panel clock: waiting for initialization-time "
                    f"synchronization (timeout {self._config.timeout_seconds:g}s)"
                )
                announced_wait = True
            if asyncio.get_running_loop().time() >= deadline:
                passed = False
                break
            await asyncio.sleep(self._config.poll_seconds)
            status = await self._api.status()

        waited_seconds = time.monotonic() - wait_started
        final_identity = self.api_identity(status)
        if final_identity != initial_identity:
            panel_report["api_status_after_clock_sync"] = final_identity
        checks.append(
            {
                "name": "panel.time",
                "status": "passed" if passed else "failed",
                "panel_time": panel_time,
                "system_time": now.isoformat(),
                "timezone": self._config.timezone,
                "difference_seconds": difference,
                "waited_seconds": round(waited_seconds, 3),
                "tolerance_seconds": self._config.time_tolerance_seconds,
            }
        )
        result = self._result(panel_report, checks, reported_signature)
        if not passed:
            raise PdaPanelIdentityFailure(
                f"Panel time differs from {self._config.timezone} system time "
                f"by {difference}s; tolerance is "
                f"{self._config.time_tolerance_seconds:g}s",
                result,
            )
        return result

    @classmethod
    def panel_type_check(
        cls,
        configured: str | None,
        reported: str,
    ) -> dict[str, Any]:
        configured_signature = cls.panel_signature(configured)
        reported_signature = cls.panel_signature(reported)
        comparable = (
            configured_signature[0] is not None
            and reported_signature[0] is not None
            and configured_signature[1] is not None
            and reported_signature[1] is not None
        )
        matches = comparable and configured_signature == reported_signature
        return {
            "name": "panel.type",
            "status": "passed" if matches else "warning",
            "configured": configured,
            "reported": reported,
            "configured_signature": list(configured_signature),
            "reported_signature": list(reported_signature),
            "reason": (
                None
                if matches
                else "Configured panel identity differs from panel screen"
            ),
        }

    @staticmethod
    def panel_signature(value: str | None) -> PanelSignature:
        if value is None:
            return (None, None, None)
        normalized = value.upper()
        family = "PDA" if "PDA" in normalized else None
        capacity_match = re.search(r"PDA-(?:PS)?(\d+)", normalized)
        capacity = int(capacity_match.group(1)) if capacity_match else None
        combo = "COMBO" in normalized if family is not None else None
        return (family, capacity, combo)

    @staticmethod
    def api_identity(status: dict[str, Any]) -> dict[str, Any]:
        return {
            key: status.get(key)
            for key in (
                "panel_type_full",
                "panel_type",
                "version",
                "date",
                "time",
            )
        }

    @staticmethod
    def panel_time_difference(
        status: dict[str, Any],
        timezone: ZoneInfo,
        *,
        now: Callable[[ZoneInfo], datetime] = datetime.now,
    ) -> tuple[str, datetime, int]:
        panel_time = status.get("time")
        if not isinstance(panel_time, str):
            raise ValueError("/api/status did not contain panel time")
        try:
            parsed = datetime.strptime(panel_time.strip().upper(), "%I:%M%p")
        except ValueError as error:
            raise ValueError(f"Could not parse panel time {panel_time!r}") from error

        system_time = now(timezone)
        panel_seconds = parsed.hour * 3600 + parsed.minute * 60
        host_seconds = (
            system_time.hour * 3600 + system_time.minute * 60 + system_time.second
        )
        difference = abs(panel_seconds - host_seconds)
        difference = min(difference, 24 * 3600 - difference)
        return panel_time.strip(), system_time, difference

    @staticmethod
    def _result(
        panel: dict[str, Any],
        checks: list[dict[str, Any]],
        signature: PanelSignature,
    ) -> PdaPanelIdentityResult:
        return PdaPanelIdentityResult(
            panel=panel,
            checks=tuple(checks),
            reported_panel_size=signature[1],
            reported_panel_combo=signature[2],
        )
