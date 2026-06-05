"""Symlink-safe helpers for project-scoped installer writes."""

from __future__ import annotations

from pathlib import Path


class UnsafeInstallPathError(ValueError):
    """Raised when an installer target could escape the project root."""


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _check_existing_components_not_symlinks(path: Path, root: Path) -> None:
    """Reject symlinks in existing path components between root and path."""
    current = root
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError as exc:  # pragma: no cover - guarded by callers
        raise UnsafeInstallPathError(f"Installer target {path} is outside project root {root}") from exc

    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeInstallPathError(f"Refusing to write through symlinked installer target: {current}")


def project_path(project_root: Path, *parts: str) -> Path:
    """Return a project-contained path and reject symlink escape hatches.

    The returned path is anchored at the resolved project root. Any existing
    component in the target path that is a symlink is rejected, including
    dangling symlinks, so file creation cannot be redirected outside the
    project by repository-controlled links.
    """
    root = project_root.resolve(strict=True)
    target = root.joinpath(*parts)
    _check_existing_components_not_symlinks(target, root)
    resolved_target = target.resolve(strict=False)
    if not _is_relative_to(resolved_target, root):
        raise UnsafeInstallPathError(f"Installer target {target} resolves outside project root {root}")
    return target


def ensure_project_dir(project_root: Path, *parts: str) -> Path:
    """Create and return a project-contained directory without following links."""
    target = project_path(project_root, *parts)
    target.mkdir(parents=True, exist_ok=True)
    # Re-check after creation so a pre-existing or concurrently inserted
    # symlink is never accepted as the installation directory.
    _check_existing_components_not_symlinks(target, project_root.resolve(strict=True))
    if not target.is_dir():
        raise UnsafeInstallPathError(f"Installer target directory is not a directory: {target}")
    return target


def reject_symlink(path: Path) -> None:
    """Reject a direct installer target that is a symlink, including dangling."""
    if path.is_symlink():
        raise UnsafeInstallPathError(f"Refusing to write through symlinked installer target: {path}")
