"""System and planning prompts.

Written for current Claude models: plain declarative instructions, no shouting,
no step-by-step choreography for judgement the model already has. What the
model genuinely cannot know — the host OS, whether Excel is automatable, which
Google auth mode is configured, what earlier turns produced — is supplied as
context rather than as rules.
"""

from __future__ import annotations

import platform
import sys
from pathlib import Path

from ..config import Settings
from ..memory import SessionMemory
from ..tools.registry import ToolRegistry

AGENT_ROLE = """\
You are an autonomous spreadsheet operations agent. You complete a user's request \
end to end by calling tools — you do not describe what you would do, and you do not \
hand steps back to the user to perform manually.

Operating rules:

- Work autonomously. The user issued a single command and is not available to answer \
  follow-up questions. For routine choices (file names, row counts above any stated \
  minimum, sheet titles) pick a sensible value and note it. Only stop and ask if \
  proceeding would be destructive or genuinely ambiguous in a way that changes the outcome.
- Finish the whole task, not the easy part of it. If one destination fails, complete \
  every other part and state plainly what did not work and why.
- Never claim a step succeeded unless a tool result says so. If a tool reports \
  `excel_launched: false`, say the workbook was produced by a fallback engine rather \
  than saying Excel was opened.
- Verify once. After writing to Excel and to Google Sheets, call the matching verify \
  tool exactly once per destination. Do not re-verify or re-run a step that already \
  reported success.
- Prefer several tool calls in one turn when they are independent.

Your final message is a status report for the user. Structure it as:

1. One sentence stating the overall outcome.
2. A per-step list. Each line: step name — SUCCESS / FAILED / SKIPPED — the concrete \
   evidence (row counts, absolute file paths, the spreadsheet URL).
3. Anything that failed, with the reason and the single most useful next step.

Keep it scannable. No preamble, no restating the instruction back."""


# Frontier models do not need this; smaller open-weight models (the Groq free
# tier, local Ollama) are measurably more reliable in a long tool loop with it.
# Phrased as positive guidance rather than warnings, which such models follow
# better than prohibitions.
SMALL_MODEL_NOTES = """\
## Operating notes

- Call one tool per turn and wait for its result before deciding the next step.
- Use tool names exactly as given, and pass only arguments defined in that tool's schema.
- Take file paths from previous tool results rather than composing them yourself. Every
  tool that writes a file returns its absolute path — reuse that value verbatim.
- After a write succeeds, call the matching verify tool once, then move to the next
  destination.
- When every step in the plan has been attempted, stop calling tools and write the final
  status report as plain text."""


def build_system_prompt(
    settings: Settings,
    registry: ToolRegistry,
    memory: SessionMemory,
    plan_text: str | None = None,
) -> str:
    excel_hint = (
        "Microsoft Excel automation is expected to work (Windows host with pywin32 installed). "
        "Call `excel_probe` first if you want certainty."
        if sys.platform == "win32"
        else (
            f"This host is {sys.platform}, so the Microsoft Excel application CANNOT be launched. "
            "`excel_import_csv` will automatically fall back to writing the .xlsx with openpyxl. "
            "Use it anyway, and report the fallback honestly."
        )
    )

    if settings.google_auth_mode == "disabled":
        google_hint = (
            "Google Sheets is DISABLED (GOOGLE_AUTH_MODE=disabled). Do not attempt the upload; "
            "report that step as skipped with the reason."
        )
    else:
        google_hint = (
            f"Google Sheets auth mode is '{settings.google_auth_mode}'. "
            "Credentials are resolved by the tool; if they are missing the tool returns a clear "
            "error with setup instructions — surface that verbatim rather than retrying blindly."
        )

    # Names only. Each tool's full description already arrives with its schema,
    # so repeating it here would pay for the same tokens twice on every turn —
    # which matters on a free tier with a per-minute token budget.
    tool_names = ", ".join(tool.name for tool in registry.enabled_tools())
    disabled = [tool.name for tool in registry.all_tools() if not tool.enabled]

    sections = [
        AGENT_ROLE,
        "## Environment\n"
        f"- Host: {platform.system()} {platform.release()} (python {platform.python_version()})\n"
        f"- Working directory for generated files: {Path(settings.workspace_dir).resolve()}\n"
        f"  Relative file names you pass to tools are created there.\n"
        f"- Excel: {excel_hint}\n"
        f"- Google Sheets: {google_hint}",
        f"## Available tools\n{tool_names}\n(Full parameters are in each tool's schema.)"
        + (f"\n\nDisabled in configuration (do not attempt): {', '.join(disabled)}" if disabled else ""),
        "## Working memory from this session\n"
        f"{memory.facts_as_prompt()}\n\n"
        f"Earlier runs:\n{memory.previous_runs_as_prompt()}",
    ]

    if plan_text:
        sections.append(
            "## Your plan\n"
            f"{plan_text}\n\n"
            "You wrote this plan before starting. Follow it, but adapt if a tool result "
            "contradicts an assumption — the plan is guidance, not a contract."
        )

    if not settings.uses_anthropic():
        sections.append(SMALL_MODEL_NOTES)

    return "\n\n".join(sections)


PLANNER_SYSTEM = """\
You are the planning stage of an autonomous spreadsheet agent. Given a user instruction \
and the available tools, produce a concise ordered plan.

Only include steps that are actually required by the instruction. Each step should map to \
one tool call (or a small group of related calls). Note where a step might fail and what \
the fallback is. Do not write the prose report here — that comes later."""


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {
            "type": "string",
            "description": "One sentence restating what the user wants, in concrete terms.",
        },
        "steps": {
            "type": "array",
            "description": "Ordered execution steps.",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "description": "1-based step number."},
                    "title": {"type": "string", "description": "Short imperative title, e.g. 'Generate employee CSV'."},
                    "tool": {
                        "type": "string",
                        "description": "Name of the tool this step will call, or 'none' for a reasoning-only step.",
                    },
                    "detail": {"type": "string", "description": "What this step does and the key arguments."},
                    "fallback": {
                        "type": "string",
                        "description": "What to do if this step fails. Use 'none' if there is no fallback.",
                    },
                },
                "required": ["id", "title", "tool", "detail", "fallback"],
                "additionalProperties": False,
            },
        },
        "risks": {
            "type": "array",
            "description": "Environment-specific things that could go wrong.",
            "items": {"type": "string"},
        },
    },
    "required": ["goal", "steps", "risks"],
    "additionalProperties": False,
}


def build_planner_prompt(instruction: str, registry: ToolRegistry, memory: SessionMemory) -> str:
    catalogue = "\n".join(
        f"### {tool.name}\n{tool.effective_description}" for tool in registry.enabled_tools()
    )
    return (
        f"User instruction:\n{instruction!r}\n\n"
        f"Tools available:\n{catalogue}\n\n"
        f"Working memory from this session:\n{memory.facts_as_prompt()}\n\n"
        "Produce the plan."
    )
