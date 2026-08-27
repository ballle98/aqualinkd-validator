from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ...adapters.aquapda import AquaPdaProtocolError
from ...interfaces import AquaPdaClient, OrderedLogEvents

_SERIAL_SEND_TIME = re.compile(
    r"Time from recv to (?:blocking )?send is\s+([0-9.]+)\s+sec",
    re.IGNORECASE,
)
_NAVIGATION_FAILURE_MARKERS = (
    "waitForPDAnextMenu - received STATUS instead of CLEAR",
    "can't goto PM_EQUIPTMENT_CONTROL menu",
    "PDA Wake Init :- can't find menu",
)


class AquaPdaValidationFailure(RuntimeError):
    """Raised when AquaPDA transport or read-only navigation is invalid."""


@dataclass(frozen=True)
class AquaPdaTransportConfig:
    packet_count: int = 20
    timeout_seconds: float = 20.0
    send_time_limit_seconds: float = 0.010


@dataclass(frozen=True)
class AquaPdaTransportResult:
    report: dict[str, Any]


@dataclass(frozen=True)
class AquaPdaMenuWalkConfig:
    timeout_seconds: float = 20.0
    maximum_depth: int = 8
    maximum_screens: int = 100
    maximum_options: int = 32
    home_attempts: int = 8
    settle_seconds: float = 0.5


@dataclass(frozen=True)
class AquaPdaMenuWalkResult:
    report: dict[str, Any]


class AquaPdaTransportValidator:
    """Validate the northbound AquaPDA WebSocket and its RS485 ACK path."""

    def __init__(
        self,
        *,
        client: AquaPdaClient,
        events: OrderedLogEvents,
        config: AquaPdaTransportConfig,
        progress: Callable[[str], None],
    ) -> None:
        self._client = client
        self._events = events
        self._config = config
        self._progress = progress

    async def validate(self) -> AquaPdaTransportResult:
        log_cursor = self._events.cursor
        packet_start = 0
        try:
            self._progress(
                "[ WAIT ] Activating AquaPDA WebSocket interface and "
                "observing RS485 traffic"
            )
            await self._client.connect()
            packet_start = self._client.packet_count
            await self._client.wait_for_packets(
                self._config.packet_count,
                after=packet_start,
                timeout_seconds=self._config.timeout_seconds,
            )
            before_back = self._client.packet_count
            await self._client.send_key("back")
            await self._client.wait_for_packets(
                2,
                after=before_back,
                timeout_seconds=self._config.timeout_seconds,
            )
        finally:
            await self._client.close()

        events = [
            event
            for event in self._events.recent_events()
            if event.sequence > log_cursor
        ]
        corruption = [
            event.text
            for event in events
            if (
                "Serial read bad Jandy checksum" in event.text
                or "BAD PACKET" in event.text
            )
        ]
        navigation_failures = [
            event.text
            for event in events
            if any(marker in event.text for marker in _NAVIGATION_FAILURE_MARKERS)
        ]
        send_times = [
            float(match.group(1))
            for event in events
            if (match := _SERIAL_SEND_TIME.search(event.text)) is not None
        ]
        slow_send_times = [
            value
            for value in send_times
            if value > self._config.send_time_limit_seconds
        ]
        packet_count = self._client.packet_count - packet_start
        report = {
            "packets_observed": packet_count,
            "bad_packets": corruption,
            "navigation_failures": navigation_failures,
            "send_time_samples_seconds": send_times,
            "maximum_send_time_seconds": max(send_times, default=None),
            "send_time_limit_seconds": self._config.send_time_limit_seconds,
            "slow_send_times_seconds": slow_send_times,
        }
        self._progress(
            f"[STATE ] AquaPDA WebSocket delivered {packet_count} packets; "
            f"BAD PACKET count {len(corruption)}"
        )
        if corruption:
            raise AquaPdaValidationFailure(
                "AquaPDA WebSocket caused bad-checksum/BAD PACKET traffic "
                f"({len(corruption)} log entries); see ballle98/AqualinkD#94 "
                "and ballle98/AqualinkD#95"
            )
        if navigation_failures:
            raise AquaPdaValidationFailure(
                "AquaPDA WebSocket caused PDA navigation failures: "
                + "; ".join(navigation_failures)
            )
        if slow_send_times:
            raise AquaPdaValidationFailure(
                "AquaPDA ACK path exceeded the "
                f"{self._config.send_time_limit_seconds * 1000:g}ms transport "
                "budget: "
                + ", ".join(f"{value:.3f}s" for value in slow_send_times)
            )
        return AquaPdaTransportResult(report)


class AquaPdaMenuWalker:
    """Recursively visit AquaPDA structural menus without changing settings."""

    def __init__(
        self,
        *,
        client: AquaPdaClient,
        config: AquaPdaMenuWalkConfig,
        progress: Callable[[str], None],
    ) -> None:
        self._client = client
        self._config = config
        self._progress = progress

    async def walk(self) -> AquaPdaMenuWalkResult:
        visited: list[dict[str, Any]] = []
        try:
            await self._client.connect()
            await self._client.wait_for_packets(
                6,
                timeout_seconds=self._config.timeout_seconds,
            )
            await self._client.wait_for_screen_settle(
                after=0,
                timeout_seconds=self._config.timeout_seconds,
            )
            await self._return_home()
            await self._walk_menus(path=("HOME",), visited=visited, depth=0)
        finally:
            await self._client.close()
        self._progress(
            f"[STATE ] AquaPDA menu walk visited {len(visited)} screens"
        )
        if len(visited) < 2:
            raise AquaPdaValidationFailure(
                "AquaPDA menu walk did not reach the main menu"
            )
        return AquaPdaMenuWalkResult(
            {"screens_visited": len(visited), "screens": visited}
        )

    async def _return_home(self) -> None:
        for _ in range(self._config.home_attempts):
            visible = {
                line.strip().upper() for line in self._client.screen.lines
            }
            if {"MENU", "EQUIPMENT ON/OFF"}.issubset(visible):
                self._progress("[STATE ] AquaPDA returned to the home screen")
                return
            await self._send_and_wait_for_screen("back")
        raise AquaPdaValidationFailure(
            "AquaPDA menu walk could not identify the home screen containing "
            "MENU and EQUIPMENT ON/OFF"
        )

    async def _walk_menus(
        self,
        *,
        path: tuple[str, ...],
        visited: list[dict[str, Any]],
        depth: int,
    ) -> None:
        if (
            depth > self._config.maximum_depth
            or len(visited) >= self._config.maximum_screens
        ):
            raise AquaPdaValidationFailure(
                "AquaPDA menu walk exceeded its traversal bound"
            )
        options = await self._enumerate_options()
        self._progress(
            f"[ WALK ] {' / '.join(path)}: {len(options)} selectable item(s)"
        )
        visited.append(
            {
                "path": list(path),
                "title": self._client.screen.title,
                "lines": [line.rstrip() for line in self._client.screen.lines],
                "options": options,
            }
        )
        candidates = [
            option
            for option in options
            if option.upper() in {"MENU", "EQUIPMENT ON/OFF"}
            or option.endswith(">")
        ]
        for option in candidates:
            await self._move_to_option(option)
            await self._send_and_wait_for_screen("select")
            await self._walk_menus(
                path=(*path, option.rstrip(" >")),
                visited=visited,
                depth=depth + 1,
            )
            await self._send_and_wait_for_screen("back")

    async def _enumerate_options(self) -> list[str]:
        first = self._client.screen.highlighted_text
        if not first:
            return []
        options = [first]
        for _ in range(self._config.maximum_options - 1):
            previous = self._client.screen.highlighted_text
            try:
                await self._send_and_wait_for_highlight(
                    "down",
                    previous,
                    timeout_seconds=min(2.0, self._config.timeout_seconds),
                )
            except AquaPdaProtocolError as error:
                if "highlight did not change" not in str(error):
                    raise
                break
            current = self._client.screen.highlighted_text
            if not current or current in options:
                break
            options.append(current)
        return options

    async def _move_to_option(self, target: str) -> None:
        for _ in range(self._config.maximum_options):
            current = self._client.screen.highlighted_text
            if current == target:
                return
            await self._send_and_wait_for_highlight("down", current)
        raise AquaPdaValidationFailure(
            f"AquaPDA menu item disappeared during walk: {target}"
        )

    async def _send_and_wait_for_highlight(
        self,
        key: str,
        previous: str | None,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        before_packets = self._client.packet_count
        before_updates = self._client.screen_update_count
        await self._client.send_key(key)
        await self._client.wait_for_highlight_change(
            previous,
            after=before_packets,
            timeout_seconds=timeout_seconds or self._config.timeout_seconds,
        )
        await self._client.wait_for_screen_settle(
            after=before_updates,
            timeout_seconds=self._config.timeout_seconds,
            idle_seconds=self._config.settle_seconds,
        )

    async def _send_and_wait_for_screen(self, key: str) -> None:
        before_packets = self._client.packet_count
        before_updates = self._client.screen_update_count
        previous = tuple(self._client.screen.lines)
        await self._client.send_key(key)
        await self._client.wait_for_screen_change(
            previous,
            after=before_packets,
            timeout_seconds=self._config.timeout_seconds,
        )
        await self._client.wait_for_screen_settle(
            after=before_updates,
            timeout_seconds=self._config.timeout_seconds,
            idle_seconds=self._config.settle_seconds,
        )
