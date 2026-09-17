"""A session that never wrote its own ending must still acquire one.

bt-2026-09-09-024244 was cut off mid-op. Its summary.json says::

    "session_outcome": "in_flight", "stop_reason": "unknown"

and eight days later it was still "in flight" to anything reading the archive:
``_record_from_summary`` dropped ``session_outcome`` on the floor at ingest, and
``_merge_row`` did not carry ``outcome`` at all, so no later source could
supply one. The session had also produced a landing — 7f8c686ce0 on an unmerged
accumulation branch — that nothing associated with it.

Reconciliation is EVIDENCE-ONLY: the session's own declared wall deadline and
its own last heartbeat. It completes a snapshot; it never corrects one.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from backend.core.ouroboros.governance import session_archive as SA


def _session(tmp_path, sid, *, outcome="in_flight", cap_s=1800.0,
             deadline_offset=-86400.0, beat_before_deadline=802.0,
             extra=None):
    """A session directory shaped exactly like the harness writes one."""
    d = tmp_path / sid
    d.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + deadline_offset
    (d / "wall_deadline.json").write_text(json.dumps({
        "armed_wall": deadline - cap_s, "cap_s": cap_s, "deadline_wall": deadline,
    }), encoding="utf-8")
    if beat_before_deadline is not None:
        (d / "heartbeat.tick").write_text(
            str(deadline - beat_before_deadline), encoding="utf-8",
        )
    payload = {
        "session_id": sid, "session_outcome": outcome, "stop_reason": "unknown",
        "duration_s": 1036.2, "cost_total": 0.0,
        "last_activity_ts": deadline - (beat_before_deadline or 0),
    }
    payload.update(extra or {})
    (d / "summary.json").write_text(json.dumps(payload), encoding="utf-8")
    return d, payload


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    """The landing lookup is exercised in its own test, not in every one."""
    monkeypatch.setattr(SA, "_landing_commits", lambda sid: ())


# --------------------------------------------------------------------------
# The defect
# --------------------------------------------------------------------------

def test_a_cut_off_session_is_reconciled(tmp_path):
    """THE regression: in_flight forever, with the evidence sitting on disk."""
    d, payload = _session(tmp_path, "bt-2026-09-09-024244")
    rec = SA.SessionArchive._record_from_summary(
        session_id=d.name, payload=payload, session_dir=d,
    )
    assert rec.outcome == "abandoned"
    assert rec.stop_reason == "abandoned_before_deadline"
    assert "802s before its own 1800s wall deadline" in rec.notes


def test_a_session_that_ran_to_its_wall_is_named_differently(tmp_path):
    """Out of budget and cut off mean opposite things about a run."""
    d, payload = _session(tmp_path, "bt-wall", beat_before_deadline=-1.0)
    rec = SA.SessionArchive._record_from_summary(
        session_id=d.name, payload=payload, session_dir=d,
    )
    assert rec.outcome == "wall_clock_exhausted"


def test_a_recorded_outcome_is_never_overwritten(tmp_path):
    """A snapshot is completed here, never corrected — the session's own
    account of itself outranks anything derived from the outside."""
    d, payload = _session(
        tmp_path, "bt-owned", outcome="in_flight_shutdown_wal_second",
    )
    rec = SA.SessionArchive._record_from_summary(
        session_id=d.name, payload=payload, session_dir=d,
    )
    assert rec.outcome == "in_flight_shutdown_wal_second"
    assert "derived" not in rec.notes


def test_a_live_session_is_left_alone(tmp_path):
    """Before its own deadline it may genuinely still be running. Guessing
    would replace an honest 'unreconciled' with a confident wrong answer."""
    d, payload = _session(tmp_path, "bt-live", deadline_offset=+600.0)
    rec = SA.SessionArchive._record_from_summary(
        session_id=d.name, payload=payload, session_dir=d,
    )
    assert rec.outcome == "in_flight"


def test_no_evidence_yields_no_verdict(tmp_path):
    d = tmp_path / "bt-bare"
    d.mkdir()
    rec = SA.SessionArchive._record_from_summary(
        session_id="bt-bare", payload={"session_outcome": "in_flight"},
        session_dir=d,
    )
    assert rec.outcome == "in_flight"


def test_derivation_never_raises_on_junk(tmp_path):
    d = tmp_path / "bt-junk"
    d.mkdir()
    (d / "wall_deadline.json").write_text("{not json", encoding="utf-8")
    (d / "heartbeat.tick").write_text("banana", encoding="utf-8")
    assert SA._derive_terminal_state({"session_outcome": "in_flight"}, d) is None


# --------------------------------------------------------------------------
# The landing, bound to the session that produced it
# --------------------------------------------------------------------------

def test_the_landing_is_bound_to_its_session(tmp_path, monkeypatch):
    """Autonomous work lands on an unmerged accumulation branch; without this
    the index cannot tell a run that landed from one that produced nothing."""
    monkeypatch.setattr(SA, "_landing_commits", lambda sid: ("7f8c686ce0ea91c9",))
    d, payload = _session(tmp_path, "bt-2026-09-09-024244")
    rec = SA.SessionArchive._record_from_summary(
        session_id=d.name, payload=payload, session_dir=d,
    )
    assert "landed=7f8c686ce0ea" in rec.notes
    assert "derived:" in rec.notes, "the landing displaced the reconciliation note"


def test_landing_lookup_degrades_to_empty(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no git here")

    monkeypatch.setattr(SA.subprocess, "run", _boom)
    assert SA._landing_commits("bt-whatever") == ()


# --------------------------------------------------------------------------
# The merge half
# --------------------------------------------------------------------------

def _rows(db):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT session_id, outcome, notes FROM session_index",
        ).fetchall()
    finally:
        conn.close()


def test_merge_fills_an_empty_outcome(tmp_path):
    """``outcome`` was absent from the merge UPDATE entirely, so a row that
    arrived without one could never acquire one."""
    archive = SA.SessionArchive(db_path=tmp_path / "a.db")
    conn = archive._ensure_db()
    assert conn is not None
    try:
        SA.SessionArchive._upsert_row(conn, SA.SessionRecord(
            session_id="s1", outcome="", session_type="live_fire",
        ))
        SA.SessionArchive._merge_row(conn, SA.SessionRecord(
            session_id="s1", outcome="abandoned", notes="derived: x",
        ))
        conn.commit()
    finally:
        conn.close()
    assert _rows(tmp_path / "a.db") == [("s1", "abandoned", "derived: x")]


def test_merge_preserves_a_recorded_outcome(tmp_path):
    archive = SA.SessionArchive(db_path=tmp_path / "b.db")
    conn = archive._ensure_db()
    try:
        SA.SessionArchive._upsert_row(conn, SA.SessionRecord(
            session_id="s2", outcome="completed", session_type="live_fire",
        ))
        SA.SessionArchive._merge_row(conn, SA.SessionRecord(
            session_id="s2", outcome="abandoned",
        ))
        conn.commit()
    finally:
        conn.close()
    assert _rows(tmp_path / "b.db")[0][1] == "completed"


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    """Run twice, same answer — reconciliation must not accumulate."""
    monkeypatch.setenv("JARVIS_SESSION_ARCHIVE_ENABLED", "true")
    sessions = tmp_path / ".ouroboros" / "sessions"
    sessions.mkdir(parents=True)
    _session(sessions, "bt-x")
    monkeypatch.setattr(SA, "_sessions_root", lambda: sessions)

    archive = SA.SessionArchive(db_path=tmp_path / "c.db")
    first = archive.backfill()
    before = _rows(tmp_path / "c.db")
    archive.backfill()
    assert first >= 1
    assert _rows(tmp_path / "c.db") == before
