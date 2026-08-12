"""Registry: discovery, YAML configuration, dispatch, and schema generation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from agentic_sheets.errors import ConfigurationError, ToolNotFoundError
from agentic_sheets.tools.base import Tool, ToolContext
from agentic_sheets.tools.registry import ToolRegistry, build_default_registry


class EchoArgs(BaseModel):
    text: str = Field(description="Text to echo back.")
    times: int = Field(default=1, ge=1, le=5)


class EchoTool(Tool):
    name = "echo"
    description = "Echo the given text back. First line only in the catalogue."
    args_model = EchoArgs
    tags = ("test",)

    def run(self, args: EchoArgs, ctx: ToolContext) -> dict[str, Any]:
        return {"ok": True, "echo": args.text * args.times}


def test_default_registry_exposes_the_full_workflow():
    registry = build_default_registry()
    names = {tool.name for tool in registry.all_tools()}
    assert {
        "generate_employee_csv",
        "convert_spreadsheet",
        "excel_import_csv",
        "excel_verify_workbook",
        "google_sheets_import",
        "google_sheets_verify",
    } <= names


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ConfigurationError):
        registry.register(EchoTool())


def test_schema_is_generated_from_the_pydantic_model():
    schema = EchoTool().to_anthropic_schema()
    assert schema["name"] == "echo"
    assert schema["input_schema"]["properties"]["text"]["description"]
    assert schema["input_schema"]["required"] == ["text"]


def test_every_builtin_tool_produces_a_valid_object_schema():
    for tool in build_default_registry().all_tools():
        schema = tool.to_anthropic_schema()
        assert schema["name"] and schema["description"]
        assert schema["input_schema"]["type"] == "object"


def test_dispatch_validates_arguments_then_runs(ctx: ToolContext):
    registry = ToolRegistry([EchoTool()])
    assert registry.execute("echo", {"text": "hi", "times": 2}, ctx)["echo"] == "hihi"


def test_dispatch_rejects_bad_arguments(ctx: ToolContext):
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(Exception):  # pydantic ValidationError
        registry.execute("echo", {"times": 99}, ctx)


def test_unknown_tool_names_the_alternatives():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ToolNotFoundError) as exc:
        registry.get("nope")
    assert "echo" in str(exc.value)


# ---- YAML configuration ----------------------------------------------------


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_config_can_disable_a_tool(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """
tools:
  generate_employee_csv:
    enabled: true
  excel_import_csv:
    enabled: false
""",
    )
    registry = build_default_registry(config)
    enabled = {tool.name for tool in registry.enabled_tools()}
    assert "generate_employee_csv" in enabled
    assert "excel_import_csv" not in enabled
    # Disabled tools are hidden from the model's toolbox entirely.
    assert "excel_import_csv" not in {s["name"] for s in registry.anthropic_schemas()}


def test_calling_a_disabled_tool_explains_what_to_do_instead(tmp_path: Path):
    config = _write_config(tmp_path, "tools:\n  excel_import_csv:\n    enabled: false\n")
    registry = build_default_registry(config)
    with pytest.raises(ToolNotFoundError) as exc:
        registry.get("excel_import_csv")
    assert "disabled" in str(exc.value).lower()


def test_config_overrides_retries_and_appends_description(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """
defaults:
  max_retries: 4
tools:
  generate_employee_csv:
    enabled: true
    description_suffix: Call this first.
  google_sheets_import:
    enabled: true
    max_retries: 6
""",
    )
    registry = build_default_registry(config)

    generate = registry.get("generate_employee_csv")
    assert generate.effective_description.endswith("Call this first.")
    assert registry.retries_for(generate, fallback=0) == 4          # from defaults
    assert registry.retries_for(registry.get("google_sheets_import"), fallback=0) == 6  # per-tool


def test_unknown_tool_in_config_is_ignored(tmp_path: Path):
    config = _write_config(tmp_path, "tools:\n  does_not_exist:\n    enabled: false\n")
    registry = build_default_registry(config)  # must not raise
    assert registry.enabled_tools()


def test_missing_config_file_keeps_defaults(tmp_path: Path):
    registry = build_default_registry(tmp_path / "absent.yaml")
    assert len(registry.enabled_tools()) == len(registry.all_tools())


def test_malformed_config_raises_a_clear_error(tmp_path: Path):
    config = _write_config(tmp_path, "tools: [this: is: not: valid\n")
    with pytest.raises(ConfigurationError):
        build_default_registry(config)
