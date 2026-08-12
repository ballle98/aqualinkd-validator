from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .cases import CASES, PdaCaseId


@dataclass(frozen=True)
class PdaSuiteDefinition:
    name: str
    description: str
    mode: str = "live-panel"
    aqualinkd_args: tuple[str, ...] = ("-vv",)
    cases: tuple[PdaCaseId, ...] = ()
    config_overrides: tuple[tuple[str, str], ...] = ()
    members: tuple[str, ...] = ()
    execution_role: Literal["single", "awake", "sleep"] = "single"
    artifact_suffix: str | None = None

    @property
    def is_composite(self) -> bool:
        return bool(self.members)

    @property
    def changes_all_discovered_devices(self) -> bool:
        return PdaCaseId.EQUIPMENT_STATUS in self.cases

    @property
    def mutates_panel(self) -> bool:
        return any(CASES[case_id].mutates_panel for case_id in self.cases)

    def override_map(self) -> dict[str, str]:
        return dict(self.config_overrides)


SUITES: dict[str, PdaSuiteDefinition] = {
    "pda-live-long": PdaSuiteDefinition(
        name="pda-live-long",
        description="Composite awake and sleep PDA live-panel validation",
        members=("pda-live-awake", "pda-live-sleep"),
    ),
    "pda-live-simulator": PdaSuiteDefinition(
        name="pda-live-simulator",
        description=(
            "AquaPDA WebSocket and RS485 transport regressions for "
            "ballle98/AqualinkD#94 and ballle98/AqualinkD#95"
        ),
        cases=(
            PdaCaseId.INITIALIZATION,
            PdaCaseId.SIMULATOR_TRANSPORT,
        ),
        config_overrides=(("pda_sleep_mode", "no"),),
        execution_role="awake",
        artifact_suffix="simulator",
    ),
    "pda-live-simulator-menu-walk": PdaSuiteDefinition(
        name="pda-live-simulator-menu-walk",
        description=(
            "Read-only traversal using AqualinkD's PDA simulator against a live panel"
        ),
        cases=(
            PdaCaseId.INITIALIZATION,
            PdaCaseId.SIMULATOR_TRANSPORT,
            PdaCaseId.MENU_WALK,
        ),
        config_overrides=(("pda_sleep_mode", "no"),),
        execution_role="awake",
        artifact_suffix="live-simulator-menu-walk",
    ),
    "pda-powercenter-simulator-menu-walk": PdaSuiteDefinition(
        name="pda-powercenter-simulator-menu-walk",
        description=(
            "Read-only PDA traversal with AqualinkD connected to the "
            "Windows power-center simulator"
        ),
        mode="jandy-simulator",
        cases=(
            PdaCaseId.INITIALIZATION,
            PdaCaseId.SIMULATOR_TRANSPORT,
            PdaCaseId.MENU_WALK,
        ),
        config_overrides=(("pda_sleep_mode", "no"),),
        execution_role="awake",
        artifact_suffix="powercenter-simulator-menu-walk",
    ),
}


def get_suite(name: str | None) -> PdaSuiteDefinition | None:
    if name is None:
        return None
    return SUITES[name]
