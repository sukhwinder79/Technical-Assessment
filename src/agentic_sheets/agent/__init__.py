"""Agent assembly: planner + executor + tools + memory, wired together."""

from __future__ import annotations

import uuid
from pathlib import Path

from ..config import Settings, get_settings
from ..events import EventBus
from ..llm import build_client
from ..logging_setup import configure_logging
from ..memory import SessionMemory
from ..tools.registry import ToolRegistry, build_default_registry
from .executor import AgentExecutor, RunResult, StepRecord
from .planner import Plan, Planner

__all__ = [
    "AgentExecutor",
    "Plan",
    "Planner",
    "RunResult",
    "StepRecord",
    "build_agent",
]


def build_agent(
    *,
    settings: Settings | None = None,
    events: EventBus | None = None,
    session_id: str | None = None,
    registry: ToolRegistry | None = None,
) -> AgentExecutor:
    """Construct a ready-to-run agent.

    Shared by the CLI, the FastAPI server and the test suite so there is exactly
    one wiring path to keep correct.
    """
    settings = settings or get_settings()
    settings.ensure_directories()
    configure_logging(Path(settings.log_dir), settings.log_level, settings.log_json)

    events = events or EventBus()
    registry = registry or build_default_registry(Path(settings.tools_config))
    memory = SessionMemory(
        session_id or f"session-{uuid.uuid4().hex[:8]}",
        Path(settings.memory_dir),
    )

    return AgentExecutor(
        llm=build_client(settings),
        registry=registry,
        settings=settings,
        events=events,
        memory=memory,
    )
