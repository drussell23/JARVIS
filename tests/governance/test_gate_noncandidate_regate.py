"""Anti-Venom Task 11 — GATE second pass: non-candidate venom-path re-gating.

Venom may write helper files during generation (via in-loop edit_file /
write_file) that are NOT part of the candidate's proposed changes.  Those
helper mutations bypass the first-pass guardian inspection which only looks
at candidate-declared paths.

The second pass (gate_runner.py lines added by Task 11) iterates
``_venom_paths – _candidate_path_set`` and runs ``inspect_batch`` on each
un-inspected venom-touched file, then applies the same tier-escalation logic.

These tests drive the real GATERunner using the established parity-test fakes.

Tests:
  1. test_noncandidate_hard_finding_escalates_to_approval_required
     - candidate=file_A, venom_edit_history also has file_B
     - guardian returns a hard finding for the file_B pair
     - risk_tier must escalate to APPROVAL_REQUIRED
  2. test_noncandidate_clean_no_false_escalation
     - candidate=file_A, venom_edit_history also has file_B
     - guardian returns no findings for either path
     - risk_tier stays SAFE_AUTO (no false escalation)
"""
from __future__ import annotations

import subprocess as _subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runners.gate_runner import GATERunner
from backend.core.ouroboros.governance.risk_engine import RiskTier
from backend.core.ouroboros.governance.semantic_guardian import Detection

# Reuse the established parity-test fakes (creates ctx + orch objects that
# drive the real GATERunner.run() through its full execution path).
from tests.governance.phase_runner import test_gate_runner_parity as _gp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_generation_with_venom_paths(*paths: str):
    """Return a SimpleNamespace mimicking ctx.generation with venom_edit_history."""
    return SimpleNamespace(
        venom_edit_history=tuple({"path": p, "action": "edit"} for p in paths),
    )


def _no_findings_inspect_batch(self, pairs):  # noqa: ANN001
    """inspect_batch stub that always returns an empty finding list."""
    return []


# ---------------------------------------------------------------------------
# Test 1 — non-candidate hard finding → APPROVAL_REQUIRED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noncandidate_hard_finding_escalates_to_approval_required(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A hard guardian finding on a venom-touched helper file (not part of the
    candidate) must escalate risk_tier to APPROVAL_REQUIRED.

    Setup:
      - candidate declares file_a.py
      - venom_edit_history also lists file_b.py (the non-candidate helper)
      - guardian returns a hard finding for ANY pair that contains file_b.py
    """
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    # Seed both files on disk so the gate_runner can read them.
    (tmp_path / "file_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "file_b.py").write_text("import os\nos.system('id')\n", encoding="utf-8")

    # Build ctx + orch via the parity helpers (GATE phase, SAFE_AUTO tier).
    ctx = _gp._gate_ctx(tmp_path)
    # Stamp venom edit history onto ctx.generation (both candidate + helper).
    gen = _make_generation_with_venom_paths("file_a.py", "file_b.py")
    object.__setattr__(ctx, "generation", gen)

    orch = _gp._orch(tmp_path)

    # Candidate only declares file_a.py.
    cand = {"candidate_id": "c0", "file_path": "file_a.py", "full_content": "x = 2\n"}

    call_log: list = []

    def _selective_inspect_batch(self, pairs):  # noqa: ANN001
        """Return a hard finding when file_b.py is in the pairs (second pass)."""
        call_log.append([p for p, _, _ in pairs])
        for path, old, new in pairs:
            if "file_b" in path:
                return [
                    Detection(
                        pattern="shell_exec_introduced",
                        severity="hard",
                        message="os.system introduced",
                        lines=(2,),
                        file_path=path,
                    )
                ]
        return []

    # git show HEAD:<path> → return the HEAD baseline for any path.
    _head = _subprocess.CompletedProcess(
        args=[], returncode=0, stdout="# head content\n", stderr="",
    )

    with patch(
        "backend.core.ouroboros.governance.semantic_guardian."
        "SemanticGuardian.inspect_batch",
        _selective_inspect_batch,
    ), patch(
        "backend.core.ouroboros.governance.phase_runners.gate_runner."
        "subprocess.run",
        return_value=_head,
    ):
        result = await GATERunner(orch, None, cand, RiskTier.SAFE_AUTO).run(ctx)

    assert result.status == "ok", f"Expected ok from GATE (not terminal), got: {result!r}"
    risk_after = result.artifacts["risk_tier"]
    assert risk_after is RiskTier.APPROVAL_REQUIRED, (
        f"Expected APPROVAL_REQUIRED after non-candidate hard finding, "
        f"got risk_tier={risk_after!r}"
    )
    # Guardian must have been called at least twice (first pass + second pass).
    assert len(call_log) >= 2, (
        f"Expected ≥2 guardian calls (candidate pass + noncandidate pass), "
        f"got {len(call_log)} calls: {call_log!r}"
    )


# ---------------------------------------------------------------------------
# Test 2 — non-candidate clean → no false escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_noncandidate_clean_no_false_escalation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A clean (no-findings) guardian result for non-candidate venom paths must
    NOT escalate the risk_tier (no false positives).

    Setup:
      - candidate declares file_a.py
      - venom_edit_history also has file_b.py
      - guardian returns [] for all pairs
      - Verify the second pass (non-candidate) actually ran
    """
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    (tmp_path / "file_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "file_b.py").write_text("y = 2\n", encoding="utf-8")

    ctx = _gp._gate_ctx(tmp_path)
    gen = _make_generation_with_venom_paths("file_a.py", "file_b.py")
    object.__setattr__(ctx, "generation", gen)

    orch = _gp._orch(tmp_path)
    cand = {"candidate_id": "c0", "file_path": "file_a.py", "full_content": "x = 2\n"}

    call_log: list = []

    def _tracking_clean_inspect_batch(self, pairs):  # noqa: ANN001
        """Track calls to inspect_batch and return no findings."""
        call_log.append([p for p, _, _ in pairs])
        return []

    _head = _subprocess.CompletedProcess(
        args=[], returncode=0, stdout="# head content\n", stderr="",
    )

    with patch(
        "backend.core.ouroboros.governance.semantic_guardian."
        "SemanticGuardian.inspect_batch",
        _tracking_clean_inspect_batch,
    ), patch(
        "backend.core.ouroboros.governance.phase_runners.gate_runner."
        "subprocess.run",
        return_value=_head,
    ):
        result = await GATERunner(orch, None, cand, RiskTier.SAFE_AUTO).run(ctx)

    assert result.status == "ok", f"Expected ok from GATE, got: {result!r}"
    risk_after = result.artifacts["risk_tier"]
    assert risk_after is RiskTier.SAFE_AUTO, (
        f"Expected SAFE_AUTO (no false escalation for clean non-candidate path), "
        f"got risk_tier={risk_after!r}"
    )
    # Strengthen: verify the second pass (non-candidate path) actually ran
    assert len(call_log) >= 2, (
        f"Expected ≥2 guardian calls (candidate pass + noncandidate pass), "
        f"got {len(call_log)} calls: {call_log!r}"
    )
