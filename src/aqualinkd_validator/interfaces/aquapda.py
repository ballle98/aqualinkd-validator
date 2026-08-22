from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class PdaScreenView(Protocol):
    @property
    def lines(self) -> Sequence[str]: ...

    @property
    def title(self) -> str: ...

    @property
    def highlighted_text(self) -> str | None: ...


class AquaPdaClient(Protocol):
    @property
    def screen(self) -> PdaScreenView: ...

    @property
    def packet_count(self) -> int: ...

    @property
    def screen_update_count(self) -> int: ...

    async def connect(self) -> None: ...

    async def send_key(self, key: str) -> None: ...

    async def wait_for_packets(
        self,
        count: int,
        *,
        after: int = 0,
        timeout_seconds: float = 10.0,
    ) -> int: ...

    async def wait_for_highlight_change(
        self,
        previous: str | None,
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> str: ...

    async def wait_for_screen_change(
        self,
        previous: tuple[str, ...],
        *,
        after: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[str, ...]: ...

    async def wait_for_screen_settle(
        self,
        *,
        after: int,
        timeout_seconds: float = 5.0,
        idle_seconds: float = 0.15,
    ) -> int: ...

    async def close(self) -> None: ...
