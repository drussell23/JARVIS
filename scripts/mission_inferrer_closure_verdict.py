#!/usr/bin/env python3
"""Empirical-closure verdict for the MissionInferrer arc.

In-process probe (no soak required) over the four contracts the
arc was supposed to deliver:

  Contract 1 -- Master flag flipped default-true post-graduation
                (operators get the substrate without explicit opt-in).
  Contract 2 -- GoalInferenceEngine.build() PRODUCES a real result
                against the live repo (corpus_n>0 or hypotheses>0
                depending on signal availability).
  Contract 3 -- priority_boost_for_signal CONSUMED by
                _compute_priority -- proven via a synthetic envelope
                whose theme matches a forced inferred goal; matched
                priority MUST be strictly lower (better) than the
                same-shape envelope without theme overlap.
  Contract 4 -- AST regression pins HOLD against current source
                (substrate + cross-file intake consumer pin).

Optional Contract 5 -- SSE event ``goal_inference_built`` fires on
cache miss. Treated as advisory because it depends on stream broker
construction in this process.

Exit codes:
    0 = all four primary contracts PASSED
    1 = at least one primary contract FAILED

Usage:
    python3 scripts/mission_inferrer_closure_verdict.py
"""
from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _eval_master_default() -> ContractVerdict:
    os.environ.pop("JARVIS_GOAL_INFERENCE_ENABLED", None)
    from backend.core.ouroboros.governance import goal_inference as gi
    enabled = gi.inference_enabled()
    recorded: Dict[str, object] = {}

    class _R:
        def register(self, spec):
            recorded[spec.name] = spec.default

    gi.register_flags(_R())
    spec_default = recorded.get("JARVIS_GOAL_INFERENCE_ENABLED")
    passed = enabled and spec_default is True
    return ContractVerdict(
        name="C1 Master flag default-true post-graduation",
        passed=bool(passed),
        evidence=(
            f"inference_enabled()={enabled} "
            f"register_flags_default={spec_default}"
        ),
    )


def _eval_engine_build() -> ContractVerdict:
    os.environ["JARVIS_GOAL_INFERENCE_ENABLED"] = "true"
    from backend.core.ouroboros.governance import goal_inference as gi
    gi.reset_default_engine()
    engine = gi.GoalInferenceEngine(repo_root=REPO_ROOT)
    result = engine.build(force=True)
    passed = (
        result is not None
        and result.build_reason in ("first_build", "refresh_elapsed")
        and result.total_samples > 0
    )
    top_theme = result.inferred[0].theme if result.inferred else "(none)"
    top_conf = (
        result.inferred[0].confidence if result.inferred else 0.0
    )
    return ContractVerdict(
        name="C2 Engine build produces real result against live repo",
        passed=bool(passed),
        evidence=(
            f"build_reason={result.build_reason} "
            f"total_samples={result.total_samples} "
            f"hypotheses={len(result.inferred)} "
            f"top_theme={top_theme!r} "
            f"top_confidence={top_conf:.3f} "
            f"sources_contributing={dict(result.sources_contributing)}"
        ),
    )


def _eval_intake_consumption() -> ContractVerdict:
    """Constructs two near-identical envelopes; only the first matches a
    forced inferred goal's theme. Matched envelope MUST yield strictly
    lower (better) priority than unmatched."""
    os.environ["JARVIS_GOAL_INFERENCE_ENABLED"] = "true"
    os.environ["JARVIS_GOAL_INFERENCE_PRIORITY_BOOST_MAX"] = "1.0"
    from backend.core.ouroboros.governance import goal_inference as gi
    from backend.core.ouroboros.governance.goal_inference import (
        InferenceResult, InferredGoal, SignalSample,
    )
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        make_envelope,
    )
    from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
        _compute_priority,
    )

    gi.reset_default_engine()
    engine = gi.GoalInferenceEngine(repo_root=REPO_ROOT)
    forced = InferenceResult(
        inferred=(InferredGoal(
            theme="authentication",
            tokens=("authentication",),
            confidence=1.0,
            supporting_sources=("commits",),
            evidence=(SignalSample(
                source="commits", token="authentication",
                weight=1.0, citation="forced",
            ),),
        ),),
        built_at=1.0, build_ms=1, total_samples=1,
        sources_contributing={"commits": 1},
        build_reason="first_build",
    )
    engine._cached = forced
    engine._last_build_mono = 1e9
    gi.register_default_engine(engine)

    def _env(desc):
        return make_envelope(
            source="backlog", description=desc,
            target_files=("a.py",), repo="jarvis", urgency="normal",
            confidence=0.5,
            evidence={"signature": f"verdict-{desc[:10]}"},
            requires_human_ack=False,
        )

    p_matched, _ = _compute_priority(_env("authentication rewrite"))
    p_unmatched, _ = _compute_priority(_env("unrelated logging change"))
    passed = p_matched < p_unmatched
    return ContractVerdict(
        name="C3 priority_boost_for_signal consumed by intake",
        passed=passed,
        evidence=(
            f"matched_priority={p_matched} "
            f"unmatched_priority={p_unmatched} "
            f"diff={p_unmatched - p_matched}"
        ),
    )


def _eval_ast_invariants() -> ContractVerdict:
    from backend.core.ouroboros.governance import goal_inference as gi
    invariants = gi.register_shipped_invariants()
    failures: List[str] = []
    inv_status: List[str] = []
    for inv in invariants:
        target_path = REPO_ROOT / inv.target_file
        if not target_path.is_file():
            failures.append(f"{inv.invariant_name}: target missing")
            continue
        source = target_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        violations = inv.validate(tree, source)
        if violations:
            failures.append(
                f"{inv.invariant_name}: {violations}"
            )
        else:
            inv_status.append(f"{inv.invariant_name}=PASS")
    return ContractVerdict(
        name="C4 AST regression pins hold against current source",
        passed=not failures,
        evidence=(
            f"invariants={len(invariants)} "
            f"results=[{', '.join(inv_status)}] "
            + (f"failures={failures}" if failures else "")
        ),
    )


def _eval_sse_publish_advisory() -> ContractVerdict:
    """Advisory: SSE publisher fires on cache miss. Wrapped in mock
    so we don't need a live broker; verifies the wire-up only."""
    os.environ["JARVIS_GOAL_INFERENCE_ENABLED"] = "true"
    from backend.core.ouroboros.governance import goal_inference as gi
    gi.reset_default_engine()
    engine = gi.GoalInferenceEngine(repo_root=REPO_ROOT)
    with mock.patch(
        "backend.core.ouroboros.governance.ide_observability_stream."
        "publish_goal_inference_built",
    ) as pub:
        engine.build(force=True)
        called = pub.called
        kwargs = pub.call_args.kwargs if pub.called else {}
    return ContractVerdict(
        name="C5 SSE event fires on cache miss (advisory)",
        passed=called,
        evidence=(
            f"publisher_called={called} "
            f"top_theme={kwargs.get('top_theme', '?')!r} "
            f"hypotheses_count={kwargs.get('hypotheses_count', '?')}"
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for MissionInferrer arc")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_master_default(),
        _eval_engine_build(),
        _eval_intake_consumption(),
        _eval_ast_invariants(),
    ]
    advisory = _eval_sse_publish_advisory()
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    mark = "PASS" if advisory.passed else "INFO"
    print(f"  [{mark}] {advisory.name}")
    print(f"         {advisory.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: MissionInferrer arc EMPIRICALLY CLOSED -- "
              "all four primary contracts PASSED.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "MissionInferrer arc not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
