#!/usr/bin/env python3
"""P0.5 — Cross-session direction memory live-fire smoke (PRD §11 Layer 3).

Mirrors ``scripts/livefire_p0_postmortem_recall.py``. In-process exercise
of the full P0.5 chain WITHOUT requiring an Anthropic API key, a running
battle-test harness, or any network call. Validates the post-graduation
contract end-to-end.

15 checks per the W2(4) curiosity / W3(6) parallel / P0 PostmortemRecall
live-fire pattern:

 1.  Master flag default ON (post-graduation)
 2.  Master flag explicit "false" hot-revert disables application
 3.  MAX_NUDGE_PER_POSTURE constant unchanged at 0.10
 4.  ArcContextSignal frozen (mutation rejected)
 5.  build_arc_context returns a complete signal on full inputs
 6.  build_arc_context tolerates missing git (LSS-only signal)
 7.  build_arc_context tolerates missing LSS (momentum-only signal)
 8.  is_empty() True when neither input present
 9.  Bounded-nudge math: feat-dominance routes to EXPLORE within cap
 10. Inferrer with flag ON applies nudge to scores
 11. Inferrer with flag OFF (hot-revert) does NOT apply nudge
 12. Strong-EXPLORE bundle stays EXPLORE under max-HARDEN arc nudge
 13. PostureReading carries arc_context through (observability)
 14. PostureReading.to_dict() emits arc_context section when present
 15. /posture explain renders Arc Context section when reading carries it

Exit 0 on PASS; non-zero with failed-check summary on FAIL.

Usage::

    python3 scripts/livefire_p0_5_arc_context.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class Journal:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  ({detail})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 64}")
        print(f"Result: {len(self.passed)}/{total} checks passed")
        if self.failed:
            print("\nFailures:")
            for n, d in self.failed:
                print(f"  - {n}: {d}")
            return 1
        print("All checks passed — P0.5 arc-context live-fire smoke OK.")
        return 0


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("P0.5 — Cross-session direction memory live-fire smoke")
    print("=" * 64)

    # Reset env for deterministic defaults
    os.environ.pop("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", None)

    from backend.core.ouroboros.governance.arc_context import (
        MAX_NUDGE_PER_POSTURE,
        ArcContextSignal,
        build_arc_context,
    )
    from backend.core.ouroboros.governance.direction_inferrer import (
        DirectionInferrer,
        arc_context_enabled,
    )
    from backend.core.ouroboros.governance.git_momentum import MomentumSnapshot
    from backend.core.ouroboros.governance.posture import (
        Posture,
        SignalBundle,
        baseline_bundle,
    )

    # --- (1) Master flag default ON
    j.check(
        "1. Master flag defaults True (post-graduation)",
        arc_context_enabled() is True,
        f"got {arc_context_enabled()}",
    )

    # --- (2) Hot-revert path
    os.environ["JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED"] = "false"
    j.check(
        "2. Hot-revert: explicit false disables application",
        arc_context_enabled() is False,
    )
    os.environ.pop("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", None)

    # --- (3) Constant invariant
    j.check(
        "3. MAX_NUDGE_PER_POSTURE constant is 0.10",
        MAX_NUDGE_PER_POSTURE == 0.10,
        f"got {MAX_NUDGE_PER_POSTURE}",
    )

    # --- (4) Frozen dataclass
    sig = ArcContextSignal()
    try:
        sig.lss_verify_ratio = 0.5  # type: ignore[misc]
        j.check("4. ArcContextSignal is frozen (mutation rejected)", False, "mutation succeeded")
    except Exception:
        j.check("4. ArcContextSignal is frozen (mutation rejected)", True)

    # --- (5) Builder happy path with full input
    snap = MomentumSnapshot(
        commit_count=10,
        scope_counts={"governance": 5, "intake": 3, "tests": 2},
        type_counts={"feat": 5, "fix": 3, "docs": 2},
        latest_subjects=("a", "b", "c"),
    )
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=snap,
    ):
        full_ctx = build_arc_context(
            Path("/fake"), lss_one_liner="apply=multi/4 verify=20/20 commit=abc1234567",
        )
    j.check(
        "5. build_arc_context full input → complete signal",
        (
            full_ctx.momentum is snap
            and full_ctx.lss_verify_ratio == 1.0
            and full_ctx.lss_apply_mode == "multi"
            and full_ctx.lss_apply_count == 4
        ),
        f"got verify={full_ctx.lss_verify_ratio} mode={full_ctx.lss_apply_mode}",
    )

    # --- (6) Builder no-git → LSS-only
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=None,
    ):
        lss_only = build_arc_context(
            Path("/fake"), lss_one_liner="apply=single/1 verify=8/10 commit=abc",
        )
    j.check(
        "6. build_arc_context no-git → LSS-only signal",
        lss_only.momentum is None and lss_only.lss_verify_ratio == 0.8,
    )

    # --- (7) Builder no-lss → momentum-only
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=snap,
    ):
        mom_only = build_arc_context(Path("/fake"), lss_one_liner="")
    j.check(
        "7. build_arc_context no-lss → momentum-only signal",
        mom_only.momentum is snap and mom_only.lss_verify_ratio is None,
    )

    # --- (8) is_empty() when neither
    with patch(
        "backend.core.ouroboros.governance.arc_context.compute_recent_momentum",
        return_value=None,
    ):
        empty_ctx = build_arc_context(Path("/fake"), lss_one_liner="")
    j.check(
        "8. is_empty() true when no inputs available",
        empty_ctx.is_empty(),
    )

    # --- (9) Bounded-nudge: feat-dominance → EXPLORE within cap
    feat_ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"feat": 10}),
    )
    nudges = feat_ctx.suggest_nudge()
    j.check(
        "9. Bounded-nudge: feat dominance → EXPLORE at cap",
        (
            nudges[Posture.EXPLORE] == MAX_NUDGE_PER_POSTURE
            and nudges[Posture.HARDEN] == 0.0
            and all(n <= MAX_NUDGE_PER_POSTURE for n in nudges.values())
        ),
        f"got {nudges}",
    )

    # --- (10) Inferrer with flag ON applies nudge
    di = DirectionInferrer()
    bundle = baseline_bundle()
    fix_ctx = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=10, type_counts={"fix": 10}),
    )
    r_on = di.infer(bundle, arc_context=fix_ctx)
    r_baseline = di.infer(bundle)
    on_scores = dict(r_on.all_scores)
    base_scores = dict(r_baseline.all_scores)
    diff = on_scores[Posture.HARDEN] - base_scores[Posture.HARDEN]
    j.check(
        "10. Flag ON applies bounded HARDEN nudge",
        0.0 < diff <= MAX_NUDGE_PER_POSTURE + 1e-9,
        f"diff={diff}",
    )

    # --- (11) Hot-revert: flag OFF does NOT apply nudge
    os.environ["JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED"] = "false"
    r_off = di.infer(bundle, arc_context=fix_ctx)
    off_scores = dict(r_off.all_scores)
    j.check(
        "11. Flag OFF: scores byte-identical to no-arc baseline",
        off_scores == base_scores,
        f"off_scores={off_scores} != base={base_scores}",
    )
    os.environ.pop("JARVIS_DIRECTION_INFERRER_ARC_CONTEXT_ENABLED", None)

    # --- (12) Strong winner stable under max nudge
    strong_explore = SignalBundle(
        feat_ratio=0.95, fix_ratio=0.0, refactor_ratio=0.05,
        test_docs_ratio=0.0, postmortem_failure_rate=0.0,
        iron_gate_reject_rate=0.0, l2_repair_rate=0.0,
        open_ops_normalized=0.0, session_lessons_infra_ratio=0.0,
        time_since_last_graduation_inv=0.5, cost_burn_normalized=0.1,
        worktree_orphan_count=0,
    )
    max_harden = ArcContextSignal(
        momentum=MomentumSnapshot(commit_count=100, type_counts={"fix": 100}),
        lss_verify_ratio=0.0,
    )
    r_strong = di.infer(strong_explore, arc_context=max_harden)
    j.check(
        "12. Strong-EXPLORE stable under max-HARDEN arc nudge",
        r_strong.posture == Posture.EXPLORE,
        f"got {r_strong.posture}",
    )

    # --- (13) Reading carries arc_context through
    j.check(
        "13. PostureReading carries arc_context for observability",
        r_on.arc_context is fix_ctx,
    )

    # --- (14) to_dict emits arc_context when present
    d = r_on.to_dict()
    j.check(
        "14. to_dict() emits arc_context section",
        "arc_context" in d
        and d["arc_context"]["has_momentum"] is True
        and d["arc_context"]["momentum_commits"] == 10,
        f"got {d.get('arc_context')}",
    )

    # --- (15) /posture explain renders Arc Context section
    from backend.core.ouroboros.governance.posture_repl import (
        _render_arc_context_section,
    )
    rendered = _render_arc_context_section(r_on)
    j.check(
        "15. /posture explain renders Arc Context block",
        (
            "Arc Context (P0.5" in rendered
            and "Momentum:" in rendered
            and "Per-posture score nudge" in rendered
        ),
        f"rendered={rendered[:160]!r}",
    )

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
