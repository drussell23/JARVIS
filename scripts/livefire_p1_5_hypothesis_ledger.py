#!/usr/bin/env python3
"""P1.5 — Hypothesis ledger — in-process live-fire smoke.

15 checks. Mirrors P0/P0.5/P1 live-fire patterns. Exercises the full
P1.5 chain WITHOUT requiring an Anthropic API key, a running battle-
test harness, or any network call.

  1.  HypothesisLedger primitive: construct + empty stats
  2.  Append + load round-trip preserves fields
  3.  make_hypothesis_id deterministic
  4.  Hypothesis frozen dataclass (mutation rejected)
  5.  Last-write-wins per hypothesis_id
  6.  REPL `/hypothesis ledger help` works (no master flag)
  7.  REPL `/hypothesis ledger list` empty / populated
  8.  Engine pairing default ON (post-graduation)
  9.  Engine pairing hot-revert: explicit false disables emission
  10. Engine emits paired hypothesis when pairing on
  11. ProposalDraft carries hypothesis_id
  12. Validator overlap math: high overlap → True
  13. Validator overlap math: low overlap → False, middle → None
  14. Validator records actual + decision back to ledger
  15. End-to-end: engine emit → validator decide → stats reflected

Exit 0 on PASS; non-zero on FAIL.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class Journal:
    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name, ok, detail=""):
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  ({detail})")

    def summary(self):
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 64}")
        print(f"Result: {len(self.passed)}/{total} checks passed")
        if self.failed:
            print("\nFailures:")
            for n, d in self.failed:
                print(f"  - {n}: {d}")
            return 1
        print("All checks passed — P1.5 Hypothesis ledger live-fire smoke OK.")
        return 0


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("P1.5 — Hypothesis ledger — live-fire smoke (PRD Phase 2)")
    print("=" * 64)

    for k in (
        "JARVIS_SELF_GOAL_FORMATION_ENABLED",
        "JARVIS_HYPOTHESIS_PAIRING_ENABLED",
        "JARVIS_SELF_GOAL_PER_SESSION_CAP",
    ):
        os.environ.pop(k, None)

    from backend.core.ouroboros.governance.hypothesis_ledger import (
        HYPOTHESIS_SCHEMA_VERSION, Hypothesis, HypothesisLedger,
        make_hypothesis_id, reset_default_ledger,
    )
    from backend.core.ouroboros.governance.hypothesis_repl import (
        dispatch_hypothesis_command as REPL,
    )
    from backend.core.ouroboros.governance.hypothesis_validator import (
        DEFAULT_OVERLAP_THRESHOLD, INVALIDATION_OVERLAP_THRESHOLD,
        classify, overlap_ratio, validate_hypothesis,
    )
    from backend.core.ouroboros.governance.postmortem_recall import (
        PostmortemRecord,
    )
    from backend.core.ouroboros.governance.posture import Posture
    from backend.core.ouroboros.governance.self_goal_formation import (
        SelfGoalFormationEngine, hypothesis_pairing_enabled,
        reset_default_engine,
    )
    reset_default_engine()
    reset_default_ledger()

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        ledger = HypothesisLedger(project_root=repo)

        # --- (1) Construct + empty stats
        s = ledger.stats()
        j.check(
            "1. Empty ledger stats {0,0,0,0}",
            s == {"total": 0, "open": 0, "validated": 0, "invalidated": 0},
            f"got {s}",
        )

        # --- (2) Append + load round-trip
        ts = time.time()
        h_id = make_hypothesis_id("op-001", "X causes Y", ts)
        h = Hypothesis(
            hypothesis_id=h_id, op_id="op-001",
            claim="X causes Y", expected_outcome="If Z then W",
            created_unix=ts, proposed_signature_hash="prop-abc",
        )
        ledger.append(h)
        rows = ledger.load_all()
        j.check(
            "2. Append + load round-trip preserves fields",
            (
                len(rows) == 1
                and rows[0].hypothesis_id == h_id
                and rows[0].claim == "X causes Y"
                and rows[0].proposed_signature_hash == "prop-abc"
            ),
            f"got {rows}",
        )

        # --- (3) Deterministic id
        a = make_hypothesis_id("op-1", "claim", 1.0)
        b = make_hypothesis_id("op-1", "claim", 1.0)
        c = make_hypothesis_id("op-1", "claim", 2.0)
        j.check(
            "3. make_hypothesis_id deterministic + distinct on different ts",
            a == b and a != c and len(a) == 12,
        )

        # --- (4) Frozen dataclass
        try:
            h.claim = "mutated"  # type: ignore[misc]
            j.check("4. Hypothesis frozen", False, "mutation succeeded")
        except Exception:
            j.check("4. Hypothesis frozen", True)

        # --- (5) Last-write-wins
        h_updated = Hypothesis(
            hypothesis_id=h_id, op_id="op-001",
            claim="X causes Y", expected_outcome="If Z then W",
            created_unix=ts, actual_outcome="W happened",
            validated=True, validated_unix=ts + 100,
        )
        ledger.append(h_updated)
        rows = ledger.load_all()
        j.check(
            "5. Last-write-wins per hypothesis_id",
            len(rows) == 1 and rows[0].is_validated()
            and rows[0].actual_outcome == "W happened",
            f"got {len(rows)} rows; validated={rows[0].validated}",
        )

        # --- (6) REPL help
        r = REPL("/hypothesis ledger help", project_root=repo, ledger=ledger)
        j.check(
            "6. REPL `/hypothesis ledger help` works",
            r.ok and "show" in r.text,
        )

        # --- (7) REPL list populated
        r = REPL("/hypothesis ledger list", project_root=repo, ledger=ledger)
        j.check(
            "7. REPL list shows the row",
            r.ok and h_id[:12] in r.text,
        )

        # Reset for engine tests
        reset_default_ledger()

    # --- (8) Engine pairing default ON
    j.check(
        "8. Engine pairing default ON",
        hypothesis_pairing_enabled() is True,
    )

    # --- (9) Hot-revert
    os.environ["JARVIS_HYPOTHESIS_PAIRING_ENABLED"] = "false"
    j.check(
        "9. Engine pairing hot-revert disables",
        hypothesis_pairing_enabled() is False,
    )
    os.environ.pop("JARVIS_HYPOTHESIS_PAIRING_ENABLED", None)

    # --- (10/11) Engine emits paired hypothesis
    os.environ["JARVIS_SELF_GOAL_FORMATION_ENABLED"] = "true"
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        eng = SelfGoalFormationEngine(
            project_root=repo,
            ledger_path=repo / ".jarvis" / "proposals.jsonl",
        )

        def stub(p, m):
            return (json.dumps({
                "description": "Investigate provider exhaustion",
                "rationale": "3 ops failed",
                "claim": "X causes Y in fallback",
                "expected_outcome": "After patch exhaustion rate drops",
            }), 0.05)

        recs = [
            PostmortemRecord(
                op_id=f"op{i}", session_id="s1",
                root_cause="all_providers_exhausted:fallback_failed",
                failed_phase="GENERATE",
                next_safe_action="retry_with_smaller_seed",
                target_files=("a.py",), timestamp_iso="2026-04-26T10:00:00",
                timestamp_unix=1700000000.0 + i * 3600.0,
            )
            for i in range(3)
        ]
        draft = eng.evaluate(
            postmortems=recs, posture=Posture.EXPLORE, model_caller=stub,
        )
        hl = HypothesisLedger(project_root=repo)
        rows = hl.load_all()
        j.check(
            "10. Engine emits paired hypothesis when pairing on",
            draft is not None and len(rows) == 1
            and rows[0].claim.startswith("X causes Y"),
            f"draft.hypothesis_id={draft.hypothesis_id if draft else None}",
        )
        j.check(
            "11. ProposalDraft carries hypothesis_id",
            draft is not None and draft.hypothesis_id is not None
            and len(draft.hypothesis_id) == 12,
            f"hid={draft.hypothesis_id if draft else None}",
        )

        # --- (14) Validator records back to ledger
        result = validate_hypothesis(
            draft.hypothesis_id,
            "After patch landed exhaustion rate dropped 60 percent",
            ledger=hl,
        )
        h_after = hl.find_by_id(draft.hypothesis_id)
        j.check(
            "14. Validator records actual + decision back to ledger",
            (
                h_after.is_validated()
                and h_after.actual_outcome.startswith("After patch")
            ),
            f"validated={h_after.validated}",
        )

        # --- (15) End-to-end stats
        s = hl.stats()
        j.check(
            "15. End-to-end: stats reflect validated",
            s == {"total": 1, "open": 0, "validated": 1, "invalidated": 0},
            f"got {s}",
        )

    # --- (12/13) Validator math
    high = overlap_ratio(
        "exhaustion rate drops below percent",
        "exhaustion rate dropped percent below five",
    )
    j.check(
        "12. Validator high-overlap → True",
        classify(
            "exhaustion rate drops below percent",
            "exhaustion rate dropped percent below five",
        ) is True and high >= DEFAULT_OVERLAP_THRESHOLD,
        f"overlap={high}",
    )

    low = overlap_ratio(
        "exhaustion rate drops below five percent",
        "completely unrelated zzz qqq",
    )
    middle = overlap_ratio(
        "alpha beta gamma delta epsilon",
        "alpha beta zeta eta",
    )
    j.check(
        "13. Validator low → False, middle → None",
        (
            classify(
                "exhaustion rate drops below five percent",
                "completely unrelated zzz qqq",
            ) is False
            and classify(
                "alpha beta gamma delta epsilon",
                "alpha beta zeta eta",
            ) is None
        ),
        f"low={low}, middle={middle}",
    )

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
