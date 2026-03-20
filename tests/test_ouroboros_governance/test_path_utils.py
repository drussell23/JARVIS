"""Tests for path canonicalization utility.

Go/No-Go tests: T01 (test_dotslash_resolves_to_same),
                T02 (test_resolves_symlink),
                T03 (test_rejects_traversal).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.path_utils import (
    PathTraversalError,
    canonicalize_path,
)


# ---------------------------------------------------------------------------
# T01 — dot-slash prefix produces same result as bare path
# ---------------------------------------------------------------------------

def test_dotslash_resolves_to_same(tmp_path: Path) -> None:
    """./foo and foo must canonicalize to the same string."""
    result_bare = canonicalize_path("foo", tmp_path)
    result_dotslash = canonicalize_path("./foo", tmp_path)
    assert result_bare == result_dotslash


# ---------------------------------------------------------------------------
# T02 — symlink is resolved to its real target
# ---------------------------------------------------------------------------

def test_resolves_symlink(tmp_path: Path) -> None:
    """A symlinked path must resolve to the real file's canonical path."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "module.py"
    real_file.write_text("# real\n")

    link = tmp_path / "link.py"
    link.symlink_to(real_file)

    result_via_link = canonicalize_path("link.py", tmp_path)
    result_direct = canonicalize_path("real/module.py", tmp_path)
    assert result_via_link == result_direct


# ---------------------------------------------------------------------------
# T03 — traversal past repo root raises PathTraversalError
# ---------------------------------------------------------------------------

def test_rejects_traversal(tmp_path: Path) -> None:
    """../../etc/passwd must raise PathTraversalError."""
    with pytest.raises(PathTraversalError):
        canonicalize_path("../../etc/passwd", tmp_path)


# ---------------------------------------------------------------------------
# Additional robustness tests
# ---------------------------------------------------------------------------

def test_strips_trailing_slash(tmp_path: Path) -> None:
    """dir/ must canonicalize to dir (no trailing separator)."""
    result = canonicalize_path("somedir/", tmp_path)
    assert not result.endswith("/")
    assert result == "somedir"


def test_normalizes_double_slash(tmp_path: Path) -> None:
    """a//b.py must canonicalize to a/b.py."""
    result = canonicalize_path("a//b.py", tmp_path)
    assert "//" not in result
    assert result == "a/b.py"


def test_absolute_within_repo_ok(tmp_path: Path) -> None:
    """An absolute path that lives inside repo_root resolves to its relative form."""
    abs_path = str(tmp_path / "backend" / "core" / "foo.py")
    result = canonicalize_path(abs_path, tmp_path)
    assert result == "backend/core/foo.py"
    assert not os.path.isabs(result)


def test_nonexistent_file_still_canonicalizes(tmp_path: Path) -> None:
    """The target file does not need to exist for canonicalization to succeed."""
    result = canonicalize_path("does/not/exist.py", tmp_path)
    assert result == "does/not/exist.py"


def test_empty_string_returns_dot(tmp_path: Path) -> None:
    """An empty string input must return '.' (repo root reference)."""
    result = canonicalize_path("", tmp_path)
    assert result == "."
