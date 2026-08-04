from __future__ import annotations

from .pda.suites import SUITES, PdaSuiteDefinition, get_suite

SuiteProfile = PdaSuiteDefinition

__all__ = ["SUITES", "SuiteProfile", "get_suite"]
