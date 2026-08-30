from __future__ import annotations

import asyncio
import json
from typing import Any, TextIO

from ..interfaces import ArtifactStore, EventTimeline, HttpTransport

_MISSING = object()


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
        self._artifacts = artifacts
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

    async def wait_json(
        self,
        path: str,
        pointer: str,
        expected: str | int | float | bool | None,
        *,
        timeout_seconds: float,
        poll_seconds: float,
        request_timeout_seconds: float,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        attempts = 0
        last_response: str | None = None
        last_value: Any = _MISSING
        last_error: str | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            attempts += 1
            try:
                last_response = await self.request(
                    "GET",
                    path,
                    value=None,
                    timeout_seconds=min(request_timeout_seconds, remaining),
                    purpose="json_poll",
                )
                last_value = _resolve_json_pointer(
                    json.loads(last_response),
                    pointer,
                )
                last_error = None
                if _json_values_equal(last_value, expected):
                    await self._timeline.write(
                        "http_json_matched",
                        path=path,
                        pointer=pointer,
                        expected=expected,
                        attempts=attempts,
                    )
                    return
            except (HttpActionFailure, json.JSONDecodeError, ValueError) as error:
                last_error = f"{type(error).__name__}: {error}"

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_seconds, remaining))

        failure = {
            "schema_version": 1,
            "method": "GET",
            "url": f"{self.base_url}{path}",
            "pointer": pointer,
            "expected": expected,
            "timeout_seconds": timeout_seconds,
            "attempts": attempts,
            "last_response": last_response,
            "last_value_available": last_value is not _MISSING,
            "last_value": None if last_value is _MISSING else last_value,
            "last_error": last_error,
            "request_history": "http.jsonl",
        }
        self._artifacts.write_json("http-poll-failure.json", failure)
        await self._timeline.write(
            "http_json_poll_failed",
            path=path,
            pointer=pointer,
            expected=expected,
            attempts=attempts,
            last_value=None if last_value is _MISSING else last_value,
            last_error=last_error,
        )
        detail = (
            last_error
            if last_error is not None
            else f"last value was {last_value!r}"
        )
        raise HttpActionFailure(
            f"GET {path} JSON pointer {pointer!r} did not equal "
            f"{expected!r} within {timeout_seconds:g}s; {detail}; "
            "see http-poll-failure.json and http.jsonl"
        )

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


def _resolve_json_pointer(document: Any, pointer: str) -> Any:
    if pointer == "":
        return document
    current = document
    for encoded_token in pointer[1:].split("/"):
        token = encoded_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                raise ValueError(f"JSON pointer token {token!r} was not found")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit():
                raise ValueError(f"JSON pointer token {token!r} is not an array index")
            index = int(token)
            if index >= len(current):
                raise ValueError(f"JSON pointer array index {index} is out of range")
            current = current[index]
        else:
            raise ValueError(
                f"JSON pointer token {token!r} cannot traverse "
                f"{type(current).__name__}"
            )
    return current


def _json_values_equal(actual: Any, expected: Any) -> bool:
    return type(actual) is type(expected) and actual == expected
