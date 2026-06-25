"""Public issue projections for agent-facing wire surfaces."""

from __future__ import annotations

from typing import Any, cast

from filigree.models import Issue
from filigree.types.api import PublicIssue, ReadyIssue, SlimIssue


def issue_to_slim(issue: Issue) -> SlimIssue:
    """Return the lightweight issue projection used by list-style surfaces."""
    return SlimIssue(
        issue_id=issue.id,
        title=issue.title,
        status=issue.status,
        priority=issue.priority,
        type=issue.type,
    )


def issue_to_ready(
    issue: Issue,
    *,
    include_context: bool = False,
    parent_title: str | None = None,
    startable: bool = True,
    next_action: str | None = None,
) -> ReadyIssue:
    """Return a ready-queue projection, optionally enriched with parent context.

    ``startable`` / ``next_action`` are computed by the caller from the issue's
    workflow template (filigree-406e6b7ee0). ``startable`` is always emitted;
    ``next_action`` is omitted when None to keep the default shape slim.
    """
    payload = cast(ReadyIssue, dict(issue_to_slim(issue)))
    payload["startable"] = startable
    if next_action is not None:
        payload["next_action"] = next_action
    if include_context:
        payload["parent_issue_id"] = issue.parent_id
        payload["parent_title"] = parent_title if issue.parent_id else None
    return payload


def issue_to_public(issue: Issue) -> PublicIssue:
    """Return the full 2.0 public issue shape with ``issue_id``.

    Keep ``Issue.to_dict()`` internal/classic-shaped; this helper is for MCP,
    CLI JSON, and other agent-facing surfaces that promise ``issue_id``.

    Emits both ``parent_id`` (legacy) and ``parent_issue_id`` (consistent
    with slim/ready shapes) carrying the same value. Closes the
    cross-tool naming inconsistency flagged by the senior-user MCP
    review (filigree-cb980eee0d, P2.9).
    """
    classic = issue.to_dict()
    return PublicIssue(
        issue_id=classic["id"],
        title=classic["title"],
        status=classic["status"],
        status_category=classic["status_category"],
        priority=classic["priority"],
        type=classic["type"],
        parent_id=classic["parent_id"],
        parent_issue_id=classic["parent_id"],
        assignee=classic["assignee"],
        claimed_at=classic["claimed_at"],
        last_heartbeat_at=classic["last_heartbeat_at"],
        claim_expires_at=classic["claim_expires_at"],
        created_at=classic["created_at"],
        updated_at=classic["updated_at"],
        closed_at=classic["closed_at"],
        claim_commit=classic["claim_commit"],
        close_commit=classic["close_commit"],
        description=classic["description"],
        notes=classic["notes"],
        fields=classic["fields"],
        labels=classic["labels"],
        blocks=classic["blocks"],
        blocked_by=classic["blocked_by"],
        is_ready=classic["is_ready"],
        children=classic["children"],
        data_warnings=classic["data_warnings"],
    )


def public_issue_with(issue: Issue, **extra: Any) -> dict[str, Any]:
    """Return public issue payload plus response-specific extension keys."""
    payload: dict[str, Any] = dict(issue_to_public(issue))
    payload.update(extra)
    return payload
