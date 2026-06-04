"""Runtime context shared by MCP transport and domain tool modules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.types import Tool

if TYPE_CHECKING:
    from filigree.core import FiligreeDB


@dataclass(frozen=True)
class McpToolMetadata:
    """Snapshot of the assembled MCP tool catalogue."""

    tools: tuple[Tool, ...] = ()
    tool_subsystem: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class McpRuntimeContext:
    """Small dependency surface injected by the MCP transport."""

    get_db: Callable[[], FiligreeDB]
    get_filigree_dir: Callable[[], Path | None]
    safe_path: Callable[[str], Path]
    refresh_summary: Callable[[], None]
    get_status_payload: Callable[[], dict[str, Any]]
    get_tool_metadata: Callable[[], McpToolMetadata]
    resolve_request_filigree_dir: Callable[[FiligreeDB], Path]

    @property
    def db(self) -> FiligreeDB:
        return self.get_db()

    @property
    def filigree_dir(self) -> Path | None:
        return self.get_filigree_dir()

    @property
    def tools(self) -> tuple[Tool, ...]:
        return self.get_tool_metadata().tools

    @property
    def tool_subsystem(self) -> Mapping[str, str]:
        return self.get_tool_metadata().tool_subsystem


_context: McpRuntimeContext | None = None


def set_runtime_context(context: McpRuntimeContext) -> None:
    global _context
    _context = context


def get_runtime_context() -> McpRuntimeContext:
    if _context is None:
        msg = "MCP runtime context has not been initialized"
        raise RuntimeError(msg)
    return _context
