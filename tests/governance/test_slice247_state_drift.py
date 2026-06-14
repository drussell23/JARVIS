"""Slice 247 — State-Drift Reconciliation & Dynamic Context Alignment.

A preempted/resurrected GOAL (Slices 245/246) can awaken to a DRIFTED target: a
human override may have patched the exact file it was about to modify. Blindly
applying its pre-computed candidate against the new disk state = AST/line-number
corruption.

Verify-first finding: the hash machinery already exists.
  * Phase 1 (capture) — generate_runner snapshots sha256 of every target file at
    GENERATE entry into ctx.generate_file_hashes, and that snapshot RIDES ALONG
    on the preserved context through preemption (245/246). No new capture needed.
  * Phase 2 (compare) — a zero-LLM hash compare already exists at APPLY
    (orchestrator), but it was LOG-ONLY and blindly applied the stale candidate.
  * Phase 3 (re-align) — GENUINELY NEW: this slice.

Fix: a resurrected op re-runs GENERATE (preemption fires in the tool loop). At
GENERATE entry, BEFORE the fresh re-snapshot erases the baseline, compare the
PRESERVED hashes vs current disk. On drift, inject a RE-ALIGNMENT instruction
(STATE=CONTEXT_DRIFTED) forcing the model to re-read the drifted files before it
regenerates — never blind-patch a drifted target. Zero-LLM detection; reuses the
existing hash field + the strategic_memory_prompt injection channel.
"""
from __future__ import annotations

import hashlib
import inspect

import pytest

from backend.core.ouroboros.governance import state_drift as sd


def _write(p, content: str) -> str:
    p.write_text(content)
    return hashlib.sha256(content.encode()).hexdigest()


class TestDetectDrift:
    def test_no_drift_when_hashes_match(self, tmp_path):
        h = _write(tmp_path / "a.py", "print('hello')\n")
        assert sd.detect_drift([("a.py", h)], tmp_path) == []

    def test_drift_when_file_mutated(self, tmp_path):
        h = _write(tmp_path / "a.py", "print('hello')\n")
        # human override rewrites the file
        (tmp_path / "a.py").write_text("print('HUMAN PATCHED')\n")
        assert sd.detect_drift([("a.py", h)], tmp_path) == ["a.py"]

    def test_new_file_empty_hash_skipped(self, tmp_path):
        # empty baseline hash = file didn't exist at GENERATE → not drift
        (tmp_path / "new.py").write_text("x = 1\n")
        assert sd.detect_drift([("new.py", "")], tmp_path) == []

    def test_missing_file_skipped(self, tmp_path):
        # file deleted since snapshot — a different problem, not drift
        assert sd.detect_drift([("gone.py", "deadbeef")], tmp_path) == []

    def test_multiple_mixed(self, tmp_path):
        ha = _write(tmp_path / "a.py", "a\n")
        hb = _write(tmp_path / "b.py", "b\n")
        (tmp_path / "a.py").write_text("a-CHANGED\n")  # drifted
        out = sd.detect_drift([("a.py", ha), ("b.py", hb)], tmp_path)
        assert out == ["a.py"]

    def test_never_raises_on_bad_input(self, tmp_path):
        assert sd.detect_drift(None, tmp_path) == []
        assert sd.detect_drift([("x", "h")], None) == []


class TestRealignmentFeedback:
    def test_names_files_and_forces_reread(self):
        fb = sd.build_realignment_feedback(["app.py", "core/db.py"])
        assert "app.py" in fb and "core/db.py" in fb
        assert "read_file" in fb  # must instruct the model to re-read
        assert fb.isascii(), "Iron Gate ASCII-strictness — feedback must be ASCII"

    def test_empty_is_empty(self):
        assert sd.build_realignment_feedback([]) == ""


class TestGate:
    def test_enabled_default_true(self, monkeypatch):
        monkeypatch.delenv("JARVIS_STATE_DRIFT_RECONCILE_ENABLED", raising=False)
        assert sd.state_drift_reconcile_enabled() is True

    def test_kill_switch(self, monkeypatch):
        monkeypatch.setenv("JARVIS_STATE_DRIFT_RECONCILE_ENABLED", "0")
        assert sd.state_drift_reconcile_enabled() is False


class TestWiring:
    def test_generate_runner_realigns_before_resnapshot(self):
        from backend.core.ouroboros.governance.phase_runners import generate_runner as gr
        src = inspect.getsource(gr)
        assert "detect_drift" in src, "GENERATE entry must run the drift validator"
        assert "build_realignment_feedback" in src, "must inject re-read feedback on drift"
        assert "CONTEXT_DRIFTED" in src
        # the compare MUST precede the re-snapshot, else the baseline is erased
        i_detect = src.find("detect_drift(")
        i_snapshot = src.find("generate_file_hashes=tuple(")
        assert 0 < i_detect < i_snapshot, "drift compare must run BEFORE the re-snapshot"

    def test_orchestrator_apply_seam_reuses_detector(self):
        from backend.core.ouroboros.governance import orchestrator as orch
        src = inspect.getsource(orch)
        assert "detect_drift" in src, "the APPLY stale-guard should reuse the shared detector"


class TestPhase4Integration:
    def test_suspend_mutate_reingest_realigns(self, tmp_path):
        """End-to-end (component): capture baseline hash of target_file.py (the
        suspended GOAL's snapshot) → human override mutates it → the validator
        catches the mismatch and produces re-read feedback naming the file, so
        the regeneration aligns to the NEW state instead of blind-patching."""
        target = tmp_path / "target_file.py"
        baseline = _write(target, "def f():\n    return 1\n")
        snapshot = [("target_file.py", baseline)]

        # ── human override mutates the target during the suspension window ──
        target.write_text("def f():\n    return 999  # human change\n    # extra line\n")

        drifted = sd.detect_drift(snapshot, tmp_path)
        assert drifted == ["target_file.py"], "drift validator must catch the mismatch"

        feedback = sd.build_realignment_feedback(drifted)
        assert "target_file.py" in feedback
        assert "read_file" in feedback
        # a non-drift control: re-snapshotting the NEW state shows alignment
        new_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        assert sd.detect_drift([("target_file.py", new_hash)], tmp_path) == []
