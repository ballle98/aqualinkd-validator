from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..interfaces import ScenarioContext, ScenarioOutcome
from ..run_targets import RUNTIME_CASES, RuntimeCaseId
from .reporting import ScenarioRecorder
from .restoration import RestorationSession

CaseOperation = Callable[[RuntimeCaseId], Awaitable[None]]
RestoreCallback = Callable[[str], Awaitable[list[str]]]
InitializedCheck = Callable[[], bool]


class RuntimeCaseRunner:
    """Execute the remaining Python-only cases with bounded safety cleanup."""

    def __init__(
        self,
        *,
        suite_name: str,
        case_ids: tuple[RuntimeCaseId, ...],
        report: dict[str, Any],
        recorder: ScenarioRecorder,
        restoration: RestorationSession,
        operation: CaseOperation,
        restore: RestoreCallback,
        initialized: InitializedCheck,
    ) -> None:
        self._suite_name = suite_name
        self._case_ids = case_ids
        self._report = report
        self._recorder = recorder
        self._restoration = restoration
        self._operation = operation
        self._restore = restore
        self._initialized = initialized

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        started = time.monotonic()
        print(f"\n=== Starting {self._suite_name} ===", flush=True)
        await context.timeline.write(
            "scenario_started",
            suite=self._suite_name,
            api_base_url=self._report["api_base_url"],
            api_endpoint_source=self._report["api_endpoint_source"],
        )
        status = "passed"
        reason = "scenario_completed"
        cancelled = False
        case_failures: list[str] = []
        for case_id in self._case_ids:
            case = RUNTIME_CASES[case_id]
            self._restoration.begin_case()
            case_started = time.monotonic()
            case_error: BaseException | None = None
            try:
                await self._run_case(case.name, case_id)
            except asyncio.CancelledError as error:
                case_error = error
                cancelled = True
            except Exception as error:
                case_error = error

            restoration_errors = (
                await self._restore(f"Restore state after {case.name}")
                if case.mutates_panel
                else []
            )
            self._report["cases"].append(
                {
                    "id": case.id.value,
                    "name": case.name,
                    "status": "passed" if case_error is None else "failed",
                    "duration_ms": round(
                        (time.monotonic() - case_started) * 1000,
                        3,
                    ),
                    "error": (
                        self._recorder.format_exception(case_error)
                        if case_error is not None
                        else None
                    ),
                    "restoration": (
                        "failed"
                        if restoration_errors
                        else ("passed" if case.mutates_panel else "not-needed")
                    ),
                }
            )
            if restoration_errors:
                status = "failed"
                reason = "restoration_failed"
                self._report["error"] = "; ".join(restoration_errors)
                break
            if cancelled:
                status = "failed"
                reason = "scenario_cancelled"
                break
            if case_error is not None:
                case_failures.append(case.id.value)
                if case.id == RuntimeCaseId.INITIALIZATION:
                    status = "failed"
                    reason = "initialization_failed"
                    self._report["error"] = self._recorder.format_exception(
                        case_error
                    )
                    break

        if reason == "scenario_completed" and case_failures:
            status = "failed"
            reason = "case_failures"
            self._report["failed_cases"] = case_failures

        final_errors = (
            await self._restore("Restore original equipment state")
            if self._restoration.has_pending_mutations
            else []
        )
        if final_errors:
            status = "failed"
            reason = "restoration_failed"
            self._report["error"] = "; ".join(final_errors)
        elif reason == "restoration_failed":
            reason = "restoration_recovered"
            self._report["restoration"]["status"] = "recovered"

        self._report["safe_to_continue"] = bool(
            self._initialized()
            and not cancelled
            and reason != "restoration_failed"
        )
        self._report["status"] = status
        self._report["reason"] = reason
        self._recorder.write(context.artifact_dir)
        await context.timeline.write(
            "scenario_finished",
            suite=self._suite_name,
            status=status,
            reason=reason,
        )
        self._recorder.progress_finished(
            self._suite_name,
            started,
            passed=status == "passed",
            detail=None if status == "passed" else reason,
        )
        if cancelled:
            raise asyncio.CancelledError
        return ScenarioOutcome(status=status, reason=reason)

    async def _run_case(self, name: str, case_id: RuntimeCaseId) -> None:
        started = self._recorder.progress_started(name)
        try:
            await self._operation(case_id)
        except asyncio.CancelledError:
            self._recorder.progress_finished(
                name,
                started,
                passed=False,
                detail="cancelled",
            )
            raise
        except Exception as error:
            self._recorder.progress_finished(
                name,
                started,
                passed=False,
                detail=self._recorder.format_exception(error),
            )
            raise
        self._recorder.progress_finished(name, started, passed=True)
