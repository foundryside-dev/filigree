"""MCP tools for cross-member federation consumer bindings.

Currently one tool — the heddle reverify-worklist consumer (Seam 2A of
``pm/2026-06-13-heddle-interface-lock.md``). heddle produces a
``heddle.reverify_worklist.v1`` worklist; Filigree is the write-capable
consumer that files-or-links its items as work, explicit-action only.

See :mod:`filigree.heddle_consumer` for the file/link logic and the acceptance
criteria it satisfies.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from mcp.types import TextContent, Tool

from filigree.heddle_consumer import ingest_reverify_worklist
from filigree.mcp_tools.common import (
    _parse_args,
    _text,
    _validate_actor,
    _validate_int_range,
    get_db,
)
from filigree.types.api import ErrorCode, ErrorResponse
from filigree.types.inputs import HeddleWorklistIngestArgs

_logger = logging.getLogger(__name__)


def register() -> tuple[list[Tool], dict[str, Callable[..., Any]]]:
    """Return (tool_definitions, handler_map) for federation consumer tools."""
    tools = [
        Tool(
            name="ingest_heddle_worklist",
            description=(
                "Consume a heddle reverify worklist (heddle.reverify_worklist.v1) and "
                "file-or-link its items as Filigree work — the write-capable half of the "
                "heddle<->filigree seam. Per item, keyed on the entity SEI: an entity already "
                "tracked by an open issue is reported as 'linked' (never re-filed); an "
                "untracked entity is 'filed' as a task carrying the heddle producer labels and "
                "an ADR-029 affected-entity association on the SEI (the surface heddle reads "
                "back, closing the loop); an item with no SEI is 'skipped'. heddle never "
                "auto-files: this call IS the explicit action, and it previews by default "
                "(apply=false, pure reads) — pass apply=true to perform the writes."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "worklist": {
                        "type": "object",
                        "description": (
                            "A heddle.reverify_worklist.v1 success envelope or its bare 'data' "
                            "payload (must contain an 'items' array)."
                        ),
                    },
                    "apply": {
                        "type": "boolean",
                        "default": False,
                        "description": "false (default) previews without writing; true files/links for real.",
                    },
                    "actor": {
                        "type": "string",
                        "description": "Identity recorded as issue creator / association attached_by (default 'heddle').",
                    },
                    "priority": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 4,
                        "description": "Optional priority override for every filed item (0=P0..4=P4).",
                    },
                    "content_hash": {
                        "type": "string",
                        "description": (
                            "Optional default content hash stamped on filed associations when an item "
                            "carries none; absent -> a documented 'unverified' sentinel."
                        ),
                    },
                },
                "required": ["worklist"],
            },
        ),
    ]

    handlers: dict[str, Callable[..., Any]] = {
        "ingest_heddle_worklist": _handle_ingest_heddle_worklist,
    }

    return tools, handlers


async def _handle_ingest_heddle_worklist(arguments: dict[str, Any]) -> list[TextContent]:
    from filigree.core import WrongProjectError

    args = _parse_args(arguments, HeddleWorklistIngestArgs)

    worklist = args.get("worklist")
    if not isinstance(worklist, Mapping):
        return _text(ErrorResponse(error="worklist must be an object", code=ErrorCode.VALIDATION))

    apply = args.get("apply", False)
    if not isinstance(apply, bool):
        return _text(ErrorResponse(error="apply must be a boolean", code=ErrorCode.VALIDATION))

    priority = args.get("priority")
    priority_err = _validate_int_range(priority, "priority", min_val=0, max_val=4)
    if priority_err:
        return priority_err

    content_hash = args.get("content_hash")
    if content_hash is not None and (not isinstance(content_hash, str) or not content_hash.strip()):
        return _text(ErrorResponse(error="content_hash must be a non-empty string when provided", code=ErrorCode.VALIDATION))

    actor, actor_err = _validate_actor(args.get("actor", "heddle"))
    if actor_err:
        return actor_err

    tracker = get_db()
    try:
        report = ingest_reverify_worklist(
            tracker,
            worklist,
            apply=apply,
            actor=actor,
            priority_override=priority,
            default_content_hash=content_hash,
        )
    except WrongProjectError as exc:
        # 2.1.0 §1.2: untrusted-surface serialisation uses safe_message.
        return _text(ErrorResponse(error=exc.safe_message, code=ErrorCode.VALIDATION))
    except ValueError as exc:
        return _text(ErrorResponse(error=str(exc), code=ErrorCode.VALIDATION))
    except sqlite3.Error as exc:
        _logger.error("ingest_heddle_worklist storage failure: %s", exc)
        return _text(ErrorResponse(error=f"Failed to ingest worklist: {exc}", code=ErrorCode.IO))
    return _text(report)
