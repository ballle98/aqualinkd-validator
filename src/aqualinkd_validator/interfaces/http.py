from __future__ import annotations

from typing import Protocol


class HttpTransport(Protocol):
    """Bounded raw HTTP operations used by declarative testcases."""

    @property
    def base_url(self) -> str: ...

    async def request(
        self,
        method: str,
        path: str,
        *,
        value: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str: ...
