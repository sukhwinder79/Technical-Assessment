"""The root `main.py` entry point.

It is the first thing a reviewer runs, so its argument handling is worth
pinning down — especially the two shorthands (`python main.py` and
`python main.py "some instruction"`) that skip the `run` subcommand.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


@pytest.fixture(scope="module")
def main_module():
    spec = importlib.util.spec_from_file_location("project_main", MAIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_main_py_exists_at_the_project_root():
    assert MAIN.is_file()


# ---- argument normalisation ------------------------------------------------


def test_no_arguments_opens_the_ui_and_never_spends_tokens(main_module):
    """The shortest command must not start a paid agent run by accident."""
    assert main_module._normalise([]) == ["serve", "--open"]
    assert "run" not in main_module._normalise([])


def test_a_bare_instruction_is_treated_as_a_run(main_module):
    assert main_module._normalise(["Create a CSV and import it"]) == [
        "run",
        "Create a CSV and import it",
    ]


@pytest.mark.parametrize("command", ["run", "chat", "tools", "doctor", "sessions", "serve", "mcp"])
def test_subcommands_pass_through_untouched(main_module, command):
    assert main_module._normalise([command]) == [command]
    assert main_module._normalise([command, "--help"]) == [command, "--help"]


def test_global_flags_pass_through(main_module):
    assert main_module._normalise(["--version"]) == ["--version"]


def test_an_instruction_keeps_its_trailing_options(main_module):
    assert main_module._normalise(["do the thing", "--no-plan"]) == [
        "run",
        "do the thing",
        "--no-plan",
    ]


def test_the_command_list_matches_the_actual_cli(main_module):
    """A new CLI command must be added to COMMANDS, or it would be swallowed
    and mistakenly re-interpreted as a natural-language instruction."""
    from agentic_sheets.cli import app

    registered = {command.name or command.callback.__name__ for command in app.registered_commands}
    assert registered == main_module.COMMANDS


# ---- it actually runs ------------------------------------------------------


def test_main_py_runs_without_the_package_installed(tmp_path):
    """`python main.py --help` must work from a bare clone.

    PYTHONPATH is deliberately cleared: main.py is responsible for putting
    `src/` on the path itself, so a reviewer needs only
    `pip install -r requirements.txt`.
    """
    result = subprocess.run(
        [sys.executable, str(MAIN), "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "", "SYSTEMROOT": "C:\\Windows", "PYTHONPATH": ""},
    )
    assert result.returncode == 0, result.stderr
    assert "Autonomous" in result.stdout or "Usage" in result.stdout


def test_main_py_tools_lists_the_toolbox(tmp_path):
    result = subprocess.run(
        [sys.executable, str(MAIN), "tools"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "generate_employee_csv" in result.stdout
