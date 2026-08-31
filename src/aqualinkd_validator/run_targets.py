from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal

from .testcases import (
    TestcaseDefinition,
    TestcaseSuiteDefinition,
    load_testcase_document,
)

RunMode = Literal["live-panel", "jandy-power-center"]
TargetKind = Literal["testcase", "suite", "composite", "python-suite"]


class RuntimeCaseId(StrEnum):
    INITIALIZATION = "initialization"
    AQUAPDA_TRANSPORT = "aquapda-transport"
    AQUAPDA_MENU_WALK = "aquapda-menu-walk"


@dataclass(frozen=True)
class RuntimeCaseDefinition:
    id: RuntimeCaseId
    name: str
    mutates_panel: bool = False


RUNTIME_CASES = {
    RuntimeCaseId.INITIALIZATION: RuntimeCaseDefinition(
        RuntimeCaseId.INITIALIZATION,
        "PDA initialization, identity, and clock",
    ),
    RuntimeCaseId.AQUAPDA_TRANSPORT: RuntimeCaseDefinition(
        RuntimeCaseId.AQUAPDA_TRANSPORT,
        "AquaPDA WebSocket transport integrity",
    ),
    RuntimeCaseId.AQUAPDA_MENU_WALK: RuntimeCaseDefinition(
        RuntimeCaseId.AQUAPDA_MENU_WALK,
        "AquaPDA read-only menu walk",
    ),
}


@dataclass(frozen=True)
class PythonTargetDefinition:
    identifier: str
    description: str
    mode: RunMode = "live-panel"
    cases: tuple[RuntimeCaseId, ...] = ()
    config_overrides: tuple[tuple[str, str], ...] = ()
    members: tuple[str, ...] = ()
    execution_role: Literal["single", "awake", "sleep"] = "single"
    artifact_suffix: str | None = None


@dataclass(frozen=True)
class DeclarativeTargetDefinition:
    source: Path
    mode: RunMode = "live-panel"
    description: str | None = None
    artifact_suffix: str | None = None


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
    case_ids: tuple[RuntimeCaseId, ...] = ()
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

    def __init__(
        self,
        declarative_suites: dict[str, DeclarativeTargetDefinition],
        python_targets: dict[str, PythonTargetDefinition],
    ) -> None:
        self._declarative_suites = declarative_suites
        self._python_targets = python_targets

    @property
    def names(self) -> tuple[str, ...]:
        return tuple((*self._declarative_suites, *self._python_targets))

    def resolve(self, value: str) -> ResolvedRunTarget:
        if Path(value).suffix.casefold() in {".yaml", ".yml"}:
            source = Path(value).expanduser().resolve()
            return self._from_document(source)
        declarative_target = self._declarative_suites.get(value)
        if declarative_target is not None:
            return self._from_document(
                declarative_target.source,
                identifier=value,
                mode=declarative_target.mode,
                description=declarative_target.description,
                artifact_suffix=declarative_target.artifact_suffix,
            )
        try:
            target = self._python_targets[value]
        except KeyError as error:
            raise ValueError(f"unknown run target {value!r}") from error

        if target.members:
            members = tuple(self.resolve(member) for member in target.members)
            incompatible = [
                member.identifier for member in members if member.mode != target.mode
            ]
            if incompatible:
                raise ValueError(
                    f"{target.identifier} has members for the wrong run mode: "
                    + ", ".join(incompatible)
                )
            return ResolvedRunTarget(
                identifier=target.identifier,
                description=target.description,
                kind="composite",
                mode=target.mode,
                mutates_panel=any(member.mutates_panel for member in members),
                uses_selected_devices=any(
                    member.uses_selected_devices for member in members
                ),
                aqualinkd_args=(),
                config_overrides=(),
                execution_role="single",
                members=target.members,
            )

        return ResolvedRunTarget(
            identifier=target.identifier,
            description=target.description,
            kind="python-suite",
            mode=target.mode,
            mutates_panel=False,
            uses_selected_devices=False,
            aqualinkd_args=("-vv",),
            config_overrides=target.config_overrides,
            execution_role=target.execution_role,
            artifact_suffix=target.artifact_suffix,
            case_ids=target.cases,
        )

    def _from_document(
        self,
        source: Path,
        *,
        identifier: str | None = None,
        mode: RunMode = "live-panel",
        description: str | None = None,
        artifact_suffix: str | None = None,
    ) -> ResolvedRunTarget:
        document = load_testcase_document(source)
        if document.mode != "physical-panel":
            raise ValueError(
                f"{source}: execution for testcase mode "
                f"{document.mode!r} is not implemented"
            )
        if isinstance(document, TestcaseDefinition):
            return ResolvedRunTarget(
                identifier=identifier or document.identifier,
                description=description or document.description,
                kind="testcase",
                mode=mode,
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
            identifier=identifier or document.identifier,
            description=description or document.description,
            kind="suite",
            mode=mode,
            mutates_panel=document.mutates_panel,
            uses_selected_devices=document.uses_selected_devices,
            aqualinkd_args=document.config.aqualinkd_args,
            config_overrides=document.config.overrides,
            execution_role=document.config.execution_role,
            artifact_suffix=artifact_suffix,
            source=source,
            schema=document.schema,
            access=document.access,
            testcases=tuple(member.testcase for member in document.members),
        )


_ROOT = Path(__file__).parents[2]
RUN_TARGETS = RunTargetRegistry(
    {
        name: DeclarativeTargetDefinition(
            _ROOT / "testcases" / "suites" / f"{name}.yaml"
        )
        for name in (
            "pda-live-fast",
            "pda-live-awake",
            "pda-live-sleep",
            "pda-live-spa",
            "pda-live-auto-config",
        )
    }
    | {
        f"pda-power-center-{phase}": DeclarativeTargetDefinition(
            _ROOT / "testcases" / "suites" / f"pda-live-{phase}.yaml",
            mode="jandy-power-center",
            description=(
                f"PDA {phase} validation against the Jandy Power Center emulator"
            ),
            artifact_suffix=phase,
        )
        for phase in ("fast", "awake", "spa")
    }
    | {
        "pda-power-center-sleep": DeclarativeTargetDefinition(
            _ROOT / "testcases" / "suites" / "pda-power-center-sleep.yaml",
            mode="jandy-power-center",
            artifact_suffix="sleep",
        ),
        "pda-power-center-service": DeclarativeTargetDefinition(
            _ROOT / "testcases" / "suites" / "pda-power-center-service.yaml",
            mode="jandy-power-center",
            artifact_suffix="service",
        ),
    },
    {
        "pda-live-long": PythonTargetDefinition(
            identifier="pda-live-long",
            description="Composite awake and sleep PDA live-panel validation",
            members=("pda-live-awake", "pda-live-sleep"),
        ),
        "pda-power-center-full": PythonTargetDefinition(
            identifier="pda-power-center-full",
            description=(
                "Complete awake, sleep, and AquaPDA menu validation against "
                "the Jandy Power Center emulator"
            ),
            mode="jandy-power-center",
            members=(
                "pda-power-center-awake",
                "pda-power-center-sleep",
                "pda-power-center-service",
                "aquapda-power-center-menu-walk",
            ),
        ),
        "aquapda-websocket-transport": PythonTargetDefinition(
            identifier="aquapda-websocket-transport",
            description=(
                "AquaPDA WebSocket and RS485 transport regressions for "
                "ballle98/AqualinkD#94 and ballle98/AqualinkD#95"
            ),
            cases=(
                RuntimeCaseId.INITIALIZATION,
                RuntimeCaseId.AQUAPDA_TRANSPORT,
            ),
            config_overrides=(("pda_sleep_mode", "no"),),
            execution_role="awake",
            artifact_suffix="aquapda-websocket",
        ),
        "aquapda-live-panel-menu-walk": PythonTargetDefinition(
            identifier="aquapda-live-panel-menu-walk",
            description="Read-only AquaPDA traversal against a physical panel",
            cases=(
                RuntimeCaseId.INITIALIZATION,
                RuntimeCaseId.AQUAPDA_TRANSPORT,
                RuntimeCaseId.AQUAPDA_MENU_WALK,
            ),
            config_overrides=(("pda_sleep_mode", "no"),),
            execution_role="awake",
            artifact_suffix="aquapda-live-panel-menu-walk",
        ),
        "aquapda-power-center-menu-walk": PythonTargetDefinition(
            identifier="aquapda-power-center-menu-walk",
            description=(
                "Read-only AquaPDA traversal with the Jandy Power Center emulator"
            ),
            mode="jandy-power-center",
            cases=(
                RuntimeCaseId.INITIALIZATION,
                RuntimeCaseId.AQUAPDA_TRANSPORT,
                RuntimeCaseId.AQUAPDA_MENU_WALK,
            ),
            config_overrides=(("pda_sleep_mode", "no"),),
            execution_role="awake",
            artifact_suffix="aquapda-power-center-menu-walk",
        ),
    },
)
