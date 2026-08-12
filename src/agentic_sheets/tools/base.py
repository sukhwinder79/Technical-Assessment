"""The tool contract every capability implements.

A tool is: a name, a description the model reads to decide *when* to call it,
a Pydantic model describing its arguments (which doubles as the JSON Schema
sent to the API), and a `run` method. Nothing else. That keeps tools trivially
unit-testable without an LLM in the loop, and makes the same objects reusable
from the CLI, the FastAPI server and the MCP server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Type

from pydantic import BaseModel

from ..config import Settings
from ..events import EventBus
from ..memory import SessionMemory


@dataclass(slots=True)
class ToolContext:
    """Everything a tool is allowed to touch. Passed in, never imported."""

    settings: Settings
    events: EventBus
    memory: SessionMemory
    workspace: Path

    def resolve(self, path: str | Path) -> Path:
        """Resolve a model-supplied path against the workspace.

        Relative paths land inside WORKSPACE_DIR; absolute paths are honoured
        so a user can point the agent at an existing file anywhere on disk.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return candidate.resolve()


class Tool(ABC):
    """Base class for all agent tools."""

    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[Type[BaseModel]]

    #: Free-form labels used by the registry / MCP server for filtering.
    tags: ClassVar[tuple[str, ...]] = ()
    #: Tools that touch the outside world get their own retry budget.
    max_retries: ClassVar[int | None] = None

    def __init__(self) -> None:
        # Applied by the registry from config/tools.yaml.
        self.enabled: bool = True
        self.description_override: str | None = None
        self.max_retries_override: int | None = None

    # ---- schema ------------------------------------------------------------

    @property
    def effective_description(self) -> str:
        return self.description_override or self.description

    @property
    def summary(self) -> str:
        """First sentence of the description — for compact listings.

        The full description is what the model reads to decide *when* to call a
        tool, so it is deliberately several sentences long. Console tables and
        the system prompt's tool index want one line.
        """
        text = " ".join(self.description.split())
        head, _, _ = text.partition(". ")
        return head if head.endswith(".") or not _ else f"{head}."

    def input_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        # `title`/`$defs` are fine for the API but the top-level title is noise.
        schema.pop("title", None)
        return schema

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.effective_description,
            "input_schema": self.input_schema(),
        }

    def parse_args(self, raw: dict[str, Any]) -> BaseModel:
        return self.args_model.model_validate(raw or {})

    # ---- execution ---------------------------------------------------------

    @abstractmethod
    def run(self, args: Any, ctx: ToolContext) -> dict[str, Any]:
        """Execute the tool. Return a JSON-serialisable dict.

        Raise `ToolError` for expected failures — the executor converts those
        into an `is_error` tool_result so the model can adapt.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Tool {self.name} enabled={self.enabled}>"
