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
    DEVICE_DURING_STATUS_RETRY = "device-during-status-retry"
    DEVICE_AFTER_PROBE = "device-after-probe"
    SIMULATOR_TRANSPORT = "simulator-transport"
    MENU_WALK = "menu-walk"


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
    PdaCaseId.DEVICE_DURING_STATUS_RETRY: PdaCaseDefinition(
        id=PdaCaseId.DEVICE_DURING_STATUS_RETRY,
        name="Device during PDA STATUS retries",
        mutates_panel=True,
    ),
    PdaCaseId.DEVICE_AFTER_PROBE: PdaCaseDefinition(
        id=PdaCaseId.DEVICE_AFTER_PROBE,
        name="Device after PDA probing begins",
        mutates_panel=True,
    ),
    PdaCaseId.SIMULATOR_TRANSPORT: PdaCaseDefinition(
        id=PdaCaseId.SIMULATOR_TRANSPORT,
        name="AquaPDA simulator transport integrity",
        mutates_panel=False,
    ),
    PdaCaseId.MENU_WALK: PdaCaseDefinition(
        id=PdaCaseId.MENU_WALK,
        name="PDA read-only menu walk",
        mutates_panel=False,
    ),
}
