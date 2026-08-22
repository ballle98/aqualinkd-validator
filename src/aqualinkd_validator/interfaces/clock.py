from __future__ import annotations

from typing import Protocol


class MonotonicClock(Protocol):
    def seconds(self) -> float: ...

    def nanoseconds(self) -> int: ...

    async def sleep(self, seconds: float) -> None: ...
