"""``ov status`` operator digest — the wired-but-inert fix.

Root cause pinned: ``ov status`` read through
``LastSessionSummary.format_for_prompt_sync`` — the AUTONOMY-plane
surface gated by ``JARVIS_LAST_SESSION_SUMMARY_ENABLED`` (default
FALSE, governing the organism's prompt-injection authority) — so an
operator with 50+ session dirs on disk saw "no prior session found".

The fix separates the planes on the SAME parse machinery:
  * ``operator_digest`` / ``_sync`` — ungated, recency (mtime) ordered;
  * ``load`` / ``format_for_prompt`` — untouched, still autonomy-gated,
    still lex-max ordered.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.last_session_summary import (
    LastSessionSummary,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_LAST_SESSION_SUMMARY_ENABLED",
        "JARVIS_OV_STATUS_SESSIONS",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _mk_session(root: Path, name: str, *, mtime: float, **overrides) -> None:
    d = root / ".ouroboros" / "sessions" / name
    d.mkdir(parents=True)
    payload = {
        "schema_version": 2,
        "session_id": name,
        "stop_reason": "idle_timeout",
        "duration_s": 120.0,
        "stats": {"attempted": 3, "completed": 1, "failed": 2,
                  "cancelled": 0, "queued": 0},
        "cost_total": 0.25,
        "cost_breakdown": {"claude": 0.25},
    }
    payload.update(overrides)
    p = d / "summary.json"
    p.write_text(json.dumps(payload))
    os.utime(p, (mtime, mtime))


# ---------------------------------------------------------------------------
# (1) The operator plane is UNGATED
# ---------------------------------------------------------------------------


def test_digest_works_without_autonomy_flag(tmp_path):
    _mk_session(tmp_path, "bt-2026-07-18-000001", mtime=time.time())
    lss = LastSessionSummary(tmp_path)
    digest = lss.operator_digest_sync()
    assert digest is not None
    assert "bt-2026-07-18-000001" in digest
    assert "idle_timeout" in digest


def test_autonomy_plane_stays_gated(tmp_path):
    """The SAME instance: operator digest renders, prompt injection
    stays None while the master flag is unset (default FALSE)."""
    _mk_session(tmp_path, "bt-2026-07-18-000001", mtime=time.time())
    lss = LastSessionSummary(tmp_path)
    assert lss.operator_digest_sync() is not None
    assert lss.format_for_prompt_sync() is None


def test_digest_none_when_no_sessions(tmp_path):
    assert LastSessionSummary(tmp_path).operator_digest_sync() is None


# ---------------------------------------------------------------------------
# (2) Recency (mtime) ordering — bt-iso-* cannot shadow newer sessions
# ---------------------------------------------------------------------------


def test_mtime_order_beats_lex_shadowing(tmp_path):
    now = time.time()
    # Lexicographically bt-iso-* > bt-2026-* ("i" > "2") — but the
    # bt-2026 session closed LATER and must render FIRST.
    _mk_session(tmp_path, "bt-iso-1783990000", mtime=now - 3600)
    _mk_session(tmp_path, "bt-2026-07-18-235959", mtime=now)
    digest = LastSessionSummary(tmp_path).operator_digest_sync()
    assert digest is not None
    lines = digest.splitlines()
    assert lines[0].startswith("bt-2026-07-18-235959")
    assert lines[1].startswith("bt-iso-")


def test_session_count_env_knob(tmp_path, monkeypatch):
    now = time.time()
    for i in range(5):
        _mk_session(tmp_path, f"bt-2026-07-18-00000{i}", mtime=now - i)
    monkeypatch.setenv("JARVIS_OV_STATUS_SESSIONS", "2")
    digest = LastSessionSummary(tmp_path).operator_digest_sync()
    assert digest is not None
    assert len(digest.splitlines()) == 2


# ---------------------------------------------------------------------------
# (3) Render content — apply/verify/commit segments are conditional
# ---------------------------------------------------------------------------


def test_render_includes_apply_verify_commit_when_present(tmp_path):
    # v1.1a fields nest under summary.json's "ops_digest" key — the
    # fixture mirrors the REAL schema the harness writes.
    _mk_session(
        tmp_path, "bt-2026-07-18-000009", mtime=time.time(),
        ops_digest={
            "last_apply_mode": "multi", "last_apply_files": 2,
            "last_verify_tests_passed": 4, "last_verify_tests_total": 4,
            "last_commit_hash": "f6abcdbec5deadbeef",
        },
    )
    digest = LastSessionSummary(tmp_path).operator_digest_sync()
    assert digest is not None
    assert "apply=multi (2 file(s))" in digest
    assert "verify=4/4" in digest
    assert "commit=f6abcdbec5" in digest


def test_render_omits_segments_when_absent(tmp_path):
    _mk_session(tmp_path, "bt-2026-07-18-000010", mtime=time.time())
    digest = LastSessionSummary(tmp_path).operator_digest_sync()
    assert digest is not None
    assert "apply=" not in digest
    assert "verify=" not in digest
    assert "commit=" not in digest


# ---------------------------------------------------------------------------
# (4) ov status wiring — provider reads the operator plane
# ---------------------------------------------------------------------------


def test_ov_status_digest_uses_operator_plane(tmp_path, monkeypatch):
    from backend.core.ouroboros.cli import ov

    _mk_session(tmp_path, "bt-2026-07-18-000011", mtime=time.time())
    lss = LastSessionSummary(tmp_path)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.last_session_summary."
        "get_default_summary",
        lambda *a, **k: lss,
    )
    out = ov.status_digest()
    assert "bt-2026-07-18-000011" in out


def test_ov_status_fallback_when_empty(tmp_path, monkeypatch):
    from backend.core.ouroboros.cli import ov

    lss = LastSessionSummary(tmp_path)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.last_session_summary."
        "get_default_summary",
        lambda *a, **k: lss,
    )
    assert "no prior session" in ov.status_digest()


def test_ov_source_reads_operator_digest():
    root = Path(__file__).resolve().parents[2]
    src = (root / "backend/core/ouroboros/cli/ov.py").read_text()
    assert "operator_digest_sync" in src
    assert "format_for_prompt_sync" not in src   # the inert path is gone
