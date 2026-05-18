"""File identity registry backends."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal, Protocol, TypedDict
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

RegistryBackend = Literal["local", "clarion"]
DEFAULT_TEST_REGISTRY_BACKENDS: tuple[RegistryBackend, ...] = ("local",)
REGISTRY_BACKEND_FEATURES: tuple[RegistryBackend, ...] = ("local", "clarion")
SUPPORTED_REGISTRY_BACKENDS = DEFAULT_TEST_REGISTRY_BACKENDS


class ResolvedFile(TypedDict):
    """File identity resolved by the configured registry backend."""

    file_id: str
    content_hash: str
    canonical_path: str
    language: str
    registry_backend: RegistryBackend


class RegistryProtocol(Protocol):
    """Protocol consumed by file auto-create paths."""

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile: ...

    def is_displaced(self) -> bool: ...


class RegistryUnavailableError(RuntimeError):
    """Raised when the configured registry backend cannot resolve a file."""


def clarion_file_read_url(base_url: str, path: str, *, language: str = "") -> str:
    """Build the Clarion read-API URL for an operator-facing hint."""
    query = urlencode({"path": path, "language": language})
    return f"{base_url.rstrip('/')}/api/v1/files?{query}"


class LocalRegistry:
    """Filigree-native registry backend."""

    def __init__(self, file_id_factory: Callable[[], str]) -> None:
        self._file_id_factory = file_id_factory

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        return ResolvedFile(
            file_id=self._file_id_factory(),
            content_hash="",
            canonical_path=path,
            language=language,
            registry_backend="local",
        )

    def is_displaced(self) -> bool:
        return False


class ClarionRegistry:
    """HTTP-backed registry that resolves file identity through Clarion."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 5) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def resolve_file(
        self,
        path: str,
        *,
        language: str = "",
        actor: str = "",
    ) -> ResolvedFile:
        url = clarion_file_read_url(self.base_url, path, language=language)
        try:
            with urlopen(url, timeout=self.timeout_seconds) as response:  # noqa: S310
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            msg = f"Clarion registry unavailable at {url}: {exc}"
            raise RegistryUnavailableError(msg) from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            msg = f"Clarion registry returned invalid JSON from {url}: {exc}"
            raise RegistryUnavailableError(msg) from exc
        if not isinstance(payload, dict):
            msg = f"Clarion registry returned non-object response from {url}: {type(payload).__name__}"
            raise RegistryUnavailableError(msg)

        required = ("entity_id", "content_hash", "canonical_path", "language")
        missing = [field for field in required if not isinstance(payload.get(field), str)]
        if missing:
            msg = f"Clarion registry response from {url} missing string field(s): {', '.join(missing)}"
            raise RegistryUnavailableError(msg)

        return ResolvedFile(
            file_id=payload["entity_id"],
            content_hash=payload["content_hash"],
            canonical_path=payload["canonical_path"],
            language=payload["language"],
            registry_backend="clarion",
        )

    def is_displaced(self) -> bool:
        return True
