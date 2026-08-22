from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from ...config import normalize_api_base_url
from ...interfaces import EventTimeline, LineEvent, OrderedLogEvents
from .programmer import PdaProgrammerFailure, PdaProgrammerObserver

INIT_FINISHED = "(Init PDA) finished"
INIT_ACTIVE = "is active (Init PDA)"
FIRMWARE_VERSION_SCREEN = "PDA Menu Line 3 = Firmware Version"
WEB_SERVER_STARTED = "Starting web server on "

_PDA_MENU_LINE = re.compile(r"PDA Menu Line (\d+) =\s*(.*?)\s*$")
_WEB_SERVER_URL = re.compile(r"Starting web server on\s+(\S+)")
_WEB_SERVER_PORT = re.compile(r"Starting web server on port\s+(\d+)")
_AQUALINKD_VERSION = re.compile(
    r"(?:Starting\s+)?Aqualink Daemon\s+(v.+?)(?:\s+!\s*)?$",
    re.IGNORECASE,
)
_CONFIGURED_PANEL = re.compile(
    r"(?:Panel set to|panel type\s*=)\s*(.+?)\s*$",
    re.IGNORECASE,
)


class PdaSessionFailure(RuntimeError):
    """Raised when the PDA session cannot complete startup discovery."""


@dataclass(frozen=True)
class PdaStartupResult:
    active: LineEvent
    completed: LineEvent
    aqualinkd_identity: dict[str, str]
    init_screen: dict[str, str]
    discovered_api_base_url: str | None


class PdaSessionInitializer:
    """Correlate PDA_INIT, daemon identity, and HTTP endpoint discovery."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        programmer: PdaProgrammerObserver,
        timeout_seconds: float,
    ) -> None:
        self._events = events
        self._timeline = timeline
        self._programmer = programmer
        self._timeout_seconds = timeout_seconds

    async def initialize(self, *, discover_api: bool) -> PdaStartupResult:
        async with asyncio.TaskGroup() as tasks:
            startup_task = tasks.create_task(self._wait_for_pda_init())
            identity_task = tasks.create_task(self._capture_aqualinkd_identity())
            discovery_task = (
                tasks.create_task(self._discover_api_base_url())
                if discover_api
                else None
            )
        active, completed, init_screen = startup_task.result()
        return PdaStartupResult(
            active=active,
            completed=completed,
            aqualinkd_identity=identity_task.result(),
            init_screen=init_screen,
            discovered_api_base_url=(
                discovery_task.result() if discovery_task is not None else None
            ),
        )

    async def _wait_for_pda_init(
        self,
    ) -> tuple[LineEvent, LineEvent, dict[str, str]]:
        try:
            active = await self._programmer.wait_for_active(
                self._events,
                self._timeline,
                task_name="Init PDA",
                marker=INIT_ACTIVE,
                after=0,
                requested_offset_ns=0,
                timeout_seconds=self._timeout_seconds,
                wait_reason="waiting for the panel probe",
            )
            async with asyncio.TaskGroup() as tasks:
                completion_task = tasks.create_task(
                    self._programmer.wait_for_completion(
                        self._events,
                        self._timeline,
                        task_name="Init PDA",
                        marker=INIT_FINISHED,
                        active=active,
                        timeout_seconds=self._timeout_seconds,
                    )
                )
                screen_task = tasks.create_task(
                    self._capture_init_screen(after=active.sequence)
                )
        except PdaProgrammerFailure as error:
            raise PdaSessionFailure(str(error)) from error
        completed = completion_task.result()
        await self._timeline.write(
            "scenario_phase",
            phase="startup",
            state="PDA_INIT completed",
        )
        return active, completed, screen_task.result()

    async def _discover_api_base_url(self) -> str:
        event = await self._events.wait_for(
            WEB_SERVER_STARTED,
            timeout_seconds=self._timeout_seconds,
        )
        port_match = _WEB_SERVER_PORT.search(event.text)
        if port_match is not None:
            return normalize_api_base_url(f"http://127.0.0.1:{port_match.group(1)}")

        match = _WEB_SERVER_URL.search(event.text)
        if match is None:
            raise PdaSessionFailure(
                "AqualinkD web-server startup log did not contain a URL"
            )
        try:
            return normalize_api_base_url(match.group(1))
        except ValueError as error:
            raise PdaSessionFailure(
                f"Invalid AqualinkD web-server URL in log: {match.group(1)}"
            ) from error

    async def _capture_aqualinkd_identity(self) -> dict[str, str]:
        async with asyncio.TaskGroup() as tasks:
            version_task = tasks.create_task(
                self._events.wait_for(
                    "Aqualink Daemon v",
                    timeout_seconds=self._timeout_seconds,
                )
            )
            panel_task = tasks.create_task(
                self._events.wait_for_any(
                    ("Panel set to ", "panel type"),
                    timeout_seconds=self._timeout_seconds,
                )
            )

        version_match = _AQUALINKD_VERSION.search(version_task.result().text)
        if version_match is None:
            raise PdaSessionFailure(
                "AqualinkD startup log did not contain a parseable version"
            )
        panel_match = _CONFIGURED_PANEL.search(panel_task.result().text)
        if panel_match is None:
            raise PdaSessionFailure(
                "AqualinkD startup log did not contain a parseable panel type"
            )
        return {
            "version": version_match.group(1).strip(),
            "configured_panel_type": panel_match.group(1).strip(),
            "source": "aqualinkd_startup_log",
        }

    async def _capture_init_screen(self, *, after: int) -> dict[str, str]:
        firmware_marker = await self._events.wait_for(
            FIRMWARE_VERSION_SCREEN,
            after=after,
            timeout_seconds=self._timeout_seconds,
        )
        panel_type = ""
        for event in reversed(
            self._events.recent_events(before=firmware_marker.sequence)
        ):
            parsed = self.parse_menu_line(event.text)
            if parsed is not None and parsed[0] == 1:
                panel_type = parsed[1]
                break
        firmware_event = await self._events.wait_for(
            "PDA Menu Line 5 =",
            after=firmware_marker.sequence,
            timeout_seconds=self._timeout_seconds,
        )
        firmware = self.parse_menu_line(firmware_event.text)
        if not panel_type:
            raise PdaSessionFailure(
                "PDA firmware-version screen did not contain a panel type on line 1"
            )
        if firmware is None or firmware[0] != 5 or not firmware[1]:
            raise PdaSessionFailure(
                "PDA firmware-version screen did not contain firmware "
                "information on line 5"
            )
        return {
            "panel_type": panel_type,
            "firmware": firmware[1],
            "source": "pda_firmware_version_screen",
        }

    @staticmethod
    def parse_menu_line(text: str) -> tuple[int, str] | None:
        match = _PDA_MENU_LINE.search(text)
        if match is None:
            return None
        return int(match.group(1)), match.group(2).strip()
