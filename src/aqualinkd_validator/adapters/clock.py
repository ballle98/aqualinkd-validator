from __future__ import annotations

import asyncio
import time


class SystemMonotonicClock:
    def seconds(self) -> float:
        return time.monotonic()

    def nanoseconds(self) -> int:
        return time.monotonic_ns()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
