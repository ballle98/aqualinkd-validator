from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from aqualinkd_validator.adapters.http import AqualinkHttpApi


class FakeWriter:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class HttpApiTests(unittest.TestCase):
    def test_reads_devices_and_sends_encoded_values(self) -> None:
        asyncio.run(self._exercise_api())

    @patch(
        "aqualinkd_validator.adapters.http.asyncio.open_connection",
        new_callable=AsyncMock,
    )
    async def _exercise_api(self, open_connection: AsyncMock) -> None:
        response_body = json.dumps(
            {
                "temp_units": "f",
                "devices": [
                    {
                        "id": "Filter_Pump",
                        "int_status": "0",
                    }
                ],
            }
        ).encode()
        requests: list[FakeWriter] = []

        async def connect(
            host: str,
            port: int,
        ) -> tuple[asyncio.StreamReader, FakeWriter]:
            self.assertEqual((host, port), ("127.0.0.1", 8080))
            reader = asyncio.StreamReader()
            reader.feed_data(
                (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Length: {len(response_body)}\r\n"
                    "\r\n"
                ).encode()
                + response_body
            )
            reader.feed_eof()
            writer = FakeWriter()
            requests.append(writer)
            return reader, writer

        open_connection.side_effect = connect
        api = AqualinkHttpApi("http://127.0.0.1:8080")

        snapshot = await api.devices()
        status = await api.status()
        await api.set_device("Filter_Pump", True)
        await api.set_setpoint("Pool_Heater", 79)

        self.assertEqual(snapshot.temp_units, "f")
        self.assertEqual(status["temp_units"], "f")
        self.assertIn("Filter_Pump", snapshot.devices)
        self.assertIn(b"GET /api/devices HTTP/1.1", requests[0].data)
        self.assertIn(b"GET /api/status HTTP/1.1", requests[1].data)
        self.assertTrue(requests[2].data.endswith(b"value=1"))
        self.assertTrue(requests[3].data.endswith(b"value=79"))


if __name__ == "__main__":
    unittest.main()
