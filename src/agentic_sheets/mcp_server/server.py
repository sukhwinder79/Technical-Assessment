"""MCP server — exposes every agent tool over the Model Context Protocol.

The same `Tool` objects the agent uses internally are published over stdio, so
an MCP client (Claude Desktop, Claude Code, any MCP host) can drive Excel and
Google Sheets with its own model doing the orchestration. The tool registry is
the single source of truth; nothing is re-declared here.

Run it with:  agentic-sheets mcp

Targets the MCP 2.x low-level server API, where request handlers are supplied
as `on_*` callbacks at construction time (the 1.x `@server.list_tools()`
decorators were removed).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from ..config import Settings, get_settings
from ..errors import AgentError, ToolError, ToolNotFoundError
from ..events import EventBus
from ..logging_setup import configure_logging, get_logger
from ..memory import SessionMemory
from ..retry import RetryPolicy, call_with_retry
from ..tools.base import ToolContext
from ..tools.registry import ToolRegistry, build_default_registry

log = get_logger(__name__)

SERVER_NAME = "agentic-sheets"
SERVER_VERSION = "1.0.0"

INSTRUCTIONS = """\
Tools for building spreadsheet deliverables end to end.

Typical order: generate_employee_csv → excel_import_csv → excel_verify_workbook,
and/or google_sheets_import → google_sheets_verify. Use convert_spreadsheet for
XLSX/ODS output when Microsoft Excel is not available (call excel_probe to find
out). Relative file names resolve inside the server's workspace directory."""


def _build_context(settings: Settings) -> ToolContext:
    workspace = Path(settings.workspace_dir).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    return ToolContext(
        settings=settings,
        events=EventBus(),
        memory=SessionMemory(f"mcp-{uuid.uuid4().hex[:8]}", Path(settings.memory_dir)),
        workspace=workspace,
    )


def _run_tool(registry: ToolRegistry, settings: Settings, ctx: ToolContext, name: str, arguments: dict) -> dict:
    """Execute one tool synchronously, converting every failure into a payload."""
    try:
        tool = registry.get(name)
    except ToolNotFoundError as exc:
        return {"ok": False, "error": str(exc)}

    policy = RetryPolicy(
        max_retries=registry.retries_for(tool, settings.tool_max_retries),
        base_delay=settings.tool_retry_base_delay,
    )

    try:
        return call_with_retry(lambda: registry.execute(name, arguments, ctx), policy)
    except ToolError as exc:
        return exc.to_payload()
    except Exception as exc:  # noqa: BLE001
        log.exception("mcp.call.failed", tool=name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


async def _serve() -> None:
    try:
        import mcp.types as types
        from mcp.server import Server, ServerRequestContext
        from mcp.server.stdio import stdio_server
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise AgentError(
            "The 'mcp' package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    settings = get_settings()
    settings.ensure_directories()
    # stdio *is* the MCP transport, so logs must never touch stdout.
    configure_logging(Path(settings.log_dir), settings.log_level, json_console=True)

    registry = build_default_registry(Path(settings.tools_config))
    ctx = _build_context(settings)

    async def on_list_tools(
        _request: "ServerRequestContext[Any]",
        _params: "types.PaginatedRequestParams | None",
    ) -> "types.ListToolsResult":
        tools = [
            types.Tool(
                name=tool.name,
                description=tool.effective_description,
                input_schema=tool.input_schema(),
            )
            for tool in registry.enabled_tools()
        ]
        log.info("mcp.list_tools", count=len(tools))
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        _request: "ServerRequestContext[Any]",
        params: "types.CallToolRequestParams",
    ) -> "types.CallToolResult":
        arguments = params.arguments or {}
        log.info("mcp.call_tool", tool=params.name, arguments=arguments)

        # Tools are blocking (COM automation, HTTP); keep the event loop free.
        payload = await asyncio.to_thread(_run_tool, registry, settings, ctx, params.name, arguments)

        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
            is_error=not payload.get("ok", False),
        )

    server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )

    log.info("mcp.serving", tools=[tool.name for tool in registry.enabled_tools()])
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio() -> None:
    """Blocking entry point used by `agentic-sheets mcp`."""
    asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover
    run_stdio()
