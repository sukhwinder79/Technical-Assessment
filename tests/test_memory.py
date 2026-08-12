"""Session memory: persistence, working facts, prompt rendering."""

from __future__ import annotations

from pathlib import Path

from agentic_sheets.memory import SessionMemory, list_sessions


def test_facts_and_messages_survive_a_reload(tmp_path: Path):
    memory = SessionMemory("demo", tmp_path)
    memory.add_message({"role": "user", "content": "make a csv"})
    memory.remember("last_csv_path", "C:/work/employees.csv")
    memory.save()

    reloaded = SessionMemory("demo", tmp_path)
    assert reloaded.recall("last_csv_path") == "C:/work/employees.csv"
    assert reloaded.messages[0]["content"] == "make a csv"


def test_separate_sessions_do_not_leak_into_each_other(tmp_path: Path):
    first = SessionMemory("a", tmp_path)
    first.remember("last_csv_path", "a.csv")
    first.save()

    second = SessionMemory("b", tmp_path)
    assert second.recall("last_csv_path") is None


def test_reset_conversation_keeps_facts(tmp_path: Path):
    memory = SessionMemory("demo", tmp_path)
    memory.add_message({"role": "user", "content": "hello"})
    memory.remember("last_workbook_path", "book.xlsx")

    memory.reset_conversation()

    assert memory.messages == []
    assert memory.recall("last_workbook_path") == "book.xlsx"


def test_facts_render_for_the_system_prompt(tmp_path: Path):
    memory = SessionMemory("demo", tmp_path)
    assert "fresh session" in memory.facts_as_prompt()

    memory.remember("last_spreadsheet_url", "https://docs.google.com/spreadsheets/d/abc")
    rendered = memory.facts_as_prompt()
    assert "last_spreadsheet_url" in rendered
    assert "abc" in rendered


def test_run_history_renders_with_status(tmp_path: Path):
    memory = SessionMemory("demo", tmp_path)
    assert "no earlier runs" in memory.previous_runs_as_prompt()

    memory.record_run({"status": "completed", "instruction": "Create a CSV and import it"})
    rendered = memory.previous_runs_as_prompt()
    assert "[completed]" in rendered
    assert "Create a CSV" in rendered


def test_history_is_trimmed_so_files_cannot_grow_without_bound(tmp_path: Path):
    memory = SessionMemory("demo", tmp_path)
    for index in range(400):
        memory.add_message({"role": "user", "content": f"msg {index}"})
    memory.save()

    reloaded = SessionMemory("demo", tmp_path)
    assert len(reloaded.messages) <= 200
    assert reloaded.messages[-1]["content"] == "msg 399"  # newest kept


def test_a_corrupt_session_file_does_not_break_startup(tmp_path: Path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    memory = SessionMemory("broken", tmp_path)  # must not raise
    assert memory.messages == []
    assert memory.facts == {}


def test_list_sessions(tmp_path: Path):
    assert list_sessions(tmp_path) == []
    SessionMemory("one", tmp_path).save()
    SessionMemory("two", tmp_path).save()
    assert list_sessions(tmp_path) == ["one", "two"]
