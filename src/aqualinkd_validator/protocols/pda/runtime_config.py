from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ...run_targets import RuntimeCaseId
from . import equipment_status, session, sleep

FILTER_PUMP = "Filter_Pump"
POOL_HEATER = "Pool_Heater"
SPA_HEATER = "Spa_Heater"
INIT_ACTIVE = session.INIT_ACTIVE
INIT_FINISHED = session.INIT_FINISHED

DEVICE_FINISHED = "(Switch PDA device on/off) finished"
DEVICE_ACTIVE = "is active (Switch PDA device on/off)"
POOL_HEATER_SETPOINT_FINISHED = "(Set PDA Pool Heater) finished"
POOL_HEATER_SETPOINT_ACTIVE = "is active (Set PDA Pool Heater)"
LEGACY_POOL_HEATER_SETPOINT_FINISHED = "(Set Pool heater setpoint) finished"
LEGACY_POOL_HEATER_SETPOINT_ACTIVE = "is active (Set Pool heater setpoint)"
POOL_HEATER_SETPOINT_FINISHED_MARKERS = (
    POOL_HEATER_SETPOINT_FINISHED,
    LEGACY_POOL_HEATER_SETPOINT_FINISHED,
)
POOL_HEATER_SETPOINT_ACTIVE_MARKERS = (
    POOL_HEATER_SETPOINT_ACTIVE,
    LEGACY_POOL_HEATER_SETPOINT_ACTIVE,
)
SPA_HEATER_SETPOINT_FINISHED = "(Set PDA Spa Heater) finished"
SPA_HEATER_SETPOINT_ACTIVE = "is active (Set PDA Spa Heater)"
LEGACY_SPA_HEATER_SETPOINT_FINISHED = "(Set Spa heater setpoint) finished"
LEGACY_SPA_HEATER_SETPOINT_ACTIVE = "is active (Set Spa heater setpoint)"
SPA_HEATER_SETPOINT_FINISHED_MARKERS = (
    SPA_HEATER_SETPOINT_FINISHED,
    LEGACY_SPA_HEATER_SETPOINT_FINISHED,
)
SPA_HEATER_SETPOINT_ACTIVE_MARKERS = (
    SPA_HEATER_SETPOINT_ACTIVE,
    LEGACY_SPA_HEATER_SETPOINT_ACTIVE,
)
STATUS_MENU_PRESENT = equipment_status.STATUS_MENU_PRESENT
LEGACY_STATUS_MENU_PRESENT = equipment_status.LEGACY_STATUS_MENU_PRESENT
PDA_SLEEPING = sleep.PDA_SLEEPING
PDA_ADDRESS_STATUS = sleep.PDA_ADDRESS_STATUS
PDA_ADDRESS_PROBE = sleep.PDA_ADDRESS_PROBE
WAKE_INIT_ACTIVE = sleep.WAKE_INIT_ACTIVE
WAKE_INIT_FINISHED = sleep.WAKE_INIT_FINISHED
EQUIPMENT_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class PdaScenarioConfig:
    suite_name: str = "pda-live-fast"
    execution_phase: Literal["single", "awake", "sleep"] = "single"
    activation_timeout_seconds: float = 130.0
    action_timeout_seconds: float = 90.0
    status_timeout_seconds: float = 180.0
    state_timeout_seconds: float = 10.0
    restoration_timeout_seconds: float = 300.0
    init_timeout_seconds: float = 180.0
    sleep_timeout_seconds: float = 120.0
    status_retry_command_delay_seconds: float = 1.0
    probe_command_min_delay_seconds: float = 3.0
    test_devices: tuple[str, ...] = ()
    disabled_button_numbers: tuple[int, ...] = ()
    panel_timezone: str = "UTC"
    panel_time_tolerance_seconds: float = 120.0
    spa_fill_seconds: float | None = None
    case_ids: tuple[RuntimeCaseId, ...] = ()
    aquapda_packet_count: int = 20
    aquapda_timeout_seconds: float = 20.0
    force_status_home_with_aquapda: bool = False
