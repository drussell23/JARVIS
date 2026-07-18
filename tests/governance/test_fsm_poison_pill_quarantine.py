"""Poison-Pill Quarantine — deterministic survival of a crash-on-resume checkpoint.

A checkpoint that deterministically crashes the pipeline when hydrated would loop
forever (hydrate → crash → reboot → hydrate the SAME checkpoint → crash …). This
is NOT solved by a generic try/except (which swallows the fault and re-attempts
anyway) but by a DETERMINISTIC counter: ``hydration_attempt_count`` is incremented
+ re-signed on disk BEFORE each resume, so the increment survives a hard crash
mid-attempt. Past the ceiling (default 3) the checkpoint is shunted to a
quarantine DLQ — reusing the SAME move-to-subdir + HMAC-signed-envelope pattern
as ``expired/`` (no novel DLQ schema) — and the loop proceeds to the next op.

These tests pin: the count rides inside the HMAC payload (tamper-evident), it
increments across simulated reboots (re-read from disk), a poison pill that
raises during processing is quarantined on the threshold breach, and the DLQ is
tagged with the ``quarantined`` state vector while the pending set is cleared.
"""
from __future__ import annotations

import json
import os

import pytest

from backend.core.ouroboros.governance import fsm_checkpoint as CK


@pytest.fixture()
def base(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_FSM_HYDRATION_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("JARVIS_CHECKPOINT_HMAC_SECRET", raising=False)
    return str(tmp_path)


def _write(base, op_id="op-x", phase="GENERATE", count=0):
    cp = CK.FSMCheckpoint(
        op_id=op_id, phase=phase, goal_description="g", target_files=["a.py"],
        hydration_attempt_count=count,
    )
    CK.write_checkpoint(cp, base_dir=base)
    return cp


# ---------------------------------------------------------------------------
# The counter rides inside the HMAC-signed payload (tamper-evident)
# ---------------------------------------------------------------------------


def test_hydration_count_persists_and_is_hmac_signed(base):
    _write(base, count=0)
    cp = CK.list_pending(base_dir=base)[0]
    assert cp.hydration_attempt_count == 0
    CK.record_hydration_attempt(cp, base_dir=base)
    # Re-read from disk (a fresh "reboot") — the count survived.
    cp2 = CK.list_pending(base_dir=base)[0]
    assert cp2.hydration_attempt_count == 1
    # It is inside the signed payload: hand-tamper the count → HMAC verify fails
    # → the checkpoint is dropped from pending (never resumed with a forged count).
    path = os.path.join(CK.checkpoint_dir(base), "op-x.json")
    raw = json.loads(open(path).read())
    payload = json.loads(raw["payload"])
    payload["hydration_attempt_count"] = 0            # forge the count back down
    raw["payload"] = json.dumps(payload, sort_keys=True)
    open(path, "w").write(json.dumps(raw))            # same hmac, tampered payload
    assert CK.list_pending(base_dir=base) == []       # HMAC rejects the forgery


def test_count_increments_across_simulated_reboots(base):
    _write(base, count=0)
    for expected in (1, 2, 3):
        cp = CK.list_pending(base_dir=base)[0]        # fresh read each "boot"
        assert CK.record_hydration_attempt(cp, base_dir=base) == expected


# ---------------------------------------------------------------------------
# THE poison pill — corrupted payload raises during processing → quarantine
# ---------------------------------------------------------------------------


def _router_hydrate_pass(base, *, reinject):
    """Mirror the router's per-checkpoint hydrate decision: increment BEFORE the
    attempt; quarantine past the ceiling; else re-inject (which may raise)."""
    resumed, quarantined = [], []
    for cp in CK.list_pending(base_dir=base):
        attempt = CK.record_hydration_attempt(cp, base_dir=base)
        if attempt > CK.hydration_max_attempts():
            CK.quarantine_checkpoint(cp, reason="hydration_attempts_exhausted", base_dir=base)
            quarantined.append(cp.op_id)
            continue
        try:
            reinject(cp)                              # the "processing" step
            CK.mark_resumed(cp.op_id, base_dir=base)
            resumed.append(cp.op_id)
        except Exception:                             # noqa: BLE001 — left pending, retried next boot
            pass
    return resumed, quarantined


def test_poison_pill_quarantined_on_threshold_breach(base):
    _write(base, op_id="op-poison", count=0)

    def _always_crashes(cp):
        raise RuntimeError("deterministic crash on resume (poison pill)")

    # 3 reboots each re-attempt and crash (left pending); the 4th quarantines.
    for boot in (1, 2, 3):
        resumed, quarantined = _router_hydrate_pass(base, reinject=_always_crashes)
        assert resumed == [] and quarantined == []
        assert len(CK.list_pending(base_dir=base)) == 1   # still pending

    resumed, quarantined = _router_hydrate_pass(base, reinject=_always_crashes)
    assert quarantined == ["op-poison"]               # shunted to DLQ
    assert CK.list_pending(base_dir=base) == []        # no longer resumed

    dlq = CK.list_quarantined(base_dir=base)
    assert [c.op_id for c in dlq] == ["op-poison"]
    tag = dlq[0].trace_lineage
    assert tag.get("quarantined") is True
    assert tag.get("quarantine_reason") == "hydration_attempts_exhausted"


def test_healthy_op_resumes_and_never_quarantines(base):
    _write(base, op_id="op-ok", count=0)
    resumed, quarantined = _router_hydrate_pass(base, reinject=lambda cp: None)
    assert resumed == ["op-ok"] and quarantined == []
    assert CK.list_pending(base_dir=base) == []        # consumed on success
    assert CK.list_quarantined(base_dir=base) == []


def test_one_poison_pill_does_not_block_the_next_op(base):
    # Pipeline survival: a poisoned op is quarantined while a healthy one resumes.
    _write(base, op_id="op-poison", count=CK.hydration_max_attempts())  # one more = breach
    _write(base, op_id="op-healthy", count=0)

    def _selective(cp):
        if cp.op_id == "op-poison":
            raise RuntimeError("crash")

    resumed, quarantined = _router_hydrate_pass(base, reinject=_selective)
    assert "op-poison" in quarantined                  # DLQ'd
    assert "op-healthy" in resumed                      # NOT blocked
    assert CK.list_quarantined(base_dir=base)[0].op_id == "op-poison"


# ---------------------------------------------------------------------------
# Config + robustness
# ---------------------------------------------------------------------------


def test_max_attempts_env_tunable(base, monkeypatch):
    monkeypatch.setenv("JARVIS_FSM_HYDRATION_MAX_ATTEMPTS", "1")
    assert CK.hydration_max_attempts() == 1
    _write(base, op_id="op-strict", count=0)
    # attempt 1 (<=1 → re-inject), attempt 2 (>1 → quarantine)
    _router_hydrate_pass(base, reinject=lambda cp: (_ for _ in ()).throw(RuntimeError()))
    _, quarantined = _router_hydrate_pass(base, reinject=lambda cp: None)
    assert quarantined == ["op-strict"]


@pytest.mark.parametrize("bad", ["", "0", "-3", "abc"])
def test_max_attempts_invalid_falls_back_to_3(base, monkeypatch, bad):
    monkeypatch.setenv("JARVIS_FSM_HYDRATION_MAX_ATTEMPTS", bad)
    assert CK.hydration_max_attempts() == 3


def test_quarantine_never_raises_on_bad_dir(monkeypatch):
    # A write failure must degrade, not raise into the hydrate loop.
    cp = CK.FSMCheckpoint(op_id="op-x", phase="GENERATE")
    monkeypatch.setattr(CK, "quarantine_dir", lambda base_dir=None: "/proc/nonexistent/x")
    assert CK.quarantine_checkpoint(cp, reason="x", base_dir=None) is None


def test_record_attempt_never_raises_on_write_failure(monkeypatch):
    cp = CK.FSMCheckpoint(op_id="op-x", phase="GENERATE", hydration_attempt_count=2)
    monkeypatch.setattr(CK, "write_checkpoint", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    # In-memory increment still monotonic even if persistence fails.
    assert CK.record_hydration_attempt(cp) == 3
