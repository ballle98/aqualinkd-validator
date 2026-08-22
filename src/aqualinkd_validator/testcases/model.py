from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias

TestcaseMode: TypeAlias = Literal["physical-panel", "rs485-panel-emulator"]
TestcaseAccess: TypeAlias = Literal["read-only", "read-write"]
TestcaseProtocol: TypeAlias = Literal["pda", "rs485"]
HttpMethod: TypeAlias = Literal["GET", "PUT"]
DeviceTargetState: TypeAlias = Literal[
    "on",
    "off",
    "original",
    "opposite-of-original",
    "requested",
]
SetpointTarget: TypeAlias = int | Literal["original"]


@dataclass(frozen=True)
class TestcaseRequirements:
    protocol: TestcaseProtocol


@dataclass(frozen=True)
class PanelFixtureDefinition:
    panel_type: str
    device_id: str
    rssa_device_id: str | None = None
    extended_device_id: str | None = None
    overrides: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class WaitForStep:
    condition: str
    timeout_seconds: float
    keyword: str = "wait_for"


@dataclass(frozen=True)
class SerialSendStep:
    payload: bytes
    timeout_seconds: float
    keyword: str = "serial_send"


@dataclass(frozen=True)
class ExpectSerialStep:
    payload: bytes
    timeout_seconds: float
    keyword: str = "expect_serial"


@dataclass(frozen=True)
class HttpRequestStep:
    method: HttpMethod
    path: str
    value: str | None
    timeout_seconds: float
    keyword: str = "http_request"


@dataclass(frozen=True)
class SetDeviceStep:
    identifier: str
    state: DeviceTargetState
    activation_timeout_seconds: float
    completion_timeout_seconds: float
    convergence_timeout_seconds: float
    keyword: str = "set_device"


@dataclass(frozen=True)
class SetSetpointStep:
    identifier: str
    value: SetpointTarget
    activation_timeout_seconds: float
    completion_timeout_seconds: float
    convergence_timeout_seconds: float
    keyword: str = "set_setpoint"


@dataclass(frozen=True)
class ExerciseHeaterStep:
    identifier: str
    optional: bool
    activation_timeout_seconds: float
    completion_timeout_seconds: float
    convergence_timeout_seconds: float
    keyword: str = "exercise_heater"


@dataclass(frozen=True)
class ExerciseSpaHeatingStep:
    timeout_seconds: float
    keyword: str = "exercise_spa_heating"


@dataclass(frozen=True)
class AssertDeviceStep:
    identifier: str
    state: DeviceTargetState
    timeout_seconds: float
    keyword: str = "assert_device"


@dataclass(frozen=True)
class AssertLogStep:
    contains: str
    timeout_seconds: float
    keyword: str = "assert_log"


@dataclass(frozen=True)
class AssertNoLogStep:
    contains: str | None
    level: str | None
    duration_seconds: float
    keyword: str = "assert_no_log"


@dataclass(frozen=True)
class WaitForStableEquipmentStep:
    identifiers: tuple[str, ...]
    timeout_seconds: float
    keyword: str = "wait_for_stable_equipment"


@dataclass(frozen=True)
class RestoreOriginalStateStep:
    timeout_seconds: float
    keyword: str = "restore_original_state"


@dataclass(frozen=True)
class VerifyEquipmentStatusStep:
    timeout_seconds: float
    keyword: str = "verify_equipment_status"


@dataclass(frozen=True)
class ExerciseDiscoveredDevicesStep:
    timeout_seconds: float
    keyword: str = "exercise_discovered_devices"


@dataclass(frozen=True)
class ObserveSleepCycleStep:
    timeout_seconds: float
    keyword: str = "observe_sleep_cycle"


@dataclass(frozen=True)
class ExerciseStatusRetryStep:
    timeout_seconds: float
    keyword: str = "exercise_status_retry"


@dataclass(frozen=True)
class ExerciseProbeTransitionStep:
    timeout_seconds: float
    keyword: str = "exercise_probe_transition"


TestcaseStep: TypeAlias = (
    WaitForStep
    | SerialSendStep
    | ExpectSerialStep
    | HttpRequestStep
    | SetDeviceStep
    | SetSetpointStep
    | ExerciseHeaterStep
    | ExerciseSpaHeatingStep
    | AssertDeviceStep
    | AssertLogStep
    | AssertNoLogStep
    | WaitForStableEquipmentStep
    | RestoreOriginalStateStep
    | VerifyEquipmentStatusStep
    | ExerciseDiscoveredDevicesStep
    | ObserveSleepCycleStep
    | ExerciseStatusRetryStep
    | ExerciseProbeTransitionStep
)


@dataclass(frozen=True)
class TestcaseDefinition:
    schema: int
    identifier: str
    description: str
    mode: TestcaseMode
    access: TestcaseAccess
    requirements: TestcaseRequirements
    steps: tuple[TestcaseStep, ...]
    finally_steps: tuple[TestcaseStep, ...]
    fixture: PanelFixtureDefinition | None = None


@dataclass(frozen=True)
class TestcaseSuiteConfig:
    aqualinkd_args: tuple[str, ...]
    overrides: tuple[tuple[str, str], ...]
    execution_role: Literal["single", "awake", "sleep"]

    def override_map(self) -> dict[str, str]:
        return dict(self.overrides)


@dataclass(frozen=True)
class TestcaseSuiteMember:
    source: Path
    testcase: TestcaseDefinition


@dataclass(frozen=True)
class TestcaseSuiteDefinition:
    schema: int
    identifier: str
    description: str
    mode: TestcaseMode
    access: TestcaseAccess
    requirements: TestcaseRequirements
    config: TestcaseSuiteConfig
    members: tuple[TestcaseSuiteMember, ...]

    @property
    def mutates_panel(self) -> bool:
        return any(member.testcase.access == "read-write" for member in self.members)

    @property
    def exercises_discovered_devices(self) -> bool:
        return any(
            any(
                isinstance(step, ExerciseDiscoveredDevicesStep)
                for step in member.testcase.steps
            )
            for member in self.members
        )

    @property
    def uses_selected_devices(self) -> bool:
        selected_device_steps = (
            ExerciseDiscoveredDevicesStep,
            ExerciseStatusRetryStep,
            ExerciseProbeTransitionStep,
        )
        return any(
            any(
                isinstance(step, selected_device_steps)
                for step in member.testcase.steps
            )
            for member in self.members
        )
