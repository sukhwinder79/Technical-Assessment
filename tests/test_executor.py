"""The agent loop: tool dispatch, retries, failure handling, memory, reporting.

These are the tests that prove the *agent* works, not just the tools. The LLM
is a scripted fake, so the loop's control flow is exercised deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from agentic_sheets.agent.executor import AgentExecutor
from agentic_sheets.errors import ToolError
from agentic_sheets.events import EventBus
from agentic_sheets.llm.base import LLMResponse
from agentic_sheets.tools.base import Tool, ToolContext
from agentic_sheets.tools.registry import ToolRegistry

from .conftest import FakeLLM, final_turn, tool_turn


# ---- fixtures --------------------------------------------------------------


class NoArgs(BaseModel):
    pass


class OkTool(Tool):
    name = "ok_tool"
    description = "Always succeeds."
    args_model = NoArgs

    def run(self, args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        ctx.memory.remember("last_csv_path", str(ctx.workspace / "made.csv"))
        return {"ok": True, "csv_path": str(ctx.workspace / "made.csv"), "row_count": 25}


class FlakyTool(Tool):
    name = "flaky_tool"
    description = "Fails twice with a retryable error, then succeeds."
    args_model = NoArgs

    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def run(self, args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        self.attempts += 1
        if self.attempts < 3:
            raise ToolError("Excel is busy", retryable=True)
        return {"ok": True, "workbook_path": "book.xlsx", "data_rows": 25}


class BrokenTool(Tool):
    name = "broken_tool"
    description = "Always fails, not retryable."
    args_model = NoArgs

    def run(self, args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        raise ToolError(
            "Google credentials missing",
            retryable=False,
            remediation="Add credentials/service_account.json",
        )


class CrashTool(Tool):
    name = "crash_tool"
    description = "Raises an unexpected exception."
    args_model = NoArgs

    def run(self, args: NoArgs, ctx: ToolContext) -> dict[str, Any]:
        raise ZeroDivisionError("boom")


def make_executor(script, tools, settings, memory, events=None) -> tuple[AgentExecutor, FakeLLM]:
    llm = FakeLLM(script)
    registry = ToolRegistry(tools)
    executor = AgentExecutor(
        llm=llm,
        registry=registry,
        settings=settings,
        events=events or EventBus(),
        memory=memory,
    )
    return executor, llm


# ---- happy path ------------------------------------------------------------


def test_runs_tools_then_returns_the_final_report(settings, memory):
    executor, llm = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("All steps completed.")],
        [OkTool()],
        settings,
        memory,
    )

    result = executor.run("make a csv")

    assert result.status == "completed"
    assert result.final_message == "All steps completed."
    assert [step.tool for step in result.steps] == ["ok_tool"]
    assert result.steps[0].status == "succeeded"
    assert result.iterations == 2


def test_multiple_tool_calls_in_one_turn_all_execute(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {}), ("flaky_tool", {})), final_turn("done")],
        [OkTool(), FlakyTool()],
        settings,
        memory,
    )
    result = executor.run("do two things")
    assert len(result.steps) == 2
    assert {step.tool for step in result.steps} == {"ok_tool", "flaky_tool"}


def test_the_model_receives_the_tool_result_before_its_next_turn(settings, memory):
    seen: dict[str, Any] = {}

    def inspect(messages) -> LLMResponse:
        last = messages[-1]
        seen["role"] = last["role"]
        seen["payload"] = json.loads(last["content"][0]["content"])
        return final_turn("acknowledged")

    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), inspect], [OkTool()], settings, memory
    )
    executor.run("go")

    assert seen["role"] == "user"
    assert seen["payload"]["row_count"] == 25


def test_summary_is_human_readable(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("ok")], [OkTool()], settings, memory
    )
    result = executor.run("go")
    assert "25 rows" in result.steps[0].summary
    assert "made.csv" in result.steps[0].summary


# ---- retries ---------------------------------------------------------------


def test_a_retryable_failure_is_retried_and_recorded(settings, memory):
    settings.tool_max_retries = 3
    flaky = FlakyTool()
    executor, _ = make_executor(
        [tool_turn(("flaky_tool", {})), final_turn("recovered")], [flaky], settings, memory
    )

    result = executor.run("import to excel")

    assert flaky.attempts == 3
    assert result.status == "completed"
    assert result.steps[0].status == "succeeded"
    assert result.steps[0].attempts == 3


def test_retry_events_are_emitted_for_progress_reporting(settings, memory):
    settings.tool_max_retries = 3
    events = EventBus()
    captured: list[str] = []
    events.subscribe(lambda event: captured.append(event.type))

    executor, _ = make_executor(
        [tool_turn(("flaky_tool", {})), final_turn("ok")], [FlakyTool()], settings, memory, events
    )
    executor.run("go")

    assert captured.count("tool_retrying") == 2
    assert "tool_succeeded" in captured


# ---- failures --------------------------------------------------------------


def test_a_tool_failure_is_reported_to_the_model_not_raised(settings, memory):
    observed: dict[str, Any] = {}

    def inspect(messages) -> LLMResponse:
        block = messages[-1]["content"][0]
        observed["is_error"] = block.get("is_error")
        observed["payload"] = json.loads(block["content"])
        return final_turn("Google Sheets step FAILED: credentials missing.")

    executor, _ = make_executor(
        [tool_turn(("broken_tool", {})), inspect], [BrokenTool()], settings, memory
    )
    result = executor.run("upload to sheets")

    assert observed["is_error"] is True
    assert "remediation" in observed["payload"]
    assert result.status == "failed"
    assert result.steps[0].status == "failed"
    assert result.steps[0].remediation == "Add credentials/service_account.json"


def test_a_non_retryable_error_is_not_retried(settings, memory):
    settings.tool_max_retries = 5
    attempts = {"n": 0}

    class Counting(BrokenTool):
        def run(self, args, ctx):
            attempts["n"] += 1
            return super().run(args, ctx)

    executor, _ = make_executor(
        [tool_turn(("broken_tool", {})), final_turn("failed")], [Counting()], settings, memory
    )
    executor.run("go")
    assert attempts["n"] == 1


def test_partial_success_is_reported_as_partial(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {}), ("broken_tool", {})), final_turn("Excel ok, Sheets failed.")],
        [OkTool(), BrokenTool()],
        settings,
        memory,
    )
    result = executor.run("both destinations")

    assert result.status == "partial"
    assert len(result.succeeded_steps) == 1
    assert len(result.failed_steps) == 1


def test_an_unexpected_exception_is_contained(settings, memory):
    settings.tool_max_retries = 0
    executor, _ = make_executor(
        [tool_turn(("crash_tool", {})), final_turn("that tool crashed")],
        [CrashTool()],
        settings,
        memory,
    )
    result = executor.run("go")

    assert result.steps[0].status == "failed"
    assert "ZeroDivisionError" in result.steps[0].error
    assert result.final_message == "that tool crashed"


def test_hallucinated_tool_name_is_rejected_without_crashing(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("no_such_tool", {})), final_turn("I used the wrong tool name.")],
        [OkTool()],
        settings,
        memory,
    )
    result = executor.run("go")

    assert result.steps[0].status == "rejected"
    assert "no_such_tool" in result.steps[0].error


# ---- loop control ----------------------------------------------------------


def test_iteration_cap_stops_a_runaway_loop(settings, memory):
    settings.agent_max_iterations = 3
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {}))] * 10, [OkTool()], settings, memory
    )
    result = executor.run("loop forever")

    assert result.iterations == 3
    assert "turn limit" in result.final_message


def test_pause_turn_resumes_without_consuming_a_tool_step(settings, memory):
    paused = LLMResponse(text="", stop_reason="pause_turn", raw_content=[{"type": "text", "text": ""}])
    executor, _ = make_executor([paused, final_turn("resumed")], [OkTool()], settings, memory)

    result = executor.run("go")

    assert result.final_message == "resumed"
    assert result.steps == []


def test_truncated_response_is_flagged(settings, memory):
    truncated = LLMResponse(
        text="partial report", stop_reason="max_tokens", raw_content=[{"type": "text", "text": "partial report"}]
    )
    executor, _ = make_executor([truncated], [OkTool()], settings, memory)
    result = executor.run("go")
    assert "truncated" in result.final_message.lower()


# ---- memory & artifacts ----------------------------------------------------


def test_a_caller_supplied_run_id_is_used_everywhere(settings, memory):
    """The HTTP API mints the id for its URL, so the run must adopt it."""
    events = EventBus()
    seen: list[str] = []
    events.subscribe(lambda event: seen.append(event.data.get("run_id", "")))

    executor, _ = make_executor([final_turn("done")], [OkTool()], settings, memory, events)
    result = executor.run("go", run_id="caller-supplied")

    assert result.run_id == "caller-supplied"
    assert "caller-supplied" in seen


def test_artifacts_are_collected_from_working_memory(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("done")], [OkTool()], settings, memory
    )
    result = executor.run("go")
    assert result.artifacts["last_csv_path"].endswith("made.csv")


def test_a_run_is_persisted_so_the_next_run_can_see_it(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("done")], [OkTool()], settings, memory
    )
    executor.run("first instruction")

    from agentic_sheets.memory import SessionMemory

    reloaded = SessionMemory(memory.session_id, Path(settings.memory_dir))
    assert reloaded.runs[-1]["instruction"] == "first instruction"
    assert reloaded.recall("last_csv_path")


def test_continue_session_keeps_the_earlier_conversation(settings, memory):
    executor, llm = make_executor(
        [final_turn("first"), final_turn("second")], [OkTool()], settings, memory
    )
    executor.run("one")
    executor.run("two", continue_session=True)

    roles = [message["role"] for message in memory.messages]
    assert roles.count("user") == 2  # both instructions retained


def test_a_fresh_run_resets_the_conversation(settings, memory):
    executor, _ = make_executor([final_turn("a"), final_turn("b")], [OkTool()], settings, memory)
    executor.run("one")
    executor.run("two")  # continue_session defaults to False
    assert [m["role"] for m in memory.messages].count("user") == 1


# ---- events ----------------------------------------------------------------


def test_lifecycle_events_are_emitted_in_order(settings, memory):
    events = EventBus()
    captured: list[str] = []
    events.subscribe(lambda event: captured.append(event.type))

    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("done")], [OkTool()], settings, memory, events
    )
    executor.run("go")

    assert captured[0] == "run_started"
    assert captured[-1] == "run_finished"
    assert captured.index("tool_started") < captured.index("tool_succeeded")


def test_a_broken_subscriber_cannot_break_the_run(settings, memory):
    events = EventBus()
    events.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("renderer bug")))

    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("done")], [OkTool()], settings, memory, events
    )
    assert executor.run("go").status == "completed"


# ---- serialisation ---------------------------------------------------------


def test_large_tool_results_are_trimmed_but_stay_valid_json():
    payload = {"ok": True, "csv_path": "x.csv", "preview": [["cell"] * 50] * 400}
    serialised = AgentExecutor._serialise(payload)
    parsed = json.loads(serialised)  # must not raise
    assert parsed["ok"] is True
    assert "preview" not in parsed


def test_small_tool_results_pass_through_untouched():
    payload = {"ok": True, "row_count": 25}
    assert json.loads(AgentExecutor._serialise(payload)) == payload


def test_run_result_is_json_serialisable(settings, memory):
    executor, _ = make_executor(
        [tool_turn(("ok_tool", {})), final_turn("done")], [OkTool()], settings, memory
    )
    result = executor.run("go")
    json.dumps(result.to_dict(), default=str)  # must not raise
