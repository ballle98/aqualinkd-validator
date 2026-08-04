from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PdaCaseId(StrEnum):
    INITIALIZATION = "initialization"
    FILTER_AFTER_INIT = "filter-after-init"
    POOL_HEATER = "pool-heater"
    EQUIPMENT_STATUS = "equipment-status"
    CONSECUTIVE_DEVICES = "consecutive-devices"
    SLEEP_CYCLE = "sleep-cycle"
    FILTER_FROM_SLEEP = "filter-from-sleep"


@dataclass(frozen=True)
class PdaCaseDefinition:
    id: PdaCaseId
    name: str
    mutates_panel: bool


CASES: dict[PdaCaseId, PdaCaseDefinition] = {
    PdaCaseId.INITIALIZATION: PdaCaseDefinition(
        id=PdaCaseId.INITIALIZATION,
        name="PDA initialization, identity, and clock",
        mutates_panel=False,
    ),
    PdaCaseId.FILTER_AFTER_INIT: PdaCaseDefinition(
        id=PdaCaseId.FILTER_AFTER_INIT,
        name="Filter pump after initialization",
        mutates_panel=True,
    ),
    PdaCaseId.POOL_HEATER: PdaCaseDefinition(
        id=PdaCaseId.POOL_HEATER,
        name="Pool heater controls",
        mutates_panel=True,
    ),
    PdaCaseId.EQUIPMENT_STATUS: PdaCaseDefinition(
        id=PdaCaseId.EQUIPMENT_STATUS,
        name="Equipment-status full-page reconciliation",
        mutates_panel=True,
    ),
    PdaCaseId.CONSECUTIVE_DEVICES: PdaCaseDefinition(
        id=PdaCaseId.CONSECUTIVE_DEVICES,
        name="Consecutive device operations",
        mutates_panel=True,
    ),
    PdaCaseId.SLEEP_CYCLE: PdaCaseDefinition(
        id=PdaCaseId.SLEEP_CYCLE,
        name="Natural PDA sleep/wake duty cycle",
        mutates_panel=False,
    ),
    PdaCaseId.FILTER_FROM_SLEEP: PdaCaseDefinition(
        id=PdaCaseId.FILTER_FROM_SLEEP,
        name="Filter pump while PDA is sleeping",
        mutates_panel=True,
    ),
}
