from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .domain import EquipmentSnapshot


class ApiError(RuntimeError):
    """Raised when AqualinkD's HTTP API cannot satisfy a request."""
class AqualinkHttpApi:
    def __init__(self, base_url: str, timeout_seconds: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        parsed = urlsplit(self._base_url)
        if parsed.scheme != "http" or parsed.hostname is None:
            raise ValueError(f"invalid HTTP API base URL: {base_url}")
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._base_path = parsed.path.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    async def devices(self) -> EquipmentSnapshot:
        payload = await self._request_json("/api/devices")
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise ApiError("/api/devices response does not contain a device list")
        result: dict[str, dict[str, Any]] = {}
        for device in devices:
            if not isinstance(device, dict):
                continue
            identifier = device.get("id")
            if isinstance(identifier, str):
                result[identifier] = device
        temp_units = payload.get("temp_units")
        return EquipmentSnapshot(
            temp_units=temp_units if isinstance(temp_units, str) else "u",
            devices=result,
        )

    async def status(self) -> dict[str, Any]:
        return await self._request_json("/api/status")

    async def set_device(self, identifier: str, enabled: bool) -> None:
        path = f"/api/{quote(identifier, safe='')}/set"
        await self._put_value(path, int(enabled))

    async def set_setpoint(self, identifier: str, value: int) -> None:
        path = f"/api/{quote(identifier, safe='')}/setpoint/set"
        await self._put_value(path, value)

    async def _request_json(self, path: str) -> dict[str, Any]:
        body = await self._request("GET", path)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise ApiError(f"{path} returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ApiError(f"{path} returned a non-object JSON value")
        return payload

    async def _put_value(self, path: str, value: int) -> None:
        data = urlencode({"value": value}).encode("ascii")
        await self._request("PUT", path, data)

    async def _request(
        self,
        method: str,
        path: str,
        body: bytes = b"",
    ) -> str:
        target = f"{self._base_path}{path}" or "/"
        host_header = self._host
        if ":" in host_header and not host_header.startswith("["):
            host_header = f"[{host_header}]"
        if self._port != 80:
            host_header = f"{host_header}:{self._port}"
        headers = [
            f"{method} {target} HTTP/1.1",
            f"Host: {host_header}",
            "Accept: application/json",
            "Connection: close",
        ]
        if body:
            headers.extend(
                [
                    "Content-Type: application/x-www-form-urlencoded",
                    f"Content-Length: {len(body)}",
                ]
            )
        payload = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body

        writer: asyncio.StreamWriter | None = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                self._timeout_seconds,
            )
            assert writer is not None
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), self._timeout_seconds)
            raw_headers = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"),
                self._timeout_seconds,
            )
            status, response_headers = self._parse_headers(raw_headers)
            content_length = response_headers.get("content-length")
            if content_length is not None:
                response_body = await asyncio.wait_for(
                    reader.readexactly(int(content_length)),
                    self._timeout_seconds,
                )
            else:
                response_body = await asyncio.wait_for(
                    reader.read(),
                    self._timeout_seconds,
                )
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as error:
            raise ApiError(
                f"{method} {self._base_url}{path} failed: {error}"
            ) from error
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(
                    OSError,
                    TimeoutError,
                ):
                    await asyncio.wait_for(
                        writer.wait_closed(),
                        self._timeout_seconds,
                    )

        text = response_body.decode("utf-8", errors="replace")
        if status < 200 or status >= 300:
            raise ApiError(
                f"{method} {self._base_url}{path} returned HTTP "
                f"{status}: {text.strip()}"
            )
        return text

    @staticmethod
    def _parse_headers(raw: bytes) -> tuple[int, dict[str, str]]:
        lines = raw.decode("iso-8859-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if len(status_parts) < 2:
            raise ApiError("HTTP response has an invalid status line")
        try:
            status = int(status_parts[1])
        except ValueError as error:
            raise ApiError("HTTP response has an invalid status code") from error
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
        return status, headers
