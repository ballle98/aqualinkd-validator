from __future__ import annotations

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from aqualinkd_validator.domain import EquipmentSnapshot
from aqualinkd_validator.interfaces import ScenarioContext
from aqualinkd_validator.panel_free import (
    PanelFreeScenario,
    run_panel_free_testcase,
)
from aqualinkd_validator.testcases import (
    ExpectPanelCommandStep,
    ExpectSerialStep,
    HttpRequestStep,
    PanelFixtureDefinition,
    SerialSendStep,
)
from aqualinkd_validator.testcases import (
    TestcaseDefinition as CaseDefinition,
)
from aqualinkd_validator.testcases import (
    TestcaseRequirements as CaseRequirements,
)
from aqualinkd_validator.testing import (
    FakeAqualinkApi,
    FakeOrderedLogEvents,
    FakeProcessRunner,
    FakeSerialTransport,
    FakeTimeline,
    MemoryArtifactStore,
)


def _testcase() -> CaseDefinition:
    return CaseDefinition(
        schema=1,
        identifier="rs485.probe",
        description="Probe exchange",
        mode="rs485-panel-emulator",
        access="read-write",
        requirements=CaseRequirements("rs485"),
        steps=(
            SerialSendStep(b"\x10\x02", 1.0),
            ExpectSerialStep(b"\x10\x03", 1.0),
        ),
        finally_steps=(),
        fixture=PanelFixtureDefinition("RS-4 Combo", "0x0a"),
    )


class _AllButtonTransport(FakeSerialTransport):
    def __init__(self) -> None:
        super().__init__()
        self.command_enabled = False
        self.command_sent = False

    async def write(self, payload: bytes) -> None:
        await super().write(payload)
        if payload[3] != 0x02:
            return
        command = 0
        if self.command_enabled and not self.command_sent:
            command = 0x02
            self.command_sent = True
        packet = bytes((0x10, 0x02, 0x00, 0x01, 0x00, command))
        await self.incoming.put(
            packet + bytes((sum(packet) & 0xFF, 0x10, 0x03))
        )


class _CommandEnablingApi(FakeAqualinkApi):
    def __init__(self, transport: _AllButtonTransport) -> None:
        super().__init__(EquipmentSnapshot(temp_units="F", devices={}))
        self._transport = transport

    async def request(
        self,
        method: str,
        path: str,
        *,
        value: str | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        response = await super().request(
            method,
            path,
            value=value,
            timeout_seconds=timeout_seconds,
        )
        if method == "PUT":
            self._transport.command_enabled = True
        return response


class PanelFreeScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_serial_yaml_after_http_is_ready(self) -> None:
        transport = FakeSerialTransport()
        await transport.incoming.put(b"\x10\x03")
        artifacts = MemoryArtifactStore()
        context = ScenarioContext(
            artifacts=artifacts,
            monitor=FakeOrderedLogEvents(),
            timeline=FakeTimeline(),
        )
        scenario = PanelFreeScenario(
            testcase=_testcase(),
            transport=transport,
            api=FakeAqualinkApi(EquipmentSnapshot(temp_units="F", devices={})),
        )
        outcome = await scenario.run(context)
        await scenario.close()

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(transport.outgoing, [b"\x10\x02"])
        self.assertFalse(transport.is_open)
        self.assertEqual(artifacts.json("scenario.json")["status"], "passed")
        self.assertTrue(artifacts.binary_values["serial.pcapng"])
        history = artifacts.values["http.jsonl"].splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0])["purpose"], "readiness")

    async def test_runs_http_action_against_stateful_allbutton_driver(self) -> None:
        transport = _AllButtonTransport()
        testcase = CaseDefinition(
            schema=1,
            identifier="rs485.allbutton-filter",
            description="HTTP to AllButton command",
            mode="rs485-panel-emulator",
            access="read-write",
            requirements=CaseRequirements("rs485"),
            steps=(
                HttpRequestStep("PUT", "/api/Filter_Pump/set", "1", 1),
                ExpectPanelCommandStep(0x02, 1),
            ),
            finally_steps=(),
            fixture=PanelFixtureDefinition(
                "RS-4 Combo",
                "0x0a",
                driver="allbutton",
            ),
        )
        artifacts = MemoryArtifactStore()
        scenario = PanelFreeScenario(
            testcase=testcase,
            transport=transport,
            api=_CommandEnablingApi(transport),
        )
        outcome = await scenario.run(
            ScenarioContext(
                artifacts=artifacts,
                monitor=FakeOrderedLogEvents(),
                timeline=FakeTimeline(),
            )
        )
        await scenario.close()

        self.assertEqual(outcome.status, "passed")
        self.assertTrue(transport.command_sent)
        history = [
            json.loads(line)
            for line in artifacts.values["http.jsonl"].splitlines()
        ]
        self.assertEqual(
            [item["purpose"] for item in history],
            ["readiness", "testcase"],
        )


class PanelFreeCompositionTests(unittest.TestCase):
    def test_composes_generated_runtime_with_process_runner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "aqualinkd"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
            web = root / "web"
            web.mkdir()
            artifacts = root / "artifacts"
            output = io.StringIO()
            process = _IdentityProcessRunner(output)
            with contextlib.redirect_stdout(output):
                result = asyncio.run(
                    run_panel_free_testcase(
                        testcase=_testcase(),
                        aqualinkd=binary,
                        web_directory=web,
                        artifact_dir=artifacts,
                        process_runner=process,
                    )
                )

            self.assertEqual(result.status, "passed")
            self.assertEqual(process.commands[0][0], str(binary))
            self.assertEqual(process.commands[0][1:3], ["-d", "-c"])
            config = (artifacts / "effective-aqualinkd.conf").read_text()
            self.assertIn("panel_type = RS-4 Combo", config)
            self.assertIn("serial_port = /dev/pts/", config)
            manifest = json.loads((artifacts / "manifest.yaml").read_text())
            self.assertEqual(manifest["testcase"]["id"], "rs485.probe")
            self.assertEqual(manifest["aqualinkd"]["reported_version"], "v3.1.1")
            self.assertEqual(
                manifest["aqualinkd"]["configured_panel_type"],
                "RS-4 Combo (Pool Only)",
            )
            self.assertEqual(manifest["fixture"]["panel_type"], "RS-4 Combo")
            self.assertEqual(
                manifest["serial"]["capture"]["fidelity"],
                {
                    "bytes": "exact",
                    "direction": "exact",
                    "framing": "exact_for_complete_dle_frames",
                    "timing": "exact_monotonic_at_frame_completion",
                },
            )
            self.assertEqual(manifest["config"]["name"], "effective-aqualinkd.conf")
            self.assertEqual(len(manifest["config"]["sha256"]), 64)
            self.assertEqual(len(manifest["aqualinkd"]["sha256"]), 64)
            self.assertIn(f"AqualinkD: {binary}", process.output_before_run)
            self.assertIn("Generated config: ", process.output_before_run)
            self.assertIn("Serial PTY: /dev/pts/", process.output_before_run)
            self.assertIn(
                "HTTP API: http://127.0.0.1:", process.output_before_run
            )


class _IdentityProcessRunner(FakeProcessRunner):
    def __init__(self, output: io.StringIO) -> None:
        super().__init__()
        self._output = output
        self.output_before_run = ""

    async def run(self, command, artifact_dir, **kwargs):
        self.output_before_run = self._output.getvalue()
        (artifact_dir / "stdout.log").write_text(
            "AqualinkD: Starting Aqualink Daemon v3.1.1 !\n"
            "AqualinkD: panel type = RS-4 Combo (Pool Only)\n",
            encoding="utf-8",
        )
        return await super().run(command, artifact_dir, **kwargs)


if __name__ == "__main__":
    unittest.main()
