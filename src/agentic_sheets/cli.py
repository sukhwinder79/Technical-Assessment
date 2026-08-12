"""Command-line interface.

    agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
    agentic-sheets doctor
    agentic-sheets tools
    agentic-sheets sessions
    agentic-sheets chat
    agentic-sheets serve
    agentic-sheets mcp
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .agent import build_agent
from .config import get_settings
from .console import ConsoleRenderer
from .errors import AgentError, ConfigurationError
from .events import EventBus
from .memory import list_sessions
from .tools.registry import build_default_registry

load_dotenv()

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Autonomous AI agent that generates spreadsheet data and imports it into Excel and Google Sheets.",
)
console = Console()

DEFAULT_PROMPT = "Create a sample employee CSV and import it into Excel and Google Sheets."


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"agentic-sheets {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version and exit."
    ),
) -> None:
    """Autonomous spreadsheet agent."""


# ==========================================================================
#  run
# ==========================================================================


@app.command()
def run(
    instruction: str = typer.Argument(
        DEFAULT_PROMPT,
        help="Natural-language instruction for the agent.",
    ),
    session: Optional[str] = typer.Option(
        None, "--session", "-s", help="Reuse (or create) a named session so the agent remembers earlier runs."
    ),
    continue_session: bool = typer.Option(
        False, "--continue", "-c", help="Continue the session's conversation instead of starting a fresh turn."
    ),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM_MODEL for this run."),
    provider: Optional[str] = typer.Option(
        None, "--provider", "-p", help="Override LLM_PROVIDER: groq | anthropic | openai | openrouter | together | ollama."
    ),
    effort: Optional[str] = typer.Option(
        None, "--effort", "-e", help="Reasoning effort (Anthropic only): low | medium | high | xhigh | max."
    ),
    no_plan: bool = typer.Option(False, "--no-plan", help="Skip the planning pass (single-phase execution)."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide the model's streamed narration."),
    json_out: Optional[Path] = typer.Option(
        None, "--json-out", help="Write the full machine-readable run report to this file."
    ),
) -> None:
    """Run the agent against a natural-language instruction."""
    settings = get_settings()
    if provider:
        settings.llm_provider = provider  # type: ignore[assignment]
    if model:
        settings.llm_model = model
    if effort:
        settings.agent_effort = effort  # type: ignore[assignment]
    if no_plan:
        settings.agent_planning = False

    events = EventBus()
    renderer = ConsoleRenderer(console, show_narration=not quiet)
    events.subscribe(renderer)

    try:
        agent = build_agent(
            settings=settings,
            events=events,
            session_id=session or f"session-{uuid.uuid4().hex[:8]}",
        )
    except ConfigurationError as exc:
        console.print(Panel(str(exc), title="Configuration error", border_style="red"))
        raise typer.Exit(code=2)

    result = agent.run(instruction, continue_session=continue_session)
    renderer.render_result(result)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
        console.print(f"[dim]Run report written to {json_out}[/dim]")

    raise typer.Exit(code={"completed": 0, "partial": 1, "failed": 2}.get(result.status, 2))


# ==========================================================================
#  chat
# ==========================================================================


@app.command()
def chat(
    session: Optional[str] = typer.Option(None, "--session", "-s", help="Session name to persist history under."),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Hide the model's streamed narration."),
) -> None:
    """Interactive multi-turn mode. Demonstrates conversation memory."""
    settings = get_settings()
    session_id = session or f"chat-{uuid.uuid4().hex[:8]}"

    events = EventBus()
    renderer = ConsoleRenderer(console, show_narration=not quiet)
    events.subscribe(renderer)

    try:
        agent = build_agent(settings=settings, events=events, session_id=session_id)
    except ConfigurationError as exc:
        console.print(Panel(str(exc), title="Configuration error", border_style="red"))
        raise typer.Exit(code=2)

    console.print(
        Panel(
            f"Session [cyan]{session_id}[/cyan] — the agent remembers earlier turns.\n"
            "Type an instruction, or 'exit' to quit.",
            title="Interactive mode",
            border_style="cyan",
        )
    )

    first = True
    while True:
        try:
            instruction = console.input("\n[bold cyan]you ›[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]bye[/dim]")
            break
        if not instruction:
            continue
        if instruction.lower() in {"exit", "quit", ":q"}:
            console.print("[dim]bye[/dim]")
            break

        result = agent.run(instruction, continue_session=not first)
        renderer.render_result(result)
        first = False


# ==========================================================================
#  tools
# ==========================================================================


@app.command()
def tools(
    schemas: bool = typer.Option(
        False, "--schemas", help="Print each tool's JSON Schema exactly as the current provider receives it."
    ),
    raw: bool = typer.Option(
        False,
        "--raw",
        help="With --schemas, show the unmodified Pydantic schema instead of the wire form.",
    ),
) -> None:
    """List the agent's toolbox as configured by config/tools.yaml."""
    settings = get_settings()
    registry = build_default_registry(Path(settings.tools_config))

    table = Table(title="Registered tools", header_style="dim")
    # Fold rather than truncate: on an 80-column terminal an ellipsised
    # `generate_employe…` hides the one field the reader actually needs.
    table.add_column("Tool", style="cyan", overflow="fold", min_width=21)
    table.add_column("On", width=4, justify="center")
    table.add_column("Retries", width=7, justify="right", style="dim")
    table.add_column("Tags", style="dim", overflow="fold")
    table.add_column("Description", overflow="fold", ratio=2)

    for tool in registry.all_tools():
        table.add_row(
            tool.name,
            "[green]✔[/green]" if tool.enabled else "[red]✘[/red]",
            str(registry.retries_for(tool, settings.tool_max_retries)),
            ", ".join(tool.tags),
            tool.summary,
        )
    console.print(table)
    console.print(f"[dim]Config: {Path(settings.tools_config).resolve()}[/dim]")

    if not schemas:
        return

    # Show what actually goes on the wire. For OpenAI-compatible providers that
    # is NOT the Pydantic schema: nullable `anyOf` unions and `title`/`default`
    # noise are stripped first, because small open models mis-generate against
    # them (see llm/openai_compatible_client.simplify_schema). Printing the raw
    # schema here would misrepresent the request.
    provider = settings.resolved_provider()
    simplify = not settings.uses_anthropic() and not raw

    if simplify:
        from .llm.openai_compatible_client import simplify_schema

        transform = simplify_schema
        note = (
            f"Schemas as sent to [cyan]{provider}[/cyan] — nullable unions collapsed and "
            "title/default stripped for reliable tool calling on smaller models. "
            "Use [cyan]--raw[/cyan] to see the original Pydantic output."
        )
    else:
        transform = lambda schema: schema  # noqa: E731 - trivial identity
        note = (
            "Unmodified Pydantic schemas."
            if raw
            else f"Schemas as sent to [cyan]{provider}[/cyan] (sent verbatim)."
        )

    console.print(f"\n[dim]{note}[/dim]\n")
    for tool in registry.enabled_tools():
        console.print(
            Panel(
                json.dumps(transform(tool.input_schema()), indent=2),
                title=f"{tool.name}  [dim]{'wire form' if simplify else 'raw'}[/dim]",
                border_style="dim",
            )
        )


# ==========================================================================
#  doctor
# ==========================================================================


@app.command()
def doctor() -> None:
    """Check the environment: API key, Excel automation, Google credentials, paths."""
    settings = get_settings()
    settings.ensure_directories()

    table = Table(title="Environment check", header_style="dim")
    table.add_column("Check", style="cyan")
    table.add_column("Status", width=8)
    table.add_column("Detail", overflow="fold")

    ok = "[green]OK[/green]"
    warn = "[yellow]WARN[/yellow]"
    fail = "[red]FAIL[/red]"
    problems = 0

    # --- LLM ----------------------------------------------------------------
    provider = settings.resolved_provider()
    key_var = settings.expected_key_env_var()
    key = settings.resolved_llm_api_key()

    table.add_row(
        "LLM provider",
        ok,
        f"{provider}"
        + ("  (free tier)" if provider == "groq" else "")
        + (f" · {settings.resolved_llm_base_url()}" if settings.resolved_llm_base_url() else ""),
    )
    if key:
        masked = f"{key[:8]}…{key[-4:]}" if len(key) > 14 else "set"
        table.add_row("API key", ok, f"{key_var} set ({masked})")
    else:
        problems += 1
        table.add_row(
            "API key",
            fail,
            f"{key_var} is not set. Copy .env.example to .env and add it. "
            + ("Free Groq keys: https://console.groq.com/keys" if provider == "groq" else ""),
        )

    model_detail = settings.resolved_llm_model()
    if settings.uses_anthropic():
        model_detail += f" (effort={settings.agent_effort or 'api default'})"
    table.add_row("Model", ok, model_detail)

    # --- Paths --------------------------------------------------------------
    for label, path in (
        ("Workspace", settings.workspace_dir),
        ("Logs", settings.log_dir),
        ("Memory", settings.memory_dir),
    ):
        resolved = Path(path).resolve()
        writable = resolved.exists()
        table.add_row(label, ok if writable else fail, str(resolved))
        if not writable:
            problems += 1

    # --- Excel --------------------------------------------------------------
    from .tools.excel_tools import _com_available, _probe_excel_registration

    available, reason = _com_available()
    if not available:
        table.add_row(
            "Microsoft Excel",
            warn,
            f"{reason} The agent will fall back to openpyxl and report excel_launched=false.",
        )
    else:
        excel_ok, excel_reason, _version = _probe_excel_registration()
        table.add_row("Microsoft Excel", ok if excel_ok else warn, excel_reason)

    # --- Google -------------------------------------------------------------
    if settings.google_auth_mode == "disabled":
        table.add_row("Google Sheets", warn, "GOOGLE_AUTH_MODE=disabled — the upload step will be skipped.")
    else:
        try:
            from .tools.sheets_tools import _load_credentials

            _load_credentials(settings)
            detail = f"credentials load OK (mode={settings.google_auth_mode})"
            if settings.google_auth_mode == "service_account" and not settings.google_share_with_email:
                detail += " · set GOOGLE_SHARE_WITH_EMAIL so you can open the created sheet"
            table.add_row("Google Sheets", ok, detail)
        except AgentError as exc:
            problems += 1
            table.add_row("Google Sheets", fail, str(exc))
        except Exception as exc:  # noqa: BLE001
            problems += 1
            table.add_row("Google Sheets", fail, f"{type(exc).__name__}: {exc}")

    # --- Tools --------------------------------------------------------------
    registry = build_default_registry(Path(settings.tools_config))
    enabled = registry.enabled_tools()
    table.add_row("Tools", ok if enabled else fail, f"{len(enabled)} enabled of {len(registry.all_tools())}")

    console.print(table)
    if problems:
        console.print(f"\n[red]{problems} blocking problem(s).[/red] See the README for setup steps.")
        raise typer.Exit(code=1)
    console.print("\n[green]Ready to run.[/green]")


# ==========================================================================
#  sessions
# ==========================================================================


@app.command()
def sessions(
    show: Optional[str] = typer.Option(None, "--show", help="Print the stored memory for one session."),
) -> None:
    """List stored agent sessions (conversation history + working memory)."""
    settings = get_settings()
    directory = Path(settings.memory_dir)

    if show:
        path = directory / f"{show}.json"
        if not path.exists():
            console.print(f"[red]No such session:[/red] {show}")
            raise typer.Exit(code=1)
        console.print(Panel(path.read_text(encoding="utf-8"), title=show, border_style="cyan"))
        return

    names = list_sessions(directory)
    if not names:
        console.print(f"[dim]No sessions yet in {directory.resolve()}[/dim]")
        return

    table = Table(title="Sessions", header_style="dim")
    table.add_column("Session", style="cyan")
    table.add_column("Runs", justify="right", style="dim")
    table.add_column("Last artifacts", overflow="fold")
    for name in names:
        try:
            payload = json.loads((directory / f"{name}.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            table.add_row(name, "?", "[red]unreadable[/red]")
            continue
        runs = payload.get("runs", [])
        facts = payload.get("facts", {})
        interesting = {k: v for k, v in facts.items() if k.startswith("last_")}
        table.add_row(name, str(len(runs)), ", ".join(f"{k}={v}" for k, v in list(interesting.items())[:3]))
    console.print(table)


# ==========================================================================
#  serve / mcp
# ==========================================================================


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)."),
    open_browser: bool = typer.Option(False, "--open", "-o", help="Open the web UI in a browser."),
) -> None:
    """Start the web UI and HTTP API (REST + Server-Sent Events progress stream)."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]uvicorn is not installed.[/red] pip install -r requirements.txt")
        raise typer.Exit(code=2)

    url = f"http://{host}:{port}"
    console.print(
        Panel(
            f"[bold]Web UI[/bold]    {url}\n[dim]API docs[/dim]  {url}/docs",
            border_style="cyan",
            title="Agentic Spreadsheet Agent",
        )
    )

    if open_browser:
        import threading
        import webbrowser

        # Fire slightly late so uvicorn is listening before the tab loads.
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    uvicorn.run("agentic_sheets.api.server:app", host=host, port=port, reload=reload, log_config=None)


@app.command()
def mcp() -> None:
    """Run the MCP server over stdio, exposing every agent tool to an MCP client."""
    from .mcp_server.server import run_stdio

    run_stdio()


def entrypoint() -> None:
    try:
        app()
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\n[dim]interrupted[/dim]")
        sys.exit(130)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
