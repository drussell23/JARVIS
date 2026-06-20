"""Regression: codegen repo-root resolution must be ABSOLUTE so relative_to over
ABSOLUTE target_files never raises (L2/batch live bug, 2026-06-20).

Trigger: repo_roots maps the primary repo to a RELATIVE path (Path('.') — the
default when JARVIS_REPO_PATH is unset), while pytest emits ABSOLUTE failing-test
paths (/app/tests/seed/test_seed_defect.py). _build_codegen_prompt did
tfile.relative_to(repo_root) → ValueError "one path is relative and the other is
absolute" → L2 repair quarantined the op as generate_error.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.providers import (
    _abs_repo_root,
    _resolve_effective_repo_root,
)


class _Ctx:
    primary_repo = "jarvis"


def test_relative_repo_root_resolves_absolute():
    # The exact live trigger: relative '.' root via repo_roots.
    r = _resolve_effective_repo_root(_Ctx(), Path("."), {"jarvis": Path(".")})
    assert r.is_absolute()
    # And an absolute target file is now relativizable (the bug).
    tfile = (Path.cwd() / "tests" / "seed" / "test_seed_defect.py")
    assert tfile.relative_to(r) == Path("tests/seed/test_seed_defect.py")


def test_empty_repo_root_falls_back_to_cwd():
    r = _resolve_effective_repo_root(_Ctx(), Path(""), None)
    assert r.is_absolute()
    assert r == Path.cwd().resolve()


def test_empty_path_in_repo_roots_resolves_to_cwd():
    # pathlib normalizes Path("") -> Path(".") , so an empty/relative entry is a
    # valid "use cwd" root (resolved absolute), NOT a fall-through. This is the
    # exact live case (repo_roots[jarvis]=Path('.')) and it must yield cwd.
    r = _resolve_effective_repo_root(_Ctx(), Path("/tmp"), {"jarvis": Path("")})
    assert r.is_absolute()
    assert r == Path.cwd().resolve()


def test_valid_absolute_repo_roots_entry_wins():
    r = _resolve_effective_repo_root(_Ctx(), Path("/other"), {"jarvis": Path("/repo")})
    assert r == Path("/repo").resolve()


def test_none_repo_root_no_map_is_cwd():
    r = _resolve_effective_repo_root(_Ctx(), None, None)
    assert r == Path.cwd().resolve()


def test_abs_repo_root_helper():
    # None falls through; a string-empty (defensive) falls through; a Path("")
    # normalizes to "." → cwd (a valid relative root, resolved absolute).
    assert _abs_repo_root(None) is None
    assert _abs_repo_root("") is None            # type: ignore[arg-type]
    assert _abs_repo_root("   ") is None         # type: ignore[arg-type]
    assert _abs_repo_root(Path("")) == Path.cwd().resolve()
    assert _abs_repo_root(Path(".")) == Path.cwd().resolve()
    assert _abs_repo_root(Path("/x/y")) == Path("/x/y").resolve()


def test_unknown_primary_repo_falls_through():
    class C:
        primary_repo = "not-in-map"
    r = _resolve_effective_repo_root(C(), Path("/base"), {"jarvis": Path("/repo")})
    assert r == Path("/base").resolve()
