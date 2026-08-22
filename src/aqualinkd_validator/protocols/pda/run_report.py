from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...engine import RestorationResult, ScenarioRecorder
from .identity import PdaPanelIdentityResult
from .session import PdaStartupResult


@dataclass(frozen=True)
class PdaRunReportConfig:
    suite_name: str
    execution_phase: str
    api_base_url: str | None
    api_endpoint_source: str
    activation_timeout_seconds: float
    action_timeout_seconds: float
    status_timeout_seconds: float
    state_timeout_seconds: float
    restoration_timeout_seconds: float
    init_timeout_seconds: float
    sleep_timeout_seconds: float
    status_retry_command_delay_seconds: float
    probe_command_min_delay_seconds: float
    spa_fill_seconds: float | None
    device_selection_mode: str
    requested_devices: tuple[str, ...]
    disabled_button_numbers: tuple[int, ...]


class PdaRunReport:
    """Own the PDA run report schema and protocol-specific updates."""

    def __init__(self, config: PdaRunReportConfig) -> None:
        self.data: dict[str, Any] = {
            "schema_version": 1,
            "suite": config.suite_name,
            "execution_phase": config.execution_phase,
            "api_base_url": config.api_base_url,
            "api_endpoint_source": config.api_endpoint_source,
            "status": "running",
            "reason": None,
            "safe_to_continue": False,
            "timeouts_seconds": {
                "activation": config.activation_timeout_seconds,
                "action": config.action_timeout_seconds,
                "status": config.status_timeout_seconds,
                "state": config.state_timeout_seconds,
                "restoration": config.restoration_timeout_seconds,
                "init": config.init_timeout_seconds,
                "sleep": config.sleep_timeout_seconds,
                "status_retry_command_delay": (
                    config.status_retry_command_delay_seconds
                ),
                "probe_command_min_delay": (
                    config.probe_command_min_delay_seconds
                ),
            },
            "site_profile": {"spa_fill_seconds": config.spa_fill_seconds},
            "checks": [],
            "aqualinkd": None,
            "panel": None,
            "equipment_status": None,
            "equipment_state_observations": [],
            "sleep_cycle": None,
            "aquapda_transport": None,
            "menu_walk": None,
            "cases": [],
            "measurements": [],
            "skipped": [],
            "device_selection": {
                "mode": config.device_selection_mode,
                "requested": list(config.requested_devices),
                "resolved": [],
                "configured_none_buttons": list(config.disabled_button_numbers),
                "reported_panel_size": None,
                "excluded": [],
            },
            "restoration": {
                "attempted": False,
                "status": "not-needed",
                "actions": [],
                "errors": [],
            },
        }
        self.recorder = ScenarioRecorder(self.data)

    def record_startup(
        self,
        result: PdaStartupResult,
        recorder: ScenarioRecorder,
    ) -> None:
        self.data["aqualinkd"] = result.aqualinkd_identity
        recorder.append_measurement(
            name="pda.init",
            category="initialization",
            phase="startup",
            target="PDA_INIT",
            requested_value=None,
            start_offset_ns=0,
            api_ack_offset_ns=None,
            task_active_offset_ns=result.active.offset_ns,
            log_completion_offset_ns=result.completed.offset_ns,
            state_observed_offset_ns=None,
        )

    def configure_api(self, base_url: str, source: str) -> None:
        self.data["api_base_url"] = base_url
        self.data["api_endpoint_source"] = source

    def record_panel_identity(self, result: PdaPanelIdentityResult) -> None:
        self.data["panel"] = result.panel
        self.data["checks"].extend(result.checks)
        self.data["device_selection"]["reported_panel_size"] = (
            result.reported_panel_size
        )

    def begin_restoration(self) -> None:
        self.data["restoration"]["attempted"] = True

    def record_restoration(self, result: RestorationResult) -> None:
        restoration = self.data["restoration"]
        restoration["actions"].extend(
            {
                "target": action.target,
                "property": action.property,
                "value": action.value,
                "status": action.status,
            }
            for action in result.actions
        )
        restoration["errors"].extend(result.errors)
        restoration["status"] = "passed" if result.passed else "failed"
