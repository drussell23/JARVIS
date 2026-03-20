"""Path canonicalization — single source of truth for path identity.

All path comparisons, deduplication, and file-fence checks in the
Autonomy Iteration Mode must pass paths through ``canonicalize_path``
before comparing or storing them.  This ensures that ./foo, foo, and
symlinks pointing at foo all collapse to the same string, and that no
operation can sneak paths outside the repo root via traversal.
"""
from __future__ import annotations

from pathlib import Path


class PathTraversalError(ValueError):
    """Raised when a path resolves to a location outside the repo root."""


def canonicalize_path(path: str, repo_root: Path) -> str:
    """Canonicalize *path* relative to *repo_root*.

    Resolves: leading ``./``, ``../`` traversal, symlinks, double
    slashes, and trailing slashes.  Returns a clean, POSIX-style
    relative path string (e.g. ``"backend/core/foo.py"``).

    Parameters
    ----------
    path:
        The raw path string supplied by the caller.  May be absolute or
        relative, may contain ``./``, ``//``, trailing ``/``, or be an
        empty string.
    repo_root:
        Absolute ``Path`` to the repository root.  Used both as the
        base for relative resolution and as the containment boundary.

    Returns
    -------
    str
        Canonical relative path string.  Returns ``"."`` when *path* is
        empty or resolves to *repo_root* itself.

    Raises
    ------
    PathTraversalError
        When the resolved absolute path lies outside *repo_root*.
    """
    if not path or path.strip() == "":
        return "."

    repo_resolved: Path = repo_root.resolve()
    p = Path(path)

    if p.is_absolute():
        # Resolve symlinks within an already-absolute path.
        target = p.resolve()
    else:
        # Anchor relative path to repo root, then resolve symlinks /
        # normalise . and .. segments.
        target = (repo_resolved / p).resolve()

    try:
        rel = target.relative_to(repo_resolved)
    except ValueError:
        raise PathTraversalError(
            f"Path {path!r} resolves to {str(target)!r} which is "
            f"outside repo root {str(repo_resolved)!r}"
        )

    result = rel.as_posix()
    return result if result != "" else "."
