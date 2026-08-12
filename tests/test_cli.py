"""CLI commands, driven through Typer's runner.

Covers the surfaces a reviewer touches first — `doctor`, `tools`, `sessions`,
`--version` — plus `run`'s exit-code contract, which is what makes the command
usable in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentic_sheets.agent.executor import RunResult, StepRecord
from agentic_sheets.cli import app
from agentic_sheets.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Point every path at tmp_path and refresh the settings singleton."""
    # Rich sizes tables from the terminal width, which varies with how pytest
    # was invoked. Pin it so table assertions don't depend on test order.
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("TOOLS_CONFIG", str(Path("config/tools.yaml").resolve()))
    monkeypatch.setenv("GOOGLE_AUTH_MODE", "disabled")
    # Pin the provider so assertions don't depend on which key happens to be
    # present in the developer's real environment.
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    for key in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)

    from agentic_sheets.config import get_settings

    get_settings(refresh=True)
    yield
    get_settings(refresh=True)


def fake_result(status: str) -> RunResult:
    return RunResult(
        run_id="cli123",
        session_id="cli-session",
        instruction="Create an employee CSV",
        status=status,
        started_at=0.0,
        duration_s=1.0,
        final_message="Step report follows.",
        steps=[StepRecord(1, "generate_employee_csv", {}, "succeeded", 0.1, summary="25 rows")],
        artifacts={"last_csv_path": "C:/w/employees.csv"},
        usage={"input_tokens": 10, "output_tokens": 2},
        iterations=2,
    )


class FakeAgent:
    def __init__(self, status="completed") -> None:
        self.status = status
        self.calls: list[tuple[str, bool]] = []

    def run(self, instruction: str, *, continue_session: bool = False) -> RunResult:
        self.calls.append((instruction, continue_session))
        return fake_result(self.status)


# ---- informational commands ------------------------------------------------


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "agentic-sheets" in result.stdout


def test_schemas_shows_the_wire_form_for_an_openai_compatible_provider(monkeypatch):
    """`--schemas` must show what the provider receives, not the Pydantic source.

    The raw schema renders optional ints as a nullable `anyOf`, which is exactly
    what breaks tool calling on smaller models — printing it here would
    misrepresent the request the agent actually makes.
    """
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings(refresh=True)

    result = runner.invoke(app, ["tools", "--schemas"])
    assert result.exit_code == 0
    assert "as sent to" in result.stdout
    assert "wire form" in result.stdout
    assert "anyOf" not in result.stdout
    assert '"title"' not in result.stdout
    # Descriptions and required-ness must survive the simplification.
    assert "Random seed" in result.stdout
    assert "required" in result.stdout


def test_schemas_raw_shows_the_original_pydantic_output(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings(refresh=True)

    result = runner.invoke(app, ["tools", "--schemas", "--raw"])
    assert result.exit_code == 0
    assert "anyOf" in result.stdout          # the nullable union is visible again
    assert "Unmodified Pydantic" in result.stdout


def test_schemas_are_sent_verbatim_to_anthropic(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings(refresh=True)

    result = runner.invoke(app, ["tools", "--schemas"])
    assert result.exit_code == 0
    assert "sent verbatim" in result.stdout
    assert "anyOf" in result.stdout          # Claude handles unions fine


def test_tools_lists_the_workflow_tools():
    result = runner.invoke(app, ["tools"])
    assert result.exit_code == 0
    for expected in ("generate_employee_csv", "excel_import_csv", "google_sheets_import"):
        assert expected in result.stdout


def test_tools_can_print_schemas():
    result = runner.invoke(app, ["tools", "--schemas"])
    assert result.exit_code == 0
    assert "row_count" in result.stdout


def test_doctor_fails_when_the_api_key_is_missing():
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "GROQ_API_KEY" in result.stdout       # names the provider's own variable
    assert "console.groq.com" in result.stdout   # and where to get a free one
    assert "blocking problem" in result.stdout


def test_doctor_passes_once_prerequisites_are_met(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_abcdefgh1234567890")
    from agentic_sheets.config import get_settings

    get_settings(refresh=True)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Ready to run." in result.stdout
    assert "groq" in result.stdout
    assert "free tier" in result.stdout
    assert "llama-3.3-70b-versatile" in result.stdout   # the resolved default model


def test_doctor_reports_the_anthropic_provider_when_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-abcdefgh1234")
    from agentic_sheets.config import get_settings

    get_settings(refresh=True)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "anthropic" in result.stdout
    assert "claude-opus-5" in result.stdout
    assert "effort=" in result.stdout            # Anthropic-only detail


def test_doctor_reports_disabled_google_as_a_warning_not_a_failure(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_abcdefgh1234567890")
    from agentic_sheets.config import get_settings

    get_settings(refresh=True)

    result = runner.invoke(app, ["doctor"])
    assert "GOOGLE_AUTH_MODE=disabled" in result.stdout
    assert result.exit_code == 0


def test_sessions_reports_an_empty_store():
    result = runner.invoke(app, ["sessions"])
    assert result.exit_code == 0
    assert "No sessions yet" in result.stdout


def test_sessions_lists_and_shows_a_stored_session(tmp_path):
    from agentic_sheets.memory import SessionMemory

    memory = SessionMemory("demo", tmp_path / "memory")
    memory.remember("last_csv_path", "C:/w/employees.csv")
    memory.record_run({"status": "completed", "instruction": "make a csv"})
    memory.save()

    listed = runner.invoke(app, ["sessions"])
    assert "demo" in listed.stdout

    shown = runner.invoke(app, ["sessions", "--show", "demo"])
    assert shown.exit_code == 0
    assert "last_csv_path" in shown.stdout


def test_sessions_show_unknown_is_an_error():
    result = runner.invoke(app, ["sessions", "--show", "nope"])
    assert result.exit_code == 1
    assert "No such session" in result.stdout


# ---- run -------------------------------------------------------------------


def test_run_without_an_api_key_exits_2_with_a_clear_message():
    result = runner.invoke(app, ["run", "make a csv"])
    assert result.exit_code == 2
    assert "GROQ_API_KEY" in result.stdout


@pytest.mark.parametrize(
    ("status", "exit_code"), [("completed", 0), ("partial", 1), ("failed", 2)]
)
def test_run_exit_code_reflects_the_outcome(monkeypatch, status, exit_code):
    agent = FakeAgent(status)
    monkeypatch.setattr("agentic_sheets.cli.build_agent", lambda **_kwargs: agent)

    result = runner.invoke(app, ["run", "make a csv"])

    assert result.exit_code == exit_code
    assert status.upper() in result.stdout


def test_run_uses_the_assessment_prompt_by_default(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("agentic_sheets.cli.build_agent", lambda **_kwargs: agent)

    runner.invoke(app, ["run"])

    instruction = agent.calls[0][0]
    assert "Excel" in instruction and "Google Sheets" in instruction


def test_run_forwards_continue_session(monkeypatch):
    agent = FakeAgent()
    monkeypatch.setattr("agentic_sheets.cli.build_agent", lambda **_kwargs: agent)

    runner.invoke(app, ["run", "again", "--session", "demo", "--continue"])

    assert agent.calls[0] == ("again", True)


def test_run_overrides_are_applied_to_settings(monkeypatch):
    captured: dict = {}

    def build(*, settings, events, session_id):
        captured["provider"] = settings.resolved_provider()
        captured["model"] = settings.resolved_llm_model()
        captured["effort"] = settings.agent_effort
        captured["planning"] = settings.agent_planning
        return FakeAgent()

    monkeypatch.setattr("agentic_sheets.cli.build_agent", build)

    runner.invoke(
        app,
        ["run", "x", "--provider", "anthropic", "--model", "claude-sonnet-5", "--effort", "low", "--no-plan"],
    )

    assert captured == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "effort": "low",
        "planning": False,
    }


def test_run_can_switch_provider_to_groq(monkeypatch):
    captured: dict = {}

    def build(*, settings, events, session_id):
        captured["provider"] = settings.resolved_provider()
        captured["model"] = settings.resolved_llm_model()
        captured["base_url"] = settings.resolved_llm_base_url()
        return FakeAgent()

    monkeypatch.setattr("agentic_sheets.cli.build_agent", build)

    runner.invoke(app, ["run", "x", "--provider", "groq"])

    assert captured["provider"] == "groq"
    assert captured["model"] == "llama-3.3-70b-versatile"
    assert captured["base_url"] == "https://api.groq.com/openai/v1"


def test_run_writes_a_machine_readable_report(monkeypatch, tmp_path):
    monkeypatch.setattr("agentic_sheets.cli.build_agent", lambda **_kwargs: FakeAgent())
    out = tmp_path / "reports" / "run.json"

    result = runner.invoke(app, ["run", "x", "--json-out", str(out)])

    assert result.exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["steps"][0]["tool"] == "generate_employee_csv"
    assert payload["artifacts"]["last_csv_path"].endswith("employees.csv")
