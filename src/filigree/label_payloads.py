"""Public label-discovery payload helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_INTERNAL_BARE_NAMESPACE = "_bare"
PUBLIC_UNNAMESPACED_NAMESPACE = "unnamespaced"


def label_namespace_from_public(namespace: str | None) -> str | None:
    """Translate public namespace names to the DB's internal sentinel."""
    if namespace == PUBLIC_UNNAMESPACED_NAMESPACE:
        return _INTERNAL_BARE_NAMESPACE
    return namespace


def label_namespace_to_public(namespace: str) -> str:
    """Translate DB namespace keys to names exposed by CLI/MCP/API clients."""
    if namespace == _INTERNAL_BARE_NAMESPACE:
        return PUBLIC_UNNAMESPACED_NAMESPACE
    return namespace


def label_namespace_item_to_public(namespace: str, data: Mapping[str, Any]) -> dict[str, Any]:
    return {"namespace": label_namespace_to_public(namespace), **dict(data)}
