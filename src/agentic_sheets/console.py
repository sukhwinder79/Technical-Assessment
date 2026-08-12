"""Rich console renderer.

Subscribes to the agent's event bus and turns it into a live, readable trace:
the plan up front, each tool call as it happens, retries in amber, and a
per-step status table at the end. This is what the demo video records.
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .agent.executor import RunResult
from .events import Event

STATUS_STYLES = {
    "completed": "bold green",
    "partial": "bold yellow",
    "failed": "bold red",
}

STEP_STYLES = {
    "succeeded": ("SUCCESS", "green"),
    "failed": ("FAILED", "red"),
    "rejected": ("SKIPPED", "yellow"),
}


class ConsoleRenderer:
    def __init__(self, console: Console | None = None, *, show_narration: bool = True) -> None:
        self.console = console or Console()
        self.show_narration = show_narration
        self._mid_text = False

    # ---- event handling ----------------------------------------------------

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    def _flush_text(self) -> None:
        if self._mid_text:
            self.console.print()
            self._mid_text = False

    def _on_run_started(self, event: Event) -> None:
        data = event.data
        header = Text()
        header.append("Instruction  ", style="dim")
        header.append(str(data.get("instruction", "")), style="bold white")
        header.append("\nModel        ", style="dim")
        header.append(str(data.get("model", "")), style="cyan")
        header.append("\nRun / session", style="dim")
        header.append(f"  {data.get('run_id')} / {data.get('session_id')}", style="dim cyan")
        self.console.print(Panel(header, title="Agentic Spreadsheet Agent", border_style="cyan"))

    def _on_planning_started(self, event: Event) -> None:
        self._flush_text()
        self.console.print(f"[dim]· {event.message}[/dim]")

    def _on_plan_ready(self, event: Event) -> None:
        self._flush_text()
        plan = event.data.get("plan")
        if not plan:
            self.console.print(f"[yellow]· {event.message}[/yellow]")
            return

        table = Table(box=None, pad_edge=False, show_header=True, header_style="dim")
        table.add_column("#", width=3, style="dim")
        table.add_column("Step", style="bold")
        table.add_column("Tool", style="cyan")
        table.add_column("Detail", overflow="fold")
        for step in plan.get("steps", []):
            detail = Text(str(step.get("detail", "")), style="dim")
            fallback = str(step.get("fallback", "") or "").strip()
            if fallback and fallback.lower() != "none":
                # The declared contingency is the interesting half of a plan —
                # show it rather than burying it in the system prompt.
                detail.append(f"\nfallback: {fallback}", style="dim yellow")
            table.add_row(
                str(step.get("id", "")),
                str(step.get("title", "")),
                str(step.get("tool", "")),
                detail,
            )

        body: list = [Text(plan.get("goal", ""), style="italic"), Text(), table]
        if risks := plan.get("risks"):
            body.append(Text())
            risk_text = Text("Risks noted:\n", style="dim")
            for risk in risks:
                risk_text.append(f"  • {risk}\n", style="dim yellow")
            body.append(risk_text)

        self.console.print(Panel(Group(*body), title="Plan", border_style="magenta"))

    def _on_llm_turn_started(self, event: Event) -> None:
        self._flush_text()
        self.console.print(f"[dim]· {event.message}[/dim]")

    def _on_assistant_text(self, event: Event) -> None:
        if not self.show_narration:
            return
        self.console.print(
            event.message, end="", style="dim italic", highlight=False, markup=False, soft_wrap=True
        )
        self._mid_text = True

    def _step_line(self, glyph: str, style: str, message: str) -> None:
        """One trace line, truncated rather than wrapped.

        A live trace stays scannable only if every step is exactly one line —
        soft-wrapped continuation lines start at column zero and read like new
        events.
        """
        self._flush_text()
        line = Text("  ")
        line.append(glyph, style=style)
        line.append(" ")
        line.append(message)
        self.console.print(line, no_wrap=True, overflow="ellipsis")

    def _on_tool_started(self, event: Event) -> None:
        self._step_line("→", "cyan", event.message)

    def _on_tool_succeeded(self, event: Event) -> None:
        self._step_line("✔", "green", event.message)

    def _on_tool_failed(self, event: Event) -> None:
        self._step_line("✘", "red", event.message)

    def _on_tool_retrying(self, event: Event) -> None:
        self._step_line("↻", "yellow", event.message)

    def _on_run_failed(self, event: Event) -> None:
        self._flush_text()
        self.console.print(f"[red]Run failed:[/red] {escape(event.message)}")

    def _on_run_finished(self, event: Event) -> None:
        self._flush_text()

    # ---- final report ------------------------------------------------------

    def render_result(self, result: RunResult) -> None:
        self._flush_text()
        self.console.print()

        if result.steps:
            table = Table(title="Step report", title_style="bold", header_style="dim", box=None)
            table.add_column("#", width=3, style="dim")
            table.add_column("Tool", style="cyan")
            table.add_column("Status", width=8)
            table.add_column("Time", width=7, justify="right", style="dim")
            table.add_column("Detail", overflow="fold")
            for step in result.steps:
                label, style = STEP_STYLES.get(step.status, (step.status.upper(), "white"))
                detail = step.summary or step.error or ""
                if step.attempts > 1:
                    detail = f"{detail}  (after {step.attempts} attempts)"
                table.add_row(
                    str(step.index),
                    step.tool,
                    f"[{style}]{label}[/{style}]",
                    f"{step.duration_s}s",
                    escape(detail),
                )
            self.console.print(table)
            self.console.print()

        if result.artifacts:
            artefacts = Table(box=None, show_header=False, pad_edge=False)
            artefacts.add_column(style="dim", no_wrap=True)
            artefacts.add_column(style="white", overflow="fold")
            for key, value in result.artifacts.items():
                artefacts.add_row(key, str(value))
            self.console.print(Panel(artefacts, title="Artifacts", border_style="blue"))

        if result.final_message:
            self.console.print(
                Panel(Markdown(result.final_message), title="Agent report", border_style="green")
            )

        if result.error:
            self.console.print(Panel(escape(result.error), title="Error", border_style="red"))

        style = STATUS_STYLES.get(result.status, "bold white")
        usage = result.usage or {}
        footer = (
            f"[{style}]{result.status.upper()}[/{style}]  "
            f"[dim]{len(result.succeeded_steps)}/{len(result.steps)} steps ok · "
            f"{result.iterations} model turns · {result.duration_s}s · "
            f"{usage.get('input_tokens', 0)} in / {usage.get('output_tokens', 0)} out tokens[/dim]"
        )
        self.console.print(footer)
