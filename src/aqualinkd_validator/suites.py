from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SuiteProfile:
    name: str
    description: str
    mode: str
    aqualinkd_args: tuple[str, ...]
    include_state_waits: bool


SUITES: dict[str, SuiteProfile] = {
    "pda-live-fast": SuiteProfile(
        name="pda-live-fast",
        description="Fast PDA regression tests against a live RS485 panel",
        mode="live-panel",
        aqualinkd_args=("-vv",),
        include_state_waits=False,
    ),
    "pda-live-long": SuiteProfile(
        name="pda-live-long",
        description=(
            "Extended PDA live-panel tests including status and sleep states"
        ),
        mode="live-panel",
        aqualinkd_args=("-vv",),
        include_state_waits=True,
    ),
}


def get_suite(name: str | None) -> SuiteProfile | None:
    if name is None:
        return None
    return SUITES[name]
