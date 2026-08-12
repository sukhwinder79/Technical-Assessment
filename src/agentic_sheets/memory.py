"""Conversation history + working memory, persisted to disk.

Two layers:

  * `messages` — the verbatim Anthropic message list (including tool_use and
    tool_result blocks). Replaying it lets a follow-up prompt continue an
    earlier session with full context.

  * `facts` — a small key/value scratchpad the tools write to
    (`last_csv_path`, `last_workbook_path`, `last_spreadsheet_url`, ...). It is
    injected into the system prompt, so "now also export that to ODS" resolves
    without the model having to re-derive anything.

Sessions are plain JSON files under MEMORY_DIR, one per session id.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

MAX_PERSISTED_MESSAGES = 200


class SessionMemory:
    def __init__(self, session_id: str, directory: Path) -> None:
        self.session_id = session_id
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{session_id}.json"

        self.messages: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}
        self.runs: list[dict[str, Any]] = []
        self.created_at: float = time.time()

        if self.path.exists():
            self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt session file must never block a run.
            return
        self.messages = payload.get("messages", [])
        self.facts = payload.get("facts", {})
        self.runs = payload.get("runs", [])
        self.created_at = payload.get("created_at", time.time())

    def save(self) -> None:
        payload = {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": time.time(),
            "facts": self.facts,
            "runs": self.runs[-20:],
            "messages": self.messages[-MAX_PERSISTED_MESSAGES:],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)

    # ---- conversation ------------------------------------------------------

    def add_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def extend_messages(self, messages: Iterable[dict[str, Any]]) -> None:
        self.messages.extend(messages)

    def reset_conversation(self) -> None:
        self.messages = []

    # ---- working memory ----------------------------------------------------

    def remember(self, key: str, value: Any) -> None:
        self.facts[key] = value

    def recall(self, key: str, default: Any = None) -> Any:
        return self.facts.get(key, default)

    def record_run(self, summary: dict[str, Any]) -> None:
        self.runs.append(summary)

    def facts_as_prompt(self) -> str:
        """Render the scratchpad for injection into the system prompt."""
        if not self.facts:
            return "(nothing yet — this is a fresh session)"
        lines = [f"- {key}: {value}" for key, value in sorted(self.facts.items())]
        return "\n".join(lines)

    def previous_runs_as_prompt(self, limit: int = 3) -> str:
        if not self.runs:
            return "(no earlier runs in this session)"
        lines = []
        for run in self.runs[-limit:]:
            status = run.get("status", "unknown")
            instruction = str(run.get("instruction", ""))[:120]
            lines.append(f'- [{status}] "{instruction}"')
        return "\n".join(lines)


def list_sessions(directory: Path) -> list[str]:
    if not directory.exists():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))
