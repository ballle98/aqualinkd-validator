from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias

TestcaseMode: TypeAlias = Literal["physical-panel", "rs485-panel-emulator"]
TestcaseAccess: TypeAlias = Literal["read-only", "read-write"]
TestcaseProtocol: TypeAlias = Literal["pda"]
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
class WaitForStep:
    condition: str
    timeout_seconds: float
    keyword: str = "wait_for"


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


TestcaseStep: TypeAlias = (
    WaitForStep
    | SetDeviceStep
    | SetSetpointStep
    | AssertDeviceStep
    | AssertLogStep
    | AssertNoLogStep
    | WaitForStableEquipmentStep
    | RestoreOriginalStateStep
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
