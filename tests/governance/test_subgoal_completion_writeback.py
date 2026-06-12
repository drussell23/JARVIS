"""§51.11.34-ROADMAP A1 — Sub-goal completion writeback (the severed feedback wire).

ROOT CAUSE this closes: the multi_step orchestrator emits sub-goal envelopes and
writes ``PROPOSED`` to the goal_decomposition completion ledger at EMIT time, but
NOTHING ever writes ``COMPLETED``/``FAILED`` back when the dispatched op reaches a
terminal phase. ``done_count`` (which counts ``completed`` rows) was therefore
STRUCTURALLY pinned at 0 — a roadmap sub-goal (e.g. GOAL-001::file-00) could
dispatch + succeed any number of times and the roadmap would never advance.

The fix wires the writeback into the orchestrator terminal hook
(``_slice12q_record_terminal``) — the same fail-soft, recorder-independent seam
the Slice-134 episodic synapse fires from. When ``ctx.intake_evidence_json``
carries a ``sub_goal_id`` + ``parent_goal_id`` (stamped by the multi_step emit
path), the terminal state is mapped to a CompletionStatus and appended to the
canonical completion ledger.

Gated by ``JARVIS_SUBGOAL_COMPLETION_WRITEBACK_ENABLED`` (default TRUE — this
closes a structural gap; OFF is byte-identical to the legacy severed loop).
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path


def _read_rows(ledger: Path):
    if not ledger.exists():
        return []
    out = []
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


class TestSubGoalCompletionWriteback(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ledger = Path(self._tmp.name) / "goal_decomposition_ledger.jsonl"
        self._saved = {k: os.environ.get(k) for k in (
            "JARVIS_GOAL_DECOMPOSITION_ENABLED",
            "JARVIS_GOAL_DECOMPOSITION_PERSIST_ENABLED",
            "JARVIS_GOAL_DECOMPOSITION_LEDGER_PATH",
            "JARVIS_SUBGOAL_COMPLETION_WRITEBACK_ENABLED",
        )}
        os.environ["JARVIS_GOAL_DECOMPOSITION_ENABLED"] = "1"
        os.environ["JARVIS_GOAL_DECOMPOSITION_PERSIST_ENABLED"] = "1"
        os.environ["JARVIS_GOAL_DECOMPOSITION_LEDGER_PATH"] = str(self._ledger)
        os.environ["JARVIS_SUBGOAL_COMPLETION_WRITEBACK_ENABLED"] = "1"

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _ctx(self, sub_goal_id="GOAL-001::file-00", parent="GOAL-001"):
        evidence = {"sub_goal_id": sub_goal_id, "parent_goal_id": parent,
                    "multi_step_orchestrated": True}
        return types.SimpleNamespace(
            op_id="op-1", terminal_reason_code="", provider_route="standard",
            intake_evidence_json=json.dumps(evidence),
        )

    def test_terminal_applied_writes_completed(self):
        from backend.core.ouroboros.governance import orchestrator as ORC
        ORC._slice12q_record_terminal(
            self._ctx(), types.SimpleNamespace(value="applied"),
            {"route": "standard"},
        )
        rows = _read_rows(self._ledger)
        done = [r for r in rows
                if r.get("sub_goal_id") == "GOAL-001::file-00"
                and r.get("status") == "completed"]
        self.assertTrue(done, f"expected a COMPLETED row; got {rows}")
        self.assertEqual(done[-1]["parent_goal_id"], "GOAL-001")

    def test_terminal_blocked_writes_failed(self):
        from backend.core.ouroboros.governance import orchestrator as ORC
        ORC._slice12q_record_terminal(
            self._ctx(), types.SimpleNamespace(value="blocked"), {},
        )
        rows = _read_rows(self._ledger)
        failed = [r for r in rows if r.get("status") == "failed"
                  and r.get("sub_goal_id") == "GOAL-001::file-00"]
        self.assertTrue(failed, f"expected a FAILED row; got {rows}")

    def test_no_subgoal_evidence_is_noop(self):
        """An op with no sub_goal_id provenance must NOT write to the ledger."""
        from backend.core.ouroboros.governance import orchestrator as ORC
        ctx = types.SimpleNamespace(
            op_id="op-2", terminal_reason_code="", provider_route="background",
            intake_evidence_json=json.dumps({"source": "opportunity_miner"}),
        )
        ORC._slice12q_record_terminal(ctx, types.SimpleNamespace(value="applied"), {})
        self.assertEqual(_read_rows(self._ledger), [])

    def test_disabled_is_noop(self):
        """Master OFF → byte-identical to the legacy severed loop."""
        os.environ["JARVIS_SUBGOAL_COMPLETION_WRITEBACK_ENABLED"] = "0"
        from backend.core.ouroboros.governance import orchestrator as ORC
        ORC._slice12q_record_terminal(
            self._ctx(), types.SimpleNamespace(value="applied"), {},
        )
        self.assertEqual(_read_rows(self._ledger), [])

    def test_fail_soft_never_raises(self):
        from backend.core.ouroboros.governance import orchestrator as ORC
        # Garbage ctx (no intake_evidence_json attr, malformed state) must not raise.
        ORC._slice12q_record_terminal(
            types.SimpleNamespace(op_id="x"), types.SimpleNamespace(value="applied"),
            None,
        )


if __name__ == "__main__":
    unittest.main()
