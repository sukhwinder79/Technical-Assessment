"""Planner: schema handling, parsing, and graceful degradation."""

from __future__ import annotations

import json

from agentic_sheets.agent.planner import Planner
from agentic_sheets.agent.prompts import PLAN_SCHEMA, build_planner_prompt
from agentic_sheets.tools.registry import build_default_registry

from .conftest import FakeLLM

VALID_PLAN = {
    "goal": "Generate 25 employee records and load them into Excel and Google Sheets.",
    "steps": [
        {
            "id": 1,
            "title": "Generate employee CSV",
            "tool": "generate_employee_csv",
            "detail": "25 rows with the five required columns.",
            "fallback": "none",
        },
        {
            "id": 2,
            "title": "Import into Excel",
            "tool": "excel_import_csv",
            "detail": "Launch Excel, import the CSV, save as employees.xlsx.",
            "fallback": "convert_spreadsheet with target_format=xlsx",
        },
        {
            "id": 3,
            "title": "Upload to Google Sheets",
            "tool": "google_sheets_import",
            "detail": "Create a spreadsheet and write the rows.",
            "fallback": "Report the step as failed with the auth error.",
        },
    ],
    "risks": ["Excel may not be installed.", "Google credentials may be missing."],
}


def test_parses_a_valid_plan(memory):
    llm = FakeLLM([])
    llm.structured_payload = VALID_PLAN

    plan = Planner(llm, build_default_registry()).plan("do the thing", memory)

    assert plan is not None
    assert len(plan.steps) == 3
    assert plan.steps[1].tool == "excel_import_csv"
    assert plan.steps[1].fallback.startswith("convert_spreadsheet")
    assert len(plan.risks) == 2
    assert llm.structured_calls == 1


def test_plan_renders_for_the_system_prompt(memory):
    llm = FakeLLM([])
    llm.structured_payload = VALID_PLAN
    plan = Planner(llm, build_default_registry()).plan("do the thing", memory)

    rendered = plan.to_prompt()
    assert "Goal:" in rendered
    assert "1. Generate employee CSV" in rendered
    assert "Fallback: convert_spreadsheet" in rendered
    assert "Known risks:" in rendered


def test_planning_failure_degrades_to_no_plan(memory):
    class Exploding(FakeLLM):
        def structured(self, **kwargs):
            raise RuntimeError("provider is down")

    planner = Planner(Exploding([]), build_default_registry())
    assert planner.plan("do the thing", memory) is None


def test_empty_step_list_is_treated_as_no_plan(memory):
    llm = FakeLLM([])
    llm.structured_payload = {"goal": "nothing", "steps": [], "risks": []}
    assert Planner(llm, build_default_registry()).plan("x", memory) is None


def test_partial_step_objects_are_repaired_not_rejected(memory):
    llm = FakeLLM([])
    llm.structured_payload = {
        "goal": "g",
        "steps": [{"title": "Only a title"}],  # missing id / tool / detail
        "risks": [],
    }
    plan = Planner(llm, build_default_registry()).plan("x", memory)
    assert plan is not None
    assert plan.steps[0].id == 1
    assert plan.steps[0].tool == "none"


def test_plan_schema_is_a_valid_strict_object_schema():
    assert PLAN_SCHEMA["type"] == "object"
    assert PLAN_SCHEMA["additionalProperties"] is False
    assert set(PLAN_SCHEMA["required"]) == {"goal", "steps", "risks"}
    step = PLAN_SCHEMA["properties"]["steps"]["items"]
    assert step["additionalProperties"] is False
    assert set(step["required"]) == {"id", "title", "tool", "detail", "fallback"}
    json.dumps(PLAN_SCHEMA)  # must be serialisable


def test_planner_prompt_lists_every_enabled_tool(memory):
    registry = build_default_registry()
    prompt = build_planner_prompt("make a csv", registry, memory)
    for tool in registry.enabled_tools():
        assert tool.name in prompt
