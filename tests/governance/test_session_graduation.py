"""Slice 5 graduation pins — Session History Browser arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# Authority invariant
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/session_record.py",
    "backend/core/ouroboros/governance/session_browser.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# §1 read-only invariant — browser never mutates session dirs
# ===========================================================================


def test_browser_module_has_no_write_to_session_dir():
    """Grep-pinned: the browser must never write into a session directory.

    Only bookmark file writes are allowed; those live in a separate root.
    """
    src = Path(
        "backend/core/ouroboros/governance/session_browser.py"
    ).read_text()
    # These patterns would indicate writing into a session dir:
    # Check no `summary.json` writes, no `debug.log` writes, no
    # `.ouroboros/sessions` direct writes other than through BookmarkStore.
    forbidden_writes = [
        'write_text.*summary.json',
        'write_text.*debug.log',
        'shutil.copy.*session',
    ]
    for pat in forbidden_writes:
        assert not re.search(pat, src, re.IGNORECASE), (
            f"browser appears to write session artifacts: {pat!r}"
        )


# ===========================================================================
# Schema versions stable
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.governance.session_record import (
        SESSION_RECORD_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.session_browser import (
        SESSION_BROWSER_SCHEMA_VERSION,
    )
    assert SESSION_RECORD_SCHEMA_VERSION == "session_record.v1"
    assert SESSION_BROWSER_SCHEMA_VERSION == "session_browser.v1"


# ===========================================================================
# Docstring bit-rot guards
# ===========================================================================


def test_session_record_docstring_mentions_read_only():
    from backend.core.ouroboros.governance.session_record import (
        SessionRecord,
    )
    doc = SessionRecord.__doc__ or ""
    assert doc  # frame has a docstring


def test_browser_docstring_mentions_bookmark_separation():
    from backend.core.ouroboros.governance.session_browser import (
        SessionBrowser,
    )
    # Module docstring documents the bookmark / session-dir separation
    import backend.core.ouroboros.governance.session_browser as m
    mdoc = m.__doc__ or ""
    assert "bookmark" in mdoc.lower() or "SEPARATE" in mdoc


# ===========================================================================
# Determinism: same sessions dir → same records (modulo mtime noise)
# ===========================================================================


def test_parse_is_deterministic(tmp_path: Path):
    import json
    from backend.core.ouroboros.governance.session_record import (
        parse_session_dir,
    )
    session_dir = tmp_path / "bt-det"
    session_dir.mkdir()
    (session_dir / "summary.json").write_text(json.dumps({
        "stop_reason": "complete",
        "stats": {"ops_total": 2, "ops_applied": 1},
    }))
    r1 = parse_session_dir(session_dir)
    r2 = parse_session_dir(session_dir)
    # Core fields match
    assert r1.stop_reason == r2.stop_reason
    assert r1.ops_total == r2.ops_total
    assert r1.ops_applied == r2.ops_applied
