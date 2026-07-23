"""Bulletproof spine for the AST-Aware Checkpoint Intent Engine.

Mandated assertions, all against the REAL substrate (real SQLite soak_intent_queue
on a temp file, real AST walk of real files on disk):

  (1) ``/enqueue_soak`` generates the correct JSON manifest of pending chunks,
  (2) simulating the Swarm completing one file correctly moves its hash to the
      ``completed_chunks`` array in SQLite, and
  (3) forcefully restarting the mock Swarm bypasses the completed hash and
      processes ONLY the remaining pending tasks — with no duplication.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from backend.core.ouroboros.governance.checkpoint_manifest import (
    build_manifest,
    completed_ids,
    enqueue_soak_manifest,
    mark_chunk_complete,
    read_manifest,
    resume_pending,
    run_checkpointed,
    serialize_manifest,
    walk_target,
)
from backend.core.ouroboros.governance.enqueue_soak_repl import (
    dispatch_enqueue_soak_command,
)
from backend.core.ouroboros.governance.soak_intent import get_manifest_json


@pytest.fixture
def project(tmp_path):
    """A tiny multi-file target with known top-level AST symbols."""
    d = tmp_path / "proj"
    d.mkdir()
    (d / "alpha.py").write_text(
        "def a_one():\n    return 1\n\n\ndef a_two():\n    return 2\n"
    )
    (d / "beta.py").write_text(
        "class B:\n    def m(self):\n        return 3\n"
    )
    return str(d)


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "chunk_strategy.db")


# ---------------------------------------------------------------------------
# (1) manifest generation
# ---------------------------------------------------------------------------


async def test_enqueue_generates_correct_manifest(project, db_path):
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    conn.close()

    assert res is not None
    # 3 top-level symbols: a_one, a_two (alpha.py) + B (beta.py).
    assert res["chunk_count"] == 3
    manifest = res["manifest"]
    assert manifest["schema_version"] == 2
    assert len(manifest["pending_chunks"]) == 3
    assert manifest["completed_chunks"] == []
    symbols = sorted(c["symbol"] for c in manifest["pending_chunks"])
    assert symbols == ["B", "a_one", "a_two"]
    # Every chunk carries a deterministic hash + line range.
    for c in manifest["pending_chunks"]:
        assert c["chunk_id"] and c["file_path"].endswith(".py")
        assert c["start_line"] >= 1 and c["end_line"] >= c["start_line"]

    # The manifest was written verbatim into the intent row (JSON, parseable).
    conn = sqlite3.connect(db_path)
    raw = get_manifest_json(conn, res["intent_id"])
    conn.close()
    assert raw is not None
    assert json.loads(raw)["schema_version"] == 2


async def test_manifest_serialization_is_deterministic(project):
    chunks = await walk_target(project)
    m1 = build_manifest(project, chunks, now=1000.0)
    m2 = build_manifest(project, chunks, now=1000.0)
    assert serialize_manifest(m1) == serialize_manifest(m2)


async def test_enqueue_soak_verb_writes_manifest(project, db_path, monkeypatch):
    """The /enqueue_soak REPL verb (naming-cage) walks + writes the manifest."""
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.dw_outage_forecaster.open_forecast_db",
        lambda path=None: sqlite3.connect(db_path),
    )
    out = await dispatch_enqueue_soak_command(f"/enqueue_soak {project}")
    assert out.ok and out.matched
    assert "soak intent queued" in out.text
    # It really landed a pending manifest row.
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT manifest_json FROM soak_intent_queue WHERE status='pending'"
    ).fetchall()
    conn.close()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["schema_version"] == 2


# ---------------------------------------------------------------------------
# (2) atomic per-chunk commit
# ---------------------------------------------------------------------------


async def test_commit_moves_hash_to_completed(project, db_path):
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    first = res["manifest"]["pending_chunks"][0]["chunk_id"]

    ok = mark_chunk_complete(conn, iid, first, result="stitched")
    assert ok is True

    manifest = read_manifest(conn, iid)
    conn.close()
    assert first in completed_ids(manifest)
    assert first not in {c["chunk_id"] for c in manifest["pending_chunks"]}
    assert len(manifest["completed_chunks"]) == 1
    assert manifest["completed_chunks"][0]["result"] == "stitched"


async def test_commit_is_idempotent(project, db_path):
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    cid = res["manifest"]["pending_chunks"][0]["chunk_id"]

    assert mark_chunk_complete(conn, iid, cid) is True
    # Re-committing the same hash is a durable no-op success, not a duplicate.
    assert mark_chunk_complete(conn, iid, cid) is True
    manifest = read_manifest(conn, iid)
    conn.close()
    assert len(manifest["completed_chunks"]) == 1  # no duplication
    # An unknown hash is rejected.
    conn = sqlite3.connect(db_path)
    assert mark_chunk_complete(conn, iid, "deadbeefdeadbeef") is False
    conn.close()


# ---------------------------------------------------------------------------
# (3) resume-from-hash — crash then restart, no duplication
# ---------------------------------------------------------------------------


async def test_restart_resumes_without_duplication(project, db_path):
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    conn.close()

    processed_run1: list = []

    async def swarm_run1(chunk):
        processed_run1.append(chunk["chunk_id"])
        # Simulate a CRASH after the first file: raise so the runner stops
        # committing further, but the first commit is already durable.
        if len(processed_run1) == 1:
            return "ok"           # first commits
        raise RuntimeError("simulated power loss")

    conn = sqlite3.connect(db_path)
    s1 = await run_checkpointed(conn, iid, swarm_run1)
    conn.close()
    # First chunk committed; the rest failed (stay pending).
    assert len(s1.processed) == 1
    assert len(s1.failed) >= 1
    committed_after_crash = set(s1.processed)

    # --- FORCEFUL RESTART: a fresh runner over the SAME intent row ---
    processed_run2: list = []

    async def swarm_run2(chunk):
        processed_run2.append(chunk["chunk_id"])
        return "ok"

    conn = sqlite3.connect(db_path)
    manifest_before = read_manifest(conn, iid)
    s2 = await run_checkpointed(conn, iid, swarm_run2)
    manifest_after = read_manifest(conn, iid)
    conn.close()

    # The already-completed hash is STRUCTURALLY bypassed — never reprocessed.
    for cid in committed_after_crash:
        assert cid not in processed_run2, "completed hash must not be reprocessed"
    # Run 2 processed exactly the remainder (3 total − 1 already done = 2).
    assert len(processed_run2) == 2
    assert set(s2.skipped) == committed_after_crash
    # Every chunk is now completed exactly once — no duplication anywhere.
    all_completed = [c["chunk_id"] for c in manifest_after["completed_chunks"]]
    assert len(all_completed) == 3
    assert len(set(all_completed)) == 3
    assert manifest_after["pending_chunks"] == []
    # Sanity: the pre-restart manifest already had the crash-survivor durable.
    assert committed_after_crash.issubset(completed_ids(manifest_before))


async def test_checkpoint_progress_surfaces_on_broker(project, db_path):
    """The map-reduce immortality is VISIBLE: enqueue + run emit the progress
    events (manifest / resume / per-chunk / run-complete) onto the real broker,
    so the operator watches it tick live in /breadcrumbs."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        reset_default_broker,
    )

    reset_default_broker()
    broker = get_default_broker()
    sub = broker.subscribe()
    seen: list = []

    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]

    async def swarm(chunk):
        return "ok"

    await run_checkpointed(conn, iid, swarm)
    conn.close()

    # Drain what the broker queued (subscribe() captured from enqueue onward).
    for _ in range(200):
        ev = None
        try:
            ev = sub.queue.get_nowait()
        except Exception:  # noqa: BLE001 — empty
            break
        seen.append(getattr(ev, "event_type", ""))
    broker.unsubscribe(sub)
    reset_default_broker()

    assert "soak_manifest_enqueued" in seen
    assert "soak_resumed" in seen
    assert seen.count("soak_chunk_committed") == 3   # one tick per chunk
    assert "soak_run_complete" in seen


async def test_poison_chunk_quarantined_after_3_strikes(project, db_path):
    """AST DLQ: a chunk that consistently fails is struck out after 3 attempts
    (across resumes) and moved to quarantined_chunks — never retried forever."""
    from backend.core.ouroboros.governance.checkpoint_manifest import quarantined_ids

    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    conn.close()

    ids = [c["chunk_id"] for c in res["manifest"]["pending_chunks"]]
    poison = ids[0]                       # this one always fails
    healthy = set(ids[1:])

    async def swarm(chunk):
        if chunk["chunk_id"] == poison:
            raise RuntimeError("context window blown")   # poison payload
        return "ok"

    # Three resumes (reboots). The healthy chunks commit on run 1; the poison
    # chunk strikes once per run and is DLQ'd on the 3rd.
    for run in range(3):
        conn = sqlite3.connect(db_path)
        summary = await run_checkpointed(conn, iid, swarm)
        manifest = read_manifest(conn, iid)
        conn.close()
        if run < 2:
            assert poison in summary.failed          # still pending, striking
            assert poison not in quarantined_ids(manifest)
        else:
            assert poison in summary.quarantined     # 3rd strike → DLQ
            assert poison in quarantined_ids(manifest)

    # Healthy chunks all completed exactly once; poison is quarantined, not lost.
    manifest = read_manifest(sqlite3.connect(db_path), iid)
    assert {c["chunk_id"] for c in manifest["completed_chunks"]} == healthy
    assert quarantined_ids(manifest) == {poison}
    assert manifest["chunk_retry_counts"][poison] == 3
    # The poison chunk carries its forensics in the DLQ.
    q = manifest["quarantined_chunks"][0]
    assert q["strikes"] == 3 and "context window blown" in q["last_error"]


async def test_resume_skips_quarantined_instantly(project, db_path):
    """Skip-and-Report: once quarantined, a poison chunk is structurally bypassed
    on every subsequent resume — the swarm moves straight to pending work."""
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    conn.close()
    poison = res["manifest"]["pending_chunks"][0]["chunk_id"]

    attempts: list = []

    async def swarm(chunk):
        attempts.append(chunk["chunk_id"])
        if chunk["chunk_id"] == poison:
            raise RuntimeError("poison")
        return "ok"

    for _ in range(3):                       # drive it to quarantine
        conn = sqlite3.connect(db_path)
        await run_checkpointed(conn, iid, swarm)
        conn.close()

    attempts.clear()
    # A fresh resume AFTER quarantine must never touch the poison chunk again.
    conn = sqlite3.connect(db_path)
    summary = await run_checkpointed(conn, iid, swarm)
    conn.close()
    assert poison not in attempts, "quarantined chunk must be structurally skipped"
    assert attempts == []                    # everything else was already done
    # Final ratio is reportable: 2 completed, 1 quarantined.
    manifest = read_manifest(sqlite3.connect(db_path), iid)
    assert len(manifest["completed_chunks"]) == 2
    assert len(manifest["quarantined_chunks"]) == 1


async def test_quarantine_ratio_surfaces_on_broker(project, db_path):
    """The final ratio ('N completed, M quarantined') + the DLQ event reach the
    broker → visible in /breadcrumbs."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        reset_default_broker,
    )

    reset_default_broker()
    broker = get_default_broker()
    sub = broker.subscribe()

    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    poison = res["manifest"]["pending_chunks"][0]["chunk_id"]
    conn.close()

    async def swarm(chunk):
        if chunk["chunk_id"] == poison:
            raise RuntimeError("poison")
        return "ok"

    for _ in range(3):
        conn = sqlite3.connect(db_path)
        await run_checkpointed(conn, iid, swarm)
        conn.close()

    seen = []
    complete_payloads = []
    for _ in range(500):
        try:
            ev = sub.queue.get_nowait()
        except Exception:  # noqa: BLE001
            break
        et = getattr(ev, "event_type", "")
        seen.append(et)
        if et == "soak_run_complete":
            complete_payloads.append(getattr(ev, "payload", {}) or {})
    broker.unsubscribe(sub)
    reset_default_broker()

    assert "soak_chunk_quarantined" in seen
    assert any(p.get("quarantined") == 1 and p.get("processed", 0) >= 0
               for p in complete_payloads), "run-complete carries the quarantine ratio"


async def test_resume_pending_skips_completed(project, db_path):
    conn = sqlite3.connect(db_path)
    res = await enqueue_soak_manifest(conn, project, priority=1)
    iid = res["intent_id"]
    ids = [c["chunk_id"] for c in res["manifest"]["pending_chunks"]]
    mark_chunk_complete(conn, iid, ids[0])
    manifest = read_manifest(conn, iid)
    conn.close()
    remaining = {c["chunk_id"] for c in resume_pending(manifest)}
    assert ids[0] not in remaining
    assert remaining == set(ids[1:])
