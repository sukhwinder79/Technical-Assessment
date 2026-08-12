"""The agent loop.

A hand-written ReAct-style loop over native Claude tool calling:

    plan → (model turn → execute tool calls → feed results back)* → final report

Everything the assessment asks for structurally lives here: the model decides
which tools to invoke and in what order, tool failures are retried with backoff
and then surfaced back to the model so it can adapt, and every step is recorded
so the run can report per-step success/failure rather than a vague summary.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import AgentError, RefusalError, ToolError, ToolNotFoundError
from ..events import EventBus
from ..llm.base import LLMClient, ToolCall, ToolOutcome
from ..logging_setup import bind_run, get_logger
from ..memory import SessionMemory
from ..retry import RetryPolicy, call_with_retry
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry
from .planner import Plan, Planner
from .prompts import build_system_prompt

log = get_logger(__name__)

#: Tool results are fed back into the model's context on every subsequent turn,
#: so their size compounds. Keep them lean: a small free-tier model may have a
#: 12k tokens-per-minute budget, and one echoed 25-row preview can consume a
#: tenth of it for no benefit.
MAX_TOOL_RESULT_CHARS = 1_400

#: Fields that exist for the *human* report, not for the model's next decision.
#: The console and the JSON report still show them in full — they are only
#: stripped from what goes back into the conversation.
BULKY_RESULT_KEYS = ("preview", "sample_rows", "values", "columns", "checks")


@dataclass(slots=True)
class StepRecord:
    index: int
    tool: str
    arguments: dict[str, Any]
    status: str  # succeeded | failed | rejected
    duration_s: float
    attempts: int = 1
    summary: str = ""
    error: str | None = None
    remediation: str | None = None
    result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RunResult:
    run_id: str
    session_id: str
    instruction: str
    status: str  # completed | partial | failed
    started_at: float
    duration_s: float
    final_message: str = ""
    plan: dict[str, Any] | None = None
    steps: list[StepRecord] = field(default_factory=list)
    artifacts: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)
    iterations: int = 0
    error: str | None = None

    @property
    def succeeded_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.status == "succeeded"]

    @property
    def failed_steps(self) -> list[StepRecord]:
        return [s for s in self.steps if s.status != "succeeded"]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["steps"] = [step.to_dict() for step in self.steps]
        return payload


class AgentExecutor:
    def __init__(
        self,
        *,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        events: EventBus,
        memory: SessionMemory,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.events = events
        self.memory = memory
        self.planner = Planner(llm, registry)

    # ------------------------------------------------------------------ run

    def run(
        self,
        instruction: str,
        *,
        continue_session: bool = False,
        run_id: str | None = None,
    ) -> RunResult:
        # Callers that already have an id (the HTTP API mints one for the URL)
        # pass it in, so logs, events and the /runs/{id} key all agree.
        run_id = run_id or uuid.uuid4().hex[:12]
        bind_run(run_id, session=self.memory.session_id)
        started = time.time()

        result = RunResult(
            run_id=run_id,
            session_id=self.memory.session_id,
            instruction=instruction,
            status="failed",
            started_at=started,
            duration_s=0.0,
        )

        self.events.emit(
            "run_started",
            f"Run {run_id} started",
            run_id=run_id,
            instruction=instruction,
            model=self.llm.model,
            session_id=self.memory.session_id,
        )
        log.info("run.started", instruction=instruction, model=self.llm.model)

        ctx = ToolContext(
            settings=self.settings,
            events=self.events,
            memory=self.memory,
            workspace=Path(self.settings.workspace_dir).resolve(),
        )
        ctx.workspace.mkdir(parents=True, exist_ok=True)

        try:
            plan = self._plan(instruction)
            if plan:
                result.plan = plan.to_dict()

            system = build_system_prompt(
                self.settings,
                self.registry,
                self.memory,
                plan_text=plan.to_prompt() if plan else None,
            )

            if not continue_session:
                self.memory.reset_conversation()
            self.memory.add_message({"role": "user", "content": instruction})

            result.final_message, result.iterations = self._loop(system, ctx, result)
            result.status = self._derive_status(result)

        except RefusalError as exc:
            result.status = "failed"
            result.error = str(exc)
            self.events.emit("run_failed", str(exc), run_id=run_id)
            log.warning("run.refused", error=str(exc))
        except AgentError as exc:
            result.status = "failed"
            result.error = str(exc)
            self.events.emit("run_failed", str(exc), run_id=run_id)
            log.error("run.failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the CLI
            result.status = "failed"
            result.error = f"{type(exc).__name__}: {exc}"
            self.events.emit("run_failed", result.error, run_id=run_id)
            log.exception("run.crashed")
        finally:
            result.duration_s = round(time.time() - started, 2)
            result.usage = dict(getattr(self.llm, "total_usage", {}))
            result.artifacts = self._collect_artifacts()

            self.memory.record_run(
                {
                    "run_id": run_id,
                    "instruction": instruction,
                    "status": result.status,
                    "steps": [
                        {"tool": s.tool, "status": s.status} for s in result.steps
                    ],
                    "artifacts": result.artifacts,
                }
            )
            self.memory.save()

            if result.status != "failed":
                self.events.emit(
                    "run_finished",
                    f"Run {run_id} {result.status} in {result.duration_s}s",
                    run_id=run_id,
                    status=result.status,
                    duration_s=result.duration_s,
                )
            log.info("run.finished", status=result.status, duration_s=result.duration_s)

        return result

    # ----------------------------------------------------------------- plan

    def _plan(self, instruction: str) -> Plan | None:
        if not self.settings.agent_planning:
            return None
        self.events.emit("planning_started", "Planning the work before touching any tool…")
        plan = self.planner.plan(instruction, self.memory)
        if plan is None:
            self.events.emit(
                "plan_ready",
                "Planning was unavailable — proceeding without an explicit plan.",
                plan=None,
            )
            return None
        self.events.emit(
            "plan_ready",
            f"Plan ready: {len(plan.steps)} step(s).",
            plan=plan.to_dict(),
        )
        log.info("plan.ready", steps=len(plan.steps), goal=plan.goal)
        return plan

    # ----------------------------------------------------------------- loop

    def _loop(self, system: str, ctx: ToolContext, result: RunResult) -> tuple[str, int]:
        tools = self.registry.anthropic_schemas()
        last_text = ""
        step_index = 0

        for iteration in range(1, self.settings.agent_max_iterations + 1):
            self.events.emit(
                "llm_turn_started",
                f"Thinking (turn {iteration}/{self.settings.agent_max_iterations})…",
                iteration=iteration,
            )

            response = self.llm.complete(
                system=system,
                messages=self.memory.messages,
                tools=tools,
                on_text=lambda chunk: self.events.emit("assistant_text", chunk, streaming=True),
            )

            self.memory.add_message(self.llm.build_assistant_message(response))
            if response.text:
                last_text = response.text

            # A server-side tool paused the turn: re-send to let it continue.
            if response.stop_reason == "pause_turn":
                continue

            if response.stop_reason == "max_tokens" and not response.tool_calls:
                log.warning("llm.truncated", iteration=iteration)
                return (
                    (last_text or "")
                    + "\n\n[The response was truncated by the token limit. "
                    "Increase AGENT_MAX_TOKENS to see the full report.]",
                    iteration,
                )

            if not response.tool_calls:
                return last_text, iteration

            outcomes: list[ToolOutcome] = []
            for call in response.tool_calls:
                step_index += 1
                record, outcome = self._execute_tool(call, ctx, step_index)
                result.steps.append(record)
                outcomes.append(outcome)

            self.memory.extend_messages(self.llm.build_tool_result_messages(outcomes))

        message = (
            f"Reached the {self.settings.agent_max_iterations}-turn limit before finishing. "
            "Increase AGENT_MAX_ITERATIONS if the task legitimately needs more steps."
        )
        log.warning("loop.exhausted", iterations=self.settings.agent_max_iterations)
        return (last_text + "\n\n" + message).strip(), self.settings.agent_max_iterations

    # ------------------------------------------------------------ tool call

    def _execute_tool(
        self, call: ToolCall, ctx: ToolContext, step_index: int
    ) -> tuple[StepRecord, ToolOutcome]:
        started = time.time()
        attempts = 1

        self.events.emit(
            "tool_started",
            f"{call.name}({self._preview_args(call.arguments)})",
            tool=call.name,
            arguments=call.arguments,
            step=step_index,
        )
        log.info("tool.started", tool=call.name, step=step_index, arguments=call.arguments)

        try:
            tool = self.registry.get(call.name)
        except ToolNotFoundError as exc:
            # Not a crash: tell the model the tool is gone and let it re-plan.
            record = StepRecord(
                index=step_index,
                tool=call.name,
                arguments=call.arguments,
                status="rejected",
                duration_s=round(time.time() - started, 2),
                error=str(exc),
            )
            self.events.emit("tool_failed", str(exc), tool=call.name, step=step_index)
            log.warning("tool.rejected", tool=call.name, error=str(exc))
            return record, ToolOutcome(
                call_id=call.id, content=json.dumps({"ok": False, "error": str(exc)}), is_error=True
            )

        policy = RetryPolicy(
            max_retries=self.registry.retries_for(tool, self.settings.tool_max_retries),
            base_delay=self.settings.tool_retry_base_delay,
        )

        def on_retry(attempt: int, exc: BaseException, delay: float) -> None:
            nonlocal attempts
            attempts = attempt + 1
            self.events.emit(
                "tool_retrying",
                f"{call.name} failed ({exc}). Retry {attempt}/{policy.max_retries} in {delay:.1f}s…",
                tool=call.name,
                attempt=attempt,
                step=step_index,
            )
            log.warning("tool.retrying", tool=call.name, attempt=attempt, error=str(exc))

        try:
            payload = call_with_retry(
                lambda: self.registry.execute(call.name, call.arguments, ctx),
                policy,
                on_retry=on_retry,
            )
        except ToolError as exc:
            record = StepRecord(
                index=step_index,
                tool=call.name,
                arguments=call.arguments,
                status="failed",
                duration_s=round(time.time() - started, 2),
                attempts=attempts,
                error=exc.message,
                remediation=exc.remediation,
            )
            self.events.emit("tool_failed", exc.message, tool=call.name, step=step_index)
            log.error("tool.failed", tool=call.name, error=exc.message)
            return record, ToolOutcome(
                call_id=call.id, content=json.dumps(exc.to_payload()), is_error=True
            )
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            record = StepRecord(
                index=step_index,
                tool=call.name,
                arguments=call.arguments,
                status="failed",
                duration_s=round(time.time() - started, 2),
                attempts=attempts,
                error=message,
            )
            self.events.emit("tool_failed", message, tool=call.name, step=step_index)
            log.exception("tool.crashed", tool=call.name)
            return record, ToolOutcome(
                call_id=call.id,
                content=json.dumps({"ok": False, "error": message}),
                is_error=True,
            )

        duration = round(time.time() - started, 2)
        summary = self._summarise(call.name, payload)
        record = StepRecord(
            index=step_index,
            tool=call.name,
            arguments=call.arguments,
            status="succeeded",
            duration_s=duration,
            attempts=attempts,
            summary=summary,
            result=payload,
        )
        self.events.emit(
            "tool_succeeded",
            f"{call.name} → {summary} ({duration}s)",
            tool=call.name,
            step=step_index,
            duration_s=duration,
            result=payload,
        )
        log.info("tool.succeeded", tool=call.name, duration_s=duration, summary=summary)
        return record, ToolOutcome(call_id=call.id, content=self._serialise(payload))

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _serialise(payload: dict[str, Any]) -> str:
        """Render a tool result for the *model*, staying under the size cap.

        Human-facing detail (row previews, sample rows, per-check expected vs
        actual arrays) is dropped: the model needs to know whether the step
        worked and what the paths are, not to re-read the data it just wrote.
        Everything is still kept in full for the console and the JSON report.

        The result is always valid JSON, at every level of trimming.
        """
        lean = {key: value for key, value in payload.items() if key not in BULKY_RESULT_KEYS}
        text = json.dumps(lean, default=str, ensure_ascii=False)
        if len(text) <= MAX_TOOL_RESULT_CHARS:
            return text

        # Still too big: keep scalars only, which is enough to decide the next step.
        return json.dumps(
            {
                "ok": payload.get("ok", True),
                "_truncated": True,
                **{
                    key: value
                    for key, value in lean.items()
                    if isinstance(value, (str, int, float, bool, type(None))) and len(str(value)) < 400
                },
            },
            default=str,
            ensure_ascii=False,
        )

    @staticmethod
    def _preview_args(arguments: dict[str, Any], *, max_chars: int = 88) -> str:
        """One-line argument preview for the console. Long paths are elided in
        the middle so the filename — the useful part — stays visible."""
        parts = []
        for key, value in list(arguments.items())[:4]:
            rendered = str(value)
            if len(rendered) > 40:
                rendered = f"{rendered[:14]}…{rendered[-24:]}"
            parts.append(f"{key}={rendered}")
        preview = ", ".join(parts)
        return preview if len(preview) <= max_chars else preview[: max_chars - 1] + "…"

    @staticmethod
    def _summarise(tool_name: str, payload: dict[str, Any]) -> str:
        """A one-line human summary for the console and the run report."""
        if not isinstance(payload, dict):
            return str(payload)[:120]

        if url := payload.get("spreadsheet_url"):
            rows = payload.get("data_rows") or payload.get("data_row_count")
            verified = payload.get("verified")
            prefix = "verified " if verified else ""
            return f"{prefix}{rows} rows — {url}" if rows is not None else f"{prefix}{url}"

        if path := payload.get("workbook_path"):
            rows = payload.get("data_rows") or payload.get("data_row_count")
            engine = payload.get("engine")
            bits = [f"{rows} rows" if rows is not None else "", Path(path).name]
            if engine:
                bits.append(f"engine={engine}")
            if payload.get("verified") is True:
                bits.append("verified")
            return " · ".join(b for b in bits if b)

        if path := payload.get("output_path"):
            return f"{payload.get('row_count', '?')} rows → {Path(path).name}"

        if path := payload.get("csv_path"):
            rows = payload.get("row_count")
            return f"{rows} rows → {Path(path).name}" if rows is not None else Path(path).name

        if "excel_available" in payload:
            available = payload["excel_available"]
            version = payload.get("excel_version")
            return f"Excel available (v{version})" if available else f"Excel unavailable: {payload.get('reason')}"

        return "ok"

    def _collect_artifacts(self) -> dict[str, Any]:
        keys = (
            "last_csv_path",
            "last_csv_row_count",
            "last_workbook_path",
            "last_xlsx_path",
            "last_ods_path",
            "last_spreadsheet_id",
            "last_spreadsheet_url",
        )
        return {key: self.memory.recall(key) for key in keys if self.memory.recall(key) is not None}

    @staticmethod
    def _derive_status(result: RunResult) -> str:
        if not result.steps:
            return "completed" if result.final_message else "failed"
        if result.failed_steps and result.succeeded_steps:
            return "partial"
        if result.failed_steps:
            return "failed"
        return "completed"
