from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from ...domain import EquipmentSnapshot
from ...interfaces import AqualinkApi, EventTimeline, OrderedLogEvents
from .identity import (
    PdaPanelIdentityConfig,
    PdaPanelIdentityResult,
    PdaPanelIdentityValidator,
)
from .programmer import PdaProgrammerObserver
from .session import PdaSessionInitializer, PdaStartupResult

ApiFactory = Callable[[str], AqualinkApi]
ApiConfigured = Callable[[AqualinkApi, str], None]
SessionObserved = Callable[[PdaStartupResult], None]
SnapshotStabilizer = Callable[
    [AqualinkApi, Sequence[str], EquipmentSnapshot], Awaitable[EquipmentSnapshot]
]
ProgressSink = Callable[[str], None]


@dataclass(frozen=True)
class PdaStartupConfig:
    init_timeout_seconds: float
    api_timeout_seconds: float
    panel_timezone: str
    panel_time_tolerance_seconds: float
    api_poll_seconds: float = 0.25


@dataclass(frozen=True)
class PdaStartupOutcome:
    api: AqualinkApi
    api_endpoint_source: str
    session: PdaStartupResult
    snapshot: EquipmentSnapshot
    panel_identity: PdaPanelIdentityResult


class PdaStartupCoordinator:
    """Bring a PDA session, HTTP API, equipment, and identity into a ready state."""

    def __init__(
        self,
        *,
        events: OrderedLogEvents,
        timeline: EventTimeline,
        programmer: PdaProgrammerObserver,
        api_factory: ApiFactory,
        config: PdaStartupConfig,
        progress: ProgressSink,
        retryable_api_errors: tuple[type[Exception], ...],
    ) -> None:
        self._events = events
        self._timeline = timeline
        self._programmer = programmer
        self._api_factory = api_factory
        self._config = config
        self._progress = progress
        self._retryable_api_errors = retryable_api_errors

    async def initialize(
        self,
        *,
        api: AqualinkApi | None,
        api_base_url_override: str | None,
        api_configured: ApiConfigured,
        session_observed: SessionObserved,
        stabilize: SnapshotStabilizer,
    ) -> PdaStartupOutcome:
        endpoint_source = "injected"
        if api is None and api_base_url_override is not None:
            api = self._api_factory(api_base_url_override)
            endpoint_source = "explicit_override"
            api_configured(api, endpoint_source)

        session = await PdaSessionInitializer(
            events=self._events,
            timeline=self._timeline,
            programmer=self._programmer,
            timeout_seconds=self._config.init_timeout_seconds,
        ).initialize(discover_api=api is None)
        session_observed(session)
        if session.discovered_api_base_url is not None:
            api = self._api_factory(session.discovered_api_base_url)
            endpoint_source = "aqualinkd_startup_log"
            api_configured(api, endpoint_source)
            await self._timeline.write(
                "api_endpoint_discovered",
                api_base_url=session.discovered_api_base_url,
                source=endpoint_source,
            )
        if api is None:
            raise RuntimeError("AqualinkD HTTP API endpoint is not configured")

        identity = session.aqualinkd_identity
        self._progress(f"[INFO  ] AqualinkD version: {identity['version']}")
        self._progress(
            f"[INFO  ] Configured panel: {identity['configured_panel_type']}"
        )
        ready = await self._wait_for_api(api)
        snapshot = await stabilize(api, self.actionable_identifiers(ready), ready)
        panel_identity = await PdaPanelIdentityValidator(
            api=api,
            config=PdaPanelIdentityConfig(
                timezone=self._config.panel_timezone,
                time_tolerance_seconds=self._config.panel_time_tolerance_seconds,
                timeout_seconds=self._config.init_timeout_seconds,
            ),
            progress=self._progress,
        ).validate(
            init_screen=session.init_screen,
            configured_panel=identity["configured_panel_type"],
        )
        return PdaStartupOutcome(
            api=api,
            api_endpoint_source=endpoint_source,
            session=session,
            snapshot=snapshot,
            panel_identity=panel_identity,
        )

    async def _wait_for_api(self, api: AqualinkApi) -> EquipmentSnapshot:
        deadline = asyncio.get_running_loop().time() + self._config.api_timeout_seconds
        last_error: Exception | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                return await api.devices()
            except self._retryable_api_errors as error:
                last_error = error
                await asyncio.sleep(self._config.api_poll_seconds)
        raise RuntimeError(
            "AqualinkD HTTP API did not become ready after PDA_INIT"
            + (f": {last_error}" if last_error is not None else "")
        )

    @staticmethod
    def actionable_identifiers(snapshot: EquipmentSnapshot) -> tuple[str, ...]:
        return tuple(
            identifier
            for identifier, device in snapshot.devices.items()
            if device.get("type") in {"switch", "setpoint_thermo"}
        )
