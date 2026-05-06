"""Agent-facing MCP response projections.

Internal database rows use the classic ``id`` primary-key field. MCP responses
use the 2.0 agent vocabulary, where an entity's own primary key is named for
the entity (``issue_id``, ``file_id``, etc.) while cross-entity references keep
their existing names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _rename_primary_id(record: Mapping[str, Any], new_key: str) -> dict[str, Any]:
    payload = dict(record)
    if "id" in payload:
        payload[new_key] = payload.pop("id")
    return payload


def issue_dict_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "issue_id")


def file_record_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "file_id")


def file_assoc_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "assoc_id")


def finding_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "finding_id")


def observation_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "observation_id")


def comment_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "comment_id")


def event_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "event_id")


def timeline_entry_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = _rename_primary_id(record, "timeline_event_id")
    source_id = payload.pop("source_id", None)
    if source_id is None:
        return payload

    event_type = payload.get("type")
    if event_type in {"finding_created", "finding_updated"}:
        payload["finding_id"] = source_id
    elif event_type == "association_created":
        payload["assoc_id"] = source_id
    elif event_type == "file_metadata_update":
        payload["file_event_id"] = source_id
    elif event_type == "issue_event":
        payload["event_id"] = source_id
        data = payload.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("issue_id"), str):
            payload["issue_id"] = data["issue_id"]
    else:
        payload["source_ref"] = source_id
    return payload


def critical_path_node_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    return _rename_primary_id(record, "issue_id")


def file_detail_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    file_record = payload.get("file")
    if isinstance(file_record, Mapping):
        payload["file"] = file_record_to_mcp(file_record)
    payload["associations"] = [file_assoc_to_mcp(item) if isinstance(item, Mapping) else item for item in payload.get("associations", [])]
    payload["recent_findings"] = [
        finding_to_mcp(item) if isinstance(item, Mapping) else item for item in payload.get("recent_findings", [])
    ]
    return payload


def plan_tree_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    milestone = payload.get("milestone")
    if isinstance(milestone, Mapping):
        payload["milestone"] = issue_dict_to_mcp(milestone)

    phases: list[Any] = []
    for phase_item in payload.get("phases", []):
        if not isinstance(phase_item, Mapping):
            phases.append(phase_item)
            continue
        phase_payload = dict(phase_item)
        phase = phase_payload.get("phase")
        if isinstance(phase, Mapping):
            phase_payload["phase"] = issue_dict_to_mcp(phase)
        phase_payload["steps"] = [issue_dict_to_mcp(step) if isinstance(step, Mapping) else step for step in phase_payload.get("steps", [])]
        phases.append(phase_payload)
    payload["phases"] = phases
    return payload


def undo_result_to_mcp(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(record)
    issue = payload.get("issue")
    if isinstance(issue, Mapping):
        payload.pop("issue", None)
        public_issue = issue_dict_to_mcp(issue)
        public_issue.update(payload)
        return public_issue
    return payload
