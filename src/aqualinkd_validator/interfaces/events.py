from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..supervisor import LineEvent


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


class EventTimeline(Protocol):
    """Monotonic event and measurement sink."""

    def offset_ns(self) -> int: ...

    async def write(self, kind: str, **fields: Any) -> int: ...
