from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .pda.cases import PdaCaseId
from .pda.suites import SUITES as LEGACY_SUITES
from .testcases import (
    TestcaseDefinition,
    TestcaseSuiteDefinition,
    load_testcase_document,
)

RunMode = Literal["live-panel", "jandy-simulator"]
TargetKind = Literal["testcase", "suite", "composite", "legacy-suite"]

_DEVICE_SELECTION_CASES = frozenset(
    {
        PdaCaseId.CONSECUTIVE_DEVICES,
        PdaCaseId.DEVICE_DURING_STATUS_RETRY,
        PdaCaseId.DEVICE_AFTER_PROBE,
    }
)


@dataclass(frozen=True)
class ResolvedRunTarget:
    """One normalized execution target, independent of its source format."""

    identifier: str
    description: str
    kind: TargetKind
    mode: RunMode
    mutates_panel: bool
    uses_selected_devices: bool
    aqualinkd_args: tuple[str, ...]
    config_overrides: tuple[tuple[str, str], ...]
    execution_role: Literal["single", "awake", "sleep"]
    artifact_suffix: str | None = None
    source: Path | None = None
    schema: int | None = None
    access: Literal["read-only", "read-write"] | None = None
    testcase: TestcaseDefinition | None = None
    testcases: tuple[TestcaseDefinition, ...] = ()
    case_ids: tuple[PdaCaseId, ...] = ()
    members: tuple[str, ...] = ()

    @property
    def is_composite(self) -> bool:
        return self.kind == "composite"

    @property
    def is_testcase(self) -> bool:
        return self.kind == "testcase"

    @property
    def is_declarative_suite(self) -> bool:
        return self.kind == "suite"

    def override_map(self) -> dict[str, str]:
        return dict(self.config_overrides)


class RunTargetRegistry:
    """Resolve built-in names and user-provided YAML through one path."""

    def __init__(self, declarative_suites: dict[str, Path]) -> None:
        self._declarative_suites = declarative_suites

    @property
    def names(self) -> tuple[str, ...]:
        return tuple((*self._declarative_suites, *LEGACY_SUITES))

    def resolve(self, value: str) -> ResolvedRunTarget:
        if Path(value).suffix.casefold() in {".yaml", ".yml"}:
            source = Path(value).expanduser().resolve()
            return self._from_document(source)
        declarative_source = self._declarative_suites.get(value)
        if declarative_source is not None:
            return self._from_document(declarative_source)
        try:
            suite = LEGACY_SUITES[value]
        except KeyError as error:
            raise ValueError(f"unknown run target {value!r}") from error

        if suite.is_composite:
            members = tuple(self.resolve(member) for member in suite.members)
            return ResolvedRunTarget(
                identifier=suite.name,
                description=suite.description,
                kind="composite",
                mode=_run_mode(suite.mode),
                mutates_panel=any(member.mutates_panel for member in members),
                uses_selected_devices=any(
                    member.uses_selected_devices for member in members
                ),
                aqualinkd_args=(),
                config_overrides=(),
                execution_role="single",
                members=suite.members,
            )

        return ResolvedRunTarget(
            identifier=suite.name,
            description=suite.description,
            kind="legacy-suite",
            mode=_run_mode(suite.mode),
            mutates_panel=suite.mutates_panel,
            uses_selected_devices=any(
                case_id in _DEVICE_SELECTION_CASES for case_id in suite.cases
            ),
            aqualinkd_args=suite.aqualinkd_args,
            config_overrides=suite.config_overrides,
            execution_role=suite.execution_role,
            artifact_suffix=suite.artifact_suffix,
            case_ids=suite.cases,
        )

    def _from_document(self, source: Path) -> ResolvedRunTarget:
        document = load_testcase_document(source)
        if document.mode != "physical-panel":
            raise ValueError(
                f"{source}: execution for testcase mode "
                f"{document.mode!r} is not implemented"
            )
        if isinstance(document, TestcaseDefinition):
            return ResolvedRunTarget(
                identifier=document.identifier,
                description=document.description,
                kind="testcase",
                mode="live-panel",
                mutates_panel=document.access == "read-write",
                uses_selected_devices=False,
                aqualinkd_args=("-vv",),
                config_overrides=(),
                execution_role="single",
                source=source,
                schema=document.schema,
                access=document.access,
                testcase=document,
            )

        assert isinstance(document, TestcaseSuiteDefinition)
        return ResolvedRunTarget(
            identifier=document.identifier,
            description=document.description,
            kind="suite",
            mode="live-panel",
            mutates_panel=document.mutates_panel,
            uses_selected_devices=document.uses_selected_devices,
            aqualinkd_args=document.config.aqualinkd_args,
            config_overrides=document.config.overrides,
            execution_role=document.config.execution_role,
            source=source,
            schema=document.schema,
            access=document.access,
            testcases=tuple(member.testcase for member in document.members),
        )


_ROOT = Path(__file__).parents[2]
RUN_TARGETS = RunTargetRegistry(
    {
        name: _ROOT / "testcases" / "suites" / f"{name}.yaml"
        for name in (
            "pda-live-fast",
            "pda-live-awake",
            "pda-live-sleep",
            "pda-live-spa",
        )
    }
)


def _run_mode(value: str) -> RunMode:
    if value not in {"live-panel", "jandy-simulator"}:
        raise ValueError(f"unsupported run mode {value!r}")
    return cast(RunMode, value)
