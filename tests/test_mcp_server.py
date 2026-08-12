"""MCP server integration test.

Spawns the real server as a subprocess over stdio and drives it with the MCP
client — a genuine protocol handshake, not a mocked one. Proves the same tool
registry the agent uses is correctly published to an MCP host.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

mcp = pytest.importorskip("mcp", reason="The 'mcp' package is not installed.")

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _server_params(tmp_path: Path) -> StdioServerParameters:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(PROJECT_ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "ANTHROPIC_API_KEY": "not-needed-for-mcp",
            "WORKSPACE_DIR": str(tmp_path / "workspace"),
            "LOG_DIR": str(tmp_path / "logs"),
            "MEMORY_DIR": str(tmp_path / "memory"),
            "TOOLS_CONFIG": str(PROJECT_ROOT / "config" / "tools.yaml"),
            "GOOGLE_AUTH_MODE": "disabled",
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "agentic_sheets.mcp_server.server"],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


async def _roundtrip(tmp_path: Path) -> tuple[list, dict]:
    async with stdio_client(_server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            called = await session.call_tool(
                "generate_employee_csv",
                {"row_count": 21, "filename": "mcp_employees.csv", "seed": 5},
            )
            payload = json.loads(called.content[0].text)
            return listed.tools, payload


def test_mcp_server_lists_tools_and_executes_one(tmp_path: Path):
    tools, payload = asyncio.run(_roundtrip(tmp_path))

    names = {tool.name for tool in tools}
    assert "generate_employee_csv" in names
    assert "excel_import_csv" in names
    assert "google_sheets_import" in names

    # Schemas must survive the protocol round-trip.
    generate = next(tool for tool in tools if tool.name == "generate_employee_csv")
    assert generate.input_schema["type"] == "object"
    assert "row_count" in generate.input_schema["properties"]
    assert generate.description

    # And the tool actually ran, inside the sandboxed workspace.
    assert payload["ok"] is True
    assert payload["row_count"] == 21
    written = Path(payload["csv_path"])
    assert written.exists()
    assert written.is_relative_to(tmp_path)


async def _error_roundtrip(tmp_path: Path) -> dict:
    async with stdio_client(_server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            called = await session.call_tool("read_csv_preview", {"csv_path": "absent.csv"})
            return json.loads(called.content[0].text)


def test_mcp_tool_errors_are_returned_as_structured_payloads(tmp_path: Path):
    payload = asyncio.run(_error_roundtrip(tmp_path))
    assert payload["ok"] is False
    assert "not found" in payload["error"].lower()
    assert "remediation" in payload
