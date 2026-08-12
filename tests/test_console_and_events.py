"""Event bus and the Rich console renderer.

The renderer is the demo surface, so a crash in it is as bad as a crash in the
agent. These tests drive every event type and every run outcome through it.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from agentic_sheets.agent.executor import RunResult, StepRecord
from agentic_sheets.console import ConsoleRenderer
from agentic_sheets.events import Event, EventBus


# ---- event bus -------------------------------------------------------------


def test_subscribers_receive_events_and_history_is_kept():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(seen.append)

    bus.emit("run_started", "starting", run_id="abc")
    bus.emit("tool_succeeded", "did a thing", tool="t")

    assert [event.type for event in seen] == ["run_started", "tool_succeeded"]
    assert seen[0].data["run_id"] == "abc"
    assert len(bus.history) == 2
    assert seen[0].ts > 0


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    seen: list[Event] = []
    unsubscribe = bus.subscribe(seen.append)

    bus.emit("run_started", "one")
    unsubscribe()
    bus.emit("run_finished", "two")

    assert len(seen) == 1


def test_a_raising_subscriber_does_not_affect_others():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(lambda event: (_ for _ in ()).throw(RuntimeError("bad renderer")))
    bus.subscribe(seen.append)

    bus.emit("run_started", "still delivered")

    assert len(seen) == 1


def test_events_serialise_for_transport():
    bus = EventBus()
    event = bus.emit("tool_started", "generate_employee_csv(...)", tool="generate_employee_csv")
    payload = event.to_dict()
    assert payload["type"] == "tool_started"
    assert payload["data"]["tool"] == "generate_employee_csv"


# ---- console renderer ------------------------------------------------------


@pytest.fixture
def rendered() -> tuple[ConsoleRenderer, io.StringIO]:
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=False, no_color=True)
    return ConsoleRenderer(console), buffer


PLAN = {
    "goal": "Generate data and load it everywhere.",
    "steps": [
        {"id": 1, "title": "Generate CSV", "tool": "generate_employee_csv", "detail": "25 rows", "fallback": "none"},
        {"id": 2, "title": "Import to Excel", "tool": "excel_import_csv", "detail": "COM", "fallback": "convert_spreadsheet"},
    ],
    "risks": ["Excel may be missing."],
}


def test_every_event_type_renders_without_raising(rendered):
    renderer, buffer = rendered
    bus = EventBus()
    bus.subscribe(renderer)

    bus.emit("run_started", "go", run_id="abc123", instruction="Do the thing", model="claude-opus-5", session_id="s1")
    bus.emit("planning_started", "Planning…")
    bus.emit("plan_ready", "Plan ready", plan=PLAN)
    bus.emit("llm_turn_started", "Thinking (turn 1/25)…", iteration=1)
    bus.emit("assistant_text", "I'll start with the CSV. ")
    bus.emit("tool_started", "generate_employee_csv(row_count=25)", tool="generate_employee_csv")
    bus.emit("tool_retrying", "excel_import_csv failed. Retry 1/2 in 1.0s…", tool="excel_import_csv")
    bus.emit("tool_succeeded", "25 rows → employees.csv", tool="generate_employee_csv")
    bus.emit("tool_failed", "credentials missing", tool="google_sheets_import")
    bus.emit("run_finished", "done", status="partial")

    output = buffer.getvalue()
    assert "Do the thing" in output
    assert "Generate CSV" in output           # plan table
    assert "convert_spreadsheet" in output    # plan fallback column
    assert "Excel may be missing." in output  # risks
    assert "employees.csv" in output


def test_plan_ready_without_a_plan_is_reported_not_skipped(rendered):
    renderer, buffer = rendered
    renderer(Event(type="plan_ready", message="Planning was unavailable", data={"plan": None}))
    assert "Planning was unavailable" in buffer.getvalue()


def test_narration_can_be_suppressed():
    buffer = io.StringIO()
    console = Console(file=buffer, width=100, force_terminal=False, no_color=True)
    renderer = ConsoleRenderer(console, show_narration=False)
    renderer(Event(type="assistant_text", message="secret narration", data={}))
    assert "secret narration" not in buffer.getvalue()


def _result(status: str) -> RunResult:
    return RunResult(
        run_id="abc123",
        session_id="s1",
        instruction="Do the thing",
        status=status,
        started_at=0.0,
        duration_s=12.5,
        final_message="- Step one — SUCCESS\n- Step two — FAILED",
        steps=[
            StepRecord(1, "generate_employee_csv", {"row_count": 25}, "succeeded", 0.1, summary="25 rows"),
            StepRecord(2, "excel_import_csv", {}, "succeeded", 4.2, attempts=3, summary="25 rows · employees.xlsx"),
            StepRecord(3, "google_sheets_import", {}, "failed", 0.2, error="credentials missing"),
            StepRecord(4, "excel_probe", {}, "rejected", 0.0, error="tool disabled"),
        ],
        artifacts={"last_csv_path": "C:/work/employees.csv"},
        usage={"input_tokens": 1000, "output_tokens": 200},
        iterations=4,
    )


@pytest.mark.parametrize("status", ["completed", "partial", "failed"])
def test_result_renders_for_every_status(rendered, status):
    renderer, buffer = rendered
    renderer.render_result(_result(status))
    output = buffer.getvalue()

    assert status.upper() in output
    assert "SUCCESS" in output and "FAILED" in output and "SKIPPED" in output
    assert "after 3 attempts" in output          # retry count is surfaced
    assert "last_csv_path" in output             # artifacts panel
    assert "2/4 steps ok" in output              # footer tally
    assert "1000 in / 200 out tokens" in output


def test_result_with_no_steps_still_renders(rendered):
    renderer, buffer = rendered
    result = RunResult(
        run_id="x", session_id="s", instruction="hi", status="completed",
        started_at=0.0, duration_s=0.4, final_message="Nothing to do.",
    )
    renderer.render_result(result)
    assert "Nothing to do." in buffer.getvalue()


def test_error_is_rendered_in_its_own_panel(rendered):
    renderer, buffer = rendered
    result = _result("failed")
    result.error = "ConfigurationError: ANTHROPIC_API_KEY is not set"
    renderer.render_result(result)
    assert "ANTHROPIC_API_KEY" in buffer.getvalue()


def test_rich_markup_in_tool_output_is_not_interpreted(rendered):
    """A tool result containing square brackets must not be parsed as markup."""
    renderer, buffer = rendered
    renderer(Event(type="tool_failed", message="bad value [not-a-tag] in row 3", data={}))
    assert "[not-a-tag]" in buffer.getvalue()
