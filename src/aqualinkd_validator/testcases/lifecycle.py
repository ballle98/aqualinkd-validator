from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..engine.reporting import ScenarioRecorder
from ..engine.restoration import RestorationSession
from ..interfaces import ScenarioContext, ScenarioOutcome
from .executor import TestcaseExecutor, TestcaseKeywords
from .model import TestcaseDefinition

KeywordsFactory = Callable[[str], TestcaseKeywords]
RestoreCallback = Callable[[ScenarioContext, str], Awaitable[list[str]]]
InitializedCheck = Callable[[], bool]


class DeclarativeScenarioRunner:
    """Run validated YAML testcases with common reporting and cleanup."""

    def __init__(
        self,
        *,
        suite_name: str,
        testcases: tuple[TestcaseDefinition, ...],
        report: dict[str, Any],
        recorder: ScenarioRecorder,
        restoration: RestorationSession,
        keywords: KeywordsFactory,
        restore: RestoreCallback,
        initialized: InitializedCheck,
    ) -> None:
        self._suite_name = suite_name
        self._testcases = testcases
        self._report = report
        self._recorder = recorder
        self._restoration = restoration
        self._keywords = keywords
        self._restore = restore
        self._initialized = initialized

    async def run(self, context: ScenarioContext) -> ScenarioOutcome:
        suite_started = time.monotonic()
        if len(self._testcases) > 1:
            print(f"\n=== Starting {self._suite_name} ===", flush=True)
        outcome = ScenarioOutcome(status="passed", reason="scenario_completed")
        for testcase in self._testcases:
            outcome = await self._run_testcase(context, testcase)
            if outcome.status != "passed":
                break
        if len(self._testcases) > 1:
            self._report["testcases"] = [
                testcase.identifier for testcase in self._testcases
            ]
            self._report.pop("testcase", None)
            self._recorder.write(context.artifacts)
            self._recorder.progress_finished(
                self._suite_name,
                suite_started,
                passed=outcome.status == "passed",
                detail=None if outcome.status == "passed" else outcome.reason,
            )
        return outcome

    async def _run_testcase(
        self,
        context: ScenarioContext,
        testcase: TestcaseDefinition,
    ) -> ScenarioOutcome:
        started = time.monotonic()
        print(f"\n=== Starting {testcase.identifier} ===", flush=True)
        await context.timeline.write(
            "scenario_started",
            suite=self._suite_name,
            testcase=testcase.identifier,
            api_base_url=self._report["api_base_url"],
            api_endpoint_source=self._report["api_endpoint_source"],
        )
        status = "passed"
        reason = "scenario_completed"
        cancelled = False
        error: BaseException | None = None
        case_started = time.monotonic()
        try:
            execution = await TestcaseExecutor(
                self._keywords(testcase.identifier)
            ).execute(testcase)
            execution_report = {
                "id": execution.identifier,
                "duration_ms": round(execution.duration_seconds * 1000, 3),
                "steps": [
                    {
                        "section": step.section,
                        "index": step.index,
                        "keyword": step.keyword,
                        "duration_ms": round(step.duration_seconds * 1000, 3),
                    }
                    for step in execution.steps
                ],
            }
            self._report.setdefault("testcase_executions", []).append(
                execution_report
            )
            self._report["testcase_execution"] = execution_report
        except asyncio.CancelledError as caught:
            error = caught
            cancelled = True
            status = "failed"
            reason = "scenario_cancelled"
        except BaseException as caught:
            error = caught
            status = "failed"
            reason = "testcase_failed"

        restoration_errors: list[str] = []
        if self._restoration.has_pending_mutations:
            restoration_errors = await self._restore(
                context,
                "Final safety restoration",
            )
        if restoration_errors:
            status = "failed"
            reason = "restoration_failed"
            self._report["error"] = "; ".join(restoration_errors)
        elif error is not None:
            self._report["error"] = self._recorder.format_exception(error)

        self._report["cases"].append(
            {
                "id": testcase.identifier,
                "name": testcase.description,
                "status": status,
                "duration_ms": round((time.monotonic() - case_started) * 1000, 3),
                "error": (
                    self._recorder.format_exception(error) if error else None
                ),
                "restoration": self._report["restoration"]["status"],
            }
        )
        self._report["safe_to_continue"] = bool(
            self._initialized()
            and not cancelled
            and reason != "restoration_failed"
        )
        self._report["status"] = status
        self._report["reason"] = reason
        self._report["testcase"] = testcase.identifier
        self._recorder.write(context.artifacts)
        await context.timeline.write(
            "scenario_finished",
            suite=self._suite_name,
            testcase=testcase.identifier,
            status=status,
            reason=reason,
        )
        self._recorder.progress_finished(
            testcase.identifier,
            started,
            passed=status == "passed",
            detail=None if status == "passed" else reason,
        )
        if cancelled:
            raise asyncio.CancelledError
        return ScenarioOutcome(status=status, reason=reason)
