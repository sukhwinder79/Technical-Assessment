#!/usr/bin/env python
"""Single entry point for the whole project.

    python main.py                          # open the web UI (spends nothing)
    python main.py run                      # run the assessment workflow
    python main.py run "your instruction"   # run any natural-language task
    python main.py doctor                   # check the environment
    python main.py tools                    # list the agent's toolbox
    python main.py chat                     # interactive, remembers context
    python main.py mcp                      # MCP server over stdio
    python main.py --help                   # everything else

The bare form starts the UI rather than executing the agent, deliberately: an
agent run costs real API tokens, so it should never be what happens by accident
when someone types the obvious command to "see the project". Opening the UI
costs nothing until you press Run.

This exists so the project runs straight from a clone with nothing installed
but `pip install -r requirements.txt` — `src/` is put on the path here, so the
editable install (`pip install -e .`) is a convenience, not a prerequisite.

Everything below simply delegates to `agentic_sheets.cli`; there is no second
implementation to keep in sync.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if SRC.is_dir() and str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

#: Subcommands understood by the CLI. Anything else on the command line is
#: treated as a natural-language instruction, so `python main.py "do X"` works
#: without having to remember to type `run` first.
COMMANDS = {"run", "chat", "tools", "doctor", "sessions", "serve", "mcp"}


def _normalise(argv: list[str]) -> list[str]:
    """Make the bare and shorthand forms behave the way people expect."""
    if not argv:
        # Start the UI, not the agent. Running the agent costs API tokens, so it
        # must be an explicit choice (`run`) rather than the default behaviour of
        # the shortest possible command.
        return ["serve", "--open"]
    first = argv[0]
    if first in COMMANDS or first.startswith("-"):
        return argv
    # A bare instruction is still a run: `python main.py "Create a CSV and ..."`
    return ["run", *argv]


def main() -> None:
    try:
        from agentic_sheets.cli import entrypoint
    except ImportError as exc:  # pragma: no cover - first-run guidance
        print(f"Could not import the agent package: {exc}\n", file=sys.stderr)
        print("Install the dependencies first:", file=sys.stderr)
        print("    pip install -r requirements.txt", file=sys.stderr)
        raise SystemExit(2) from exc

    sys.argv = [sys.argv[0], *_normalise(sys.argv[1:])]
    entrypoint()


if __name__ == "__main__":
    main()
