"""Multi-step planning pass.

Before touching any tool the agent produces an explicit, ordered plan with a
declared fallback per step. Two reasons this is worth a separate LLM call:

  * The plan is shown to the user up front, so a long autonomous run is legible
    while it happens rather than only in hindsight.
  * The plan is injected into the executor's system prompt, which measurably
    reduces mid-run drift on multi-destination tasks.

Planning is best-effort: if the call fails the executor still runs, just
without a plan. A planning outage must never block the actual work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..llm.base import LLMClient
from ..logging_setup import get_logger
from ..memory import SessionMemory
from ..tools.registry import ToolRegistry
from .prompts import PLAN_SCHEMA, PLANNER_SYSTEM, build_planner_prompt

log = get_logger(__name__)


@dataclass(slots=True)
class PlanStep:
    id: int
    title: str
    tool: str
    detail: str
    fallback: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "tool": self.tool,
            "detail": self.detail,
            "fallback": self.fallback,
        }


@dataclass(slots=True)
class Plan:
    goal: str
    steps: list[PlanStep] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "steps": [step.to_dict() for step in self.steps],
            "risks": self.risks,
        }

    def to_prompt(self) -> str:
        lines = [f"Goal: {self.goal}", "", "Steps:"]
        for step in self.steps:
            lines.append(f"  {step.id}. {step.title}  [tool: {step.tool}]")
            lines.append(f"     {step.detail}")
            if step.fallback and step.fallback.lower() != "none":
                lines.append(f"     Fallback: {step.fallback}")
        if self.risks:
            lines.append("")
            lines.append("Known risks:")
            lines.extend(f"  - {risk}" for risk in self.risks)
        return "\n".join(lines)


class Planner:
    def __init__(self, llm: LLMClient, registry: ToolRegistry) -> None:
        self.llm = llm
        self.registry = registry

    def plan(self, instruction: str, memory: SessionMemory) -> Plan | None:
        prompt = build_planner_prompt(instruction, self.registry, memory)
        try:
            payload = self.llm.structured(
                system=PLANNER_SYSTEM,
                prompt=prompt,
                schema=PLAN_SCHEMA,
                max_tokens=4096,
            )
        except Exception as exc:  # noqa: BLE001 - planning is advisory
            log.warning("planner.failed", error=str(exc))
            return None

        return self._parse(payload)

    @staticmethod
    def _parse(payload: dict[str, Any]) -> Plan | None:
        if not isinstance(payload, dict):
            return None
        steps: list[PlanStep] = []
        for index, raw in enumerate(payload.get("steps") or [], start=1):
            if not isinstance(raw, dict):
                continue
            steps.append(
                PlanStep(
                    id=int(raw.get("id") or index),
                    title=str(raw.get("title") or f"Step {index}"),
                    tool=str(raw.get("tool") or "none"),
                    detail=str(raw.get("detail") or ""),
                    fallback=str(raw.get("fallback") or "none"),
                )
            )
        if not steps:
            return None
        risks = [str(r) for r in (payload.get("risks") or []) if str(r).strip()]
        return Plan(goal=str(payload.get("goal") or "").strip(), steps=steps, risks=risks)
