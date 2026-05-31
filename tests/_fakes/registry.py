"""Shared registry fakes for tests that need explicit file identity behavior."""

from __future__ import annotations

from typing import cast

from filigree.registry import BatchQuery, BatchResolution, ResolvedFile, resolve_files_batch_via_loop
from filigree.types.core import EntityId, FileId, RegistryBackend, make_entity_id, make_file_id


class FixedRegistry:
    """Registry fake that always returns the configured identity."""

    def __init__(
        self,
        *,
        file_id: str,
        content_hash: str = "",
        canonical_path: str | None = None,
        registry_backend: RegistryBackend = "local",
        displaced: bool = False,
    ) -> None:
        self.file_id = file_id
        self.content_hash = content_hash
        self.canonical_path = canonical_path
        self.registry_backend = registry_backend
        self.displaced = displaced

    def _resolved_file_id(self) -> FileId | EntityId:
        if self.displaced or self.registry_backend == "clarion":
            return make_entity_id(self.file_id)
        return make_file_id(self.file_id)

    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        # Deliberately polymorphic test double: callers inject arbitrary
        # backend/identity/hash combinations (including ones the discriminated
        # ResolvedFile union forbids) to exercise validation paths, so cast past
        # the union rather than statically committing to a member.
        return cast(
            ResolvedFile,
            {
                "file_id": self._resolved_file_id(),
                "content_hash": self.content_hash,
                "canonical_path": self.canonical_path or path,
                "language": language,
                "registry_backend": self.registry_backend,
            },
        )

    def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
        return resolve_files_batch_via_loop(self, queries, actor=actor)

    def is_displaced(self) -> bool:
        return self.displaced


class PathRegistry:
    """Registry fake that derives a stable local file id from the input path."""

    def resolve_file(self, path: str, *, language: str = "", actor: str = "") -> ResolvedFile:
        file_path = path.replace("/", "-")
        return {
            "file_id": make_file_id(f"core:file:{file_path}"),
            "content_hash": "",
            "canonical_path": path,
            "language": language,
            "registry_backend": "local",
        }

    def resolve_files_batch(self, queries: list[BatchQuery], *, actor: str = "") -> BatchResolution:
        return resolve_files_batch_via_loop(self, queries, actor=actor)

    def is_displaced(self) -> bool:
        return False
