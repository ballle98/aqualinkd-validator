from __future__ import annotations

import asyncio
import unittest
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.protocols.pda import (
    PdaPanelIdentityConfig,
    PdaPanelIdentityFailure,
    PdaPanelIdentityResult,
    PdaPanelIdentityValidator,
)


class StatusApi:
    base_url = "http://127.0.0.1:8080"

    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self._statuses = iter(statuses)
        self._last: dict[str, Any] | None = None

    async def status(self) -> dict[str, Any]:
        try:
            self._last = next(self._statuses)
        except StopIteration:
            if self._last is None:
                raise AssertionError("No API status was configured") from None
        return self._last

    async def devices(self) -> EquipmentSnapshot:
        raise NotImplementedError

    async def set_device(self, identifier: str, enabled: bool) -> None:
        raise NotImplementedError

    async def set_setpoint(self, identifier: str, value: int) -> None:
        raise NotImplementedError


class PdaPanelIdentityValidatorTests(unittest.TestCase):
    def test_matching_identity_and_synchronized_clock(self) -> None:
        result, progress = asyncio.run(
            self._validate(
                [self._status(time="12:00PM")],
                configured="PDA-6 Combo (Pool & Spa)",
            )
        )

        self.assertEqual(result.reported_panel_size, 6)
        self.assertTrue(result.reported_panel_combo)
        self.assertEqual(
            [check["status"] for check in result.checks],
            ["passed", "passed"],
        )
        self.assertEqual(result.checks[1]["difference_seconds"], 30)
        self.assertEqual(
            progress,
            ["[INFO  ] Panel reported: PDA-PS6 Combo; firmware PDA: 7.1.0"],
        )

    def test_clock_wait_records_final_api_identity(self) -> None:
        first = self._status(time="12:10PM", date="08/19/26")
        synchronized = self._status(time="12:00PM", date="08/20/26")
        result, progress = asyncio.run(
            self._validate([first, synchronized], configured="PDA-6 Combo")
        )

        self.assertIn("api_status_after_clock_sync", result.panel)
        self.assertEqual(
            result.panel["api_status_after_clock_sync"]["date"],
            "08/20/26",
        )
        self.assertTrue(
            any(message.startswith("[ WAIT ] Panel clock") for message in progress)
        )

    def test_failed_clock_check_is_available_on_failure(self) -> None:
        async def run() -> PdaPanelIdentityFailure:
            try:
                await self._validator(
                    [self._status(time="12:10PM")],
                    timeout_seconds=0,
                ).validate(
                    init_screen=self._init_screen(),
                    configured_panel="PDA-8 Combo",
                )
            except PdaPanelIdentityFailure as error:
                return error
            raise AssertionError("Expected panel clock validation to fail")

        error = asyncio.run(run())
        self.assertIn("Panel time differs", str(error))
        self.assertEqual(
            [check["status"] for check in error.result.checks],
            ["warning", "failed"],
        )

    def test_clock_difference_wraps_across_midnight(self) -> None:
        _, _, difference = PdaPanelIdentityValidator.panel_time_difference(
            {"time": "11:59PM"},
            ZoneInfo("UTC"),
            now=lambda timezone: datetime(2026, 8, 20, 0, 0, 30, tzinfo=timezone),
        )
        self.assertEqual(difference, 90)

    async def _validate(
        self,
        statuses: list[dict[str, Any]],
        *,
        configured: str,
    ) -> tuple[PdaPanelIdentityResult, list[str]]:
        progress: list[str] = []
        result = await self._validator(statuses, progress=progress).validate(
            init_screen=self._init_screen(),
            configured_panel=configured,
        )
        return result, progress

    def _validator(
        self,
        statuses: list[dict[str, Any]],
        *,
        progress: list[str] | None = None,
        timeout_seconds: float = 1,
    ) -> PdaPanelIdentityValidator:
        messages = progress if progress is not None else []
        return PdaPanelIdentityValidator(
            api=StatusApi(statuses),
            config=PdaPanelIdentityConfig(
                timezone="UTC",
                time_tolerance_seconds=120,
                timeout_seconds=timeout_seconds,
                poll_seconds=0,
            ),
            progress=messages.append,
            now=lambda timezone: datetime(2026, 8, 20, 12, 0, 30, tzinfo=timezone),
        )

    @staticmethod
    def _init_screen() -> dict[str, str]:
        return {
            "panel_type": "PDA-PS6 Combo",
            "firmware": "PDA: 7.1.0",
            "source": "pda_firmware_version_screen",
        }

    @staticmethod
    def _status(*, time: str, date: str = "08/20/26") -> dict[str, Any]:
        return {
            "panel_type_full": "PDA-PS6 Combo",
            "panel_type": "PDA",
            "version": "7.1.0",
            "date": date,
            "time": time,
        }


if __name__ == "__main__":
    unittest.main()
