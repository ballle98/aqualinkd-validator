from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .artifacts import ArtifactStore
from .events import EventTimeline, OrderedLogEvents, ProcessOutputObserver


@dataclass(frozen=True)
class RunResult:
    status: str
    reason: str
    child_returncode: int | None
    duration_ns: int


@dataclass(frozen=True)
class ScenarioOutcome:
    status: str
    reason: str


@dataclass(frozen=True)
class ScenarioContext:
    artifacts: ArtifactStore
    monitor: OrderedLogEvents
    timeline: EventTimeline


class Scenario(Protocol):
    async def run(self, context: ScenarioContext) -> ScenarioOutcome: ...


class ProcessOutputObserverFactory(Protocol):
    def __call__(
        self,
        artifacts: ArtifactStore,
        timeline: EventTimeline,
    ) -> ProcessOutputObserver: ...


class ProcessRunner(Protocol):
    async def run(
        self,
        command: list[str],
        artifact_dir: Path,
        *,
        cwd: Path | None,
        duration_seconds: float | None,
        sample_interval_seconds: float,
        terminate_grace_seconds: float,
        scenario: Scenario | None = None,
        scenario_cleanup_seconds: float = 120.0,
        output_observer_factories: tuple[ProcessOutputObserverFactory, ...] = (),
    ) -> RunResult: ...
