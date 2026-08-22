from __future__ import annotations

import asyncio
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
    ExpectSerialStep,
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
        outcome = await PanelFreeScenario(
            testcase=_testcase(),
            transport=transport,
            api=FakeAqualinkApi(EquipmentSnapshot(temp_units="F", devices={})),
        ).run(context)

        self.assertEqual(outcome.status, "passed")
        self.assertEqual(transport.outgoing, [b"\x10\x02"])
        self.assertFalse(transport.is_open)
        self.assertEqual(artifacts.json("scenario.json")["status"], "passed")
        self.assertTrue(artifacts.binary_values["serial.pcapng"])
        history = artifacts.values["http.jsonl"].splitlines()
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0])["purpose"], "readiness")


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
            process = FakeProcessRunner()

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
            self.assertEqual(manifest["testcase"], "rs485.probe")


if __name__ == "__main__":
    unittest.main()
