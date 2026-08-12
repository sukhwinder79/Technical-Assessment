"""Tool registry — discovery, YAML-driven configuration, and dispatch.

The registry is the only thing that knows the full toolbox. The executor asks
it for schemas, hands back a tool name, and gets an executed result. Because
enabling/disabling happens here (from `config/tools.yaml`), flipping a tool off
changes what the agent can *plan*, not just what it can call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import yaml

from ..errors import ConfigurationError, ToolNotFoundError
from ..logging_setup import get_logger
from .base import Tool, ToolContext

log = get_logger(__name__)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    # ---- registration ------------------------------------------------------

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ConfigurationError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def apply_config(self, config_path: Path) -> None:
        """Overlay `config/tools.yaml`. A missing file is not an error — the
        registry simply keeps its built-in defaults."""
        if not config_path.exists():
            log.info("tools.config.missing", path=str(config_path))
            return

        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"Could not parse {config_path}: {exc}") from exc

        defaults = raw.get("defaults") or {}
        default_retries = defaults.get("max_retries")

        for name, spec in (raw.get("tools") or {}).items():
            tool = self._tools.get(name)
            if tool is None:
                log.warning("tools.config.unknown_tool", tool=name)
                continue
            spec = spec or {}
            tool.enabled = bool(spec.get("enabled", True))
            if suffix := spec.get("description_suffix"):
                tool.description_override = f"{tool.description}\n\n{suffix.strip()}"
            retries = spec.get("max_retries", default_retries)
            if retries is not None:
                tool.max_retries_override = int(retries)

        # Tools absent from the YAML still pick up the global default.
        if default_retries is not None:
            for tool in self._tools.values():
                if tool.max_retries_override is None:
                    tool.max_retries_override = int(default_retries)

        log.info(
            "tools.config.applied",
            path=str(config_path),
            enabled=[t.name for t in self.enabled_tools()],
            disabled=[t.name for t in self._tools.values() if not t.enabled],
        )

    # ---- introspection -----------------------------------------------------

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def enabled_tools(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.enabled]

    def get(self, name: str) -> Tool:
        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(sorted(t.name for t in self.enabled_tools()))
            raise ToolNotFoundError(f"Unknown tool '{name}'. Available: {available}")
        if not tool.enabled:
            raise ToolNotFoundError(
                f"Tool '{name}' is disabled in config/tools.yaml. "
                "Complete as much of the task as the remaining tools allow, "
                "then report this step as skipped."
            )
        return tool

    def anthropic_schemas(self) -> list[dict[str, Any]]:
        return [t.to_anthropic_schema() for t in self.enabled_tools()]

    def retries_for(self, tool: Tool, fallback: int) -> int:
        if tool.max_retries_override is not None:
            return tool.max_retries_override
        if tool.max_retries is not None:
            return tool.max_retries
        return fallback

    # ---- dispatch ----------------------------------------------------------

    def execute(self, name: str, raw_args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
        tool = self.get(name)
        args = tool.parse_args(raw_args)
        return tool.run(args, ctx)


def build_default_registry(config_path: Path | None = None) -> ToolRegistry:
    """Construct the registry with every built-in tool, then apply YAML config."""
    from . import data_tools, excel_tools, sheets_tools  # local import avoids cycles

    registry = ToolRegistry(
        [
            data_tools.GenerateEmployeeCsvTool(),
            data_tools.ConvertSpreadsheetTool(),
            data_tools.ReadCsvPreviewTool(),
            excel_tools.ExcelProbeTool(),
            excel_tools.ExcelImportCsvTool(),
            excel_tools.ExcelVerifyWorkbookTool(),
            sheets_tools.GoogleSheetsImportTool(),
            sheets_tools.GoogleSheetsVerifyTool(),
        ]
    )
    if config_path is not None:
        registry.apply_config(config_path)
    return registry
