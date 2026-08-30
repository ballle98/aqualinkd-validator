from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LineEvent:
    sequence: int
    offset_ns: int
    stream: str
    text: str


class OrderedLogEvents(Protocol):
    """Ordered, cursor-addressable output events from the supervised SUT."""

    @property
    def cursor(self) -> int: ...

    def recent_events(self, *, before: int | None = None) -> list[LineEvent]: ...

    async def wait_for(
        self,
        predicate: str,
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent: ...

    async def wait_for_any(
        self,
        predicates: tuple[str, ...],
        *,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent: ...

    async def wait_for_match(
        self,
        predicate: Callable[[LineEvent], bool],
        *,
        description: str,
        after: int = 0,
        timeout_seconds: float,
    ) -> LineEvent: ...


class ProcessOutputObserver(Protocol):
    """Incremental observer of complete output lines from the supervised SUT."""

    async def observe(self, event: LineEvent) -> None: ...

    async def close(self) -> None: ...


class EventTimeline(Protocol):
    """Monotonic event and measurement sink."""

    def offset_ns(self) -> int: ...

    async def write(self, kind: str, **fields: Any) -> int: ...

    def close(self) -> None: ...
