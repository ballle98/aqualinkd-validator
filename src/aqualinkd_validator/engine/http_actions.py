from __future__ import annotations

import asyncio
import json
from typing import TextIO

from ..interfaces import ArtifactStore, EventTimeline, HttpTransport


class HttpActionFailure(RuntimeError):
    """Raised when a bounded declarative HTTP operation fails."""


class HttpActions:
    """Record bounded HTTP requests in the shared timeline and http.jsonl."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        timeline: EventTimeline,
        artifacts: ArtifactStore,
    ) -> None:
        self._transport = transport
        self._timeline = timeline
        self._history: TextIO = artifacts.open_text("http.jsonl")
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    async def wait_ready(
        self,
        *,
        timeout_seconds: float,
        poll_seconds: float = 0.05,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        last_error: Exception | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise HttpActionFailure(
                    f"HTTP API did not become ready within {timeout_seconds:g}s: "
                    f"{last_error}"
                )
            try:
                await self.request(
                    "GET",
                    "/api/status",
                    value=None,
                    timeout_seconds=min(1.0, remaining),
                    purpose="readiness",
                )
                return
            except HttpActionFailure as error:
                last_error = error
            remaining = deadline - asyncio.get_running_loop().time()
            await asyncio.sleep(min(poll_seconds, remaining))

    async def request(
        self,
        method: str,
        path: str,
        *,
        value: str | None,
        timeout_seconds: float,
        purpose: str = "testcase",
    ) -> str:
        started_ns = self._timeline.offset_ns()
        await self._timeline.write(
            "http_request",
            method=method,
            path=path,
            value=value,
            purpose=purpose,
            timeout_seconds=timeout_seconds,
        )
        try:
            response = await asyncio.wait_for(
                self._transport.request(
                    method,
                    path,
                    value=value,
                    timeout_seconds=timeout_seconds,
                ),
                timeout=timeout_seconds,
            )
        except Exception as error:
            finished_ns = self._timeline.offset_ns()
            message = f"{type(error).__name__}: {error}"
            await self._timeline.write(
                "http_request_failed",
                method=method,
                path=path,
                purpose=purpose,
                error=message,
            )
            self._write_history(
                started_ns=started_ns,
                finished_ns=finished_ns,
                method=method,
                path=path,
                value=value,
                purpose=purpose,
                response=None,
                error=message,
            )
            raise HttpActionFailure(
                f"{method} {path} failed after {timeout_seconds:g}s: {message}"
            ) from error
        finished_ns = self._timeline.offset_ns()
        await self._timeline.write(
            "http_response",
            method=method,
            path=path,
            purpose=purpose,
            response_bytes=len(response.encode("utf-8")),
        )
        self._write_history(
            started_ns=started_ns,
            finished_ns=finished_ns,
            method=method,
            path=path,
            value=value,
            purpose=purpose,
            response=response,
            error=None,
        )
        return response

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._history.close()

    def _write_history(
        self,
        *,
        started_ns: int,
        finished_ns: int,
        method: str,
        path: str,
        value: str | None,
        purpose: str,
        response: str | None,
        error: str | None,
    ) -> None:
        record = {
            "offset_ns": started_ns,
            "duration_ns": max(0, finished_ns - started_ns),
            "method": method,
            "url": f"{self.base_url}{path}",
            "value": value,
            "purpose": purpose,
            "response": response,
            "error": error,
        }
        self._history.write(json.dumps(record, separators=(",", ":")) + "\n")
        self._history.flush()
