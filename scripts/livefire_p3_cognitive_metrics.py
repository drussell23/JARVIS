#!/usr/bin/env python3
"""Phase 4 P3 — Cognitive metrics — in-process live-fire smoke.

15 checks. Mirrors P0 / P0.5 / P1 / P1.5 live-fire patterns. Exercises
the full P3 chain (un-stranded RSI modules + wrapper + REPL +
orchestrator helper) WITHOUT requiring an Anthropic API key, a running
battle-test harness, or any network call.

  1.  Master flag default ON (post-graduation)
  2.  Hot-revert: explicit false disables
  3.  Schema version pinned at "cognitive_metrics.1"
  4.  CognitiveMetricRecord frozen
  5.  CognitiveMetricsService constructs against duck-typed oracle
  6.  score_pre_apply returns PreScoreResult with valid gate
  7.  score_pre_apply persists ledger row when flag on
  8.  score_pre_apply does NOT persist when flag off (hot-revert)
  9.  reflect_post_apply returns VindicationResult with valid advisory
  10. reflect_post_apply persists ledger row when flag on
  11. Oracle failure → score_pre_apply returns neutral fallback
  12. Oracle failure → reflect_post_apply returns neutral fallback
  13. Orchestrator helper short-circuits cleanly when singleton missing
  14. Orchestrator helper writes ledger row when wired + flag on
  15. /cognitive REPL stats surfaces aggregates

Exit 0 on PASS; non-zero on FAIL.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class Journal:
    def __init__(self):
        self.passed = []
        self.failed = []

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
        print("All checks passed — P3 Cognitive Metrics live-fire smoke OK.")
        return 0


def _stub_oracle():
    o = MagicMock()
    o.compute_blast_radius.return_value = MagicMock(
        risk_level="LOW", total_affected=5,
    )
    o.get_dependencies.return_value = []
    o.get_dependents.return_value = []
    return o


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("P3 — Cognitive metrics — live-fire smoke (PRD Phase 4)")
    print("=" * 64)

    os.environ.pop("JARVIS_COGNITIVE_METRICS_ENABLED", None)

    from backend.core.ouroboros.governance.cognitive_metrics import (
        COGNITIVE_METRICS_SCHEMA_VERSION,
        CognitiveMetricRecord, CognitiveMetricsService,
        is_enabled, reset_default_service, set_default_service,
    )
    from backend.core.ouroboros.governance.cognitive_metrics_repl import (
        dispatch_cognitive_command as REPL,
    )
    from backend.core.ouroboros.governance.op_context import OperationContext
    from backend.core.ouroboros.governance.orchestrator import (
        _score_cognitive_metrics_pre_apply_impl,
    )
    reset_default_service()

    # --- (1) Master flag default ON
    j.check(
        "1. Master flag defaults True (post-graduation)",
        is_enabled() is True,
        f"got {is_enabled()}",
    )

    # --- (2) Hot-revert
    os.environ["JARVIS_COGNITIVE_METRICS_ENABLED"] = "false"
    j.check(
        "2. Hot-revert: explicit false disables",
        is_enabled() is False,
    )
    os.environ.pop("JARVIS_COGNITIVE_METRICS_ENABLED", None)

    # --- (3) Schema version pinned
    j.check(
        "3. Schema version = cognitive_metrics.1",
        COGNITIVE_METRICS_SCHEMA_VERSION == "cognitive_metrics.1",
    )

    # --- (4) Frozen dataclass
    rec = CognitiveMetricRecord(
        schema_version=COGNITIVE_METRICS_SCHEMA_VERSION,
        op_id="x", kind="pre_score", target_files=("a.py",),
    )
    try:
        rec.kind = "vindication"  # type: ignore[misc]
        j.check("4. CognitiveMetricRecord frozen", False, "mutation succeeded")
    except Exception:
        j.check("4. CognitiveMetricRecord frozen", True)

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)

        # --- (5) Construction
        oracle = _stub_oracle()
        try:
            svc = CognitiveMetricsService(oracle=oracle, project_root=repo)
            j.check(
                "5. CognitiveMetricsService constructs against duck-typed oracle",
                True,
            )
        except Exception as exc:  # noqa: BLE001
            j.check("5. construct", False, repr(exc))
            return j.summary()

        # --- (6) score_pre_apply
        result = svc.score_pre_apply("op-pre", ["a.py", "b.py"], max_complexity=10)
        j.check(
            "6. score_pre_apply returns PreScoreResult with valid gate",
            result.gate in ("FAST_TRACK", "NORMAL", "WARN")
            and 0.0 <= result.pre_score <= 1.0,
            f"got pre_score={result.pre_score} gate={result.gate}",
        )

        # --- (7) Persists when flag on (default true post-graduation)
        j.check(
            "7. score_pre_apply persists ledger row when flag on",
            svc.ledger_path.exists() and svc.ledger_path.stat().st_size > 0,
        )

        # --- (8) Does NOT persist when flag off
        os.environ["JARVIS_COGNITIVE_METRICS_ENABLED"] = "false"
        size_before = svc.ledger_path.stat().st_size
        svc.score_pre_apply("op-pre-off", ["c.py"], max_complexity=5)
        size_after = svc.ledger_path.stat().st_size
        j.check(
            "8. score_pre_apply NO write when flag off (hot-revert)",
            size_before == size_after,
            f"size {size_before} → {size_after}",
        )
        os.environ.pop("JARVIS_COGNITIVE_METRICS_ENABLED", None)

        # --- (9) reflect_post_apply
        v = svc.reflect_post_apply(
            "op-vind", ["a.py"],
            coupling_after=0, blast_radius_after=0,
            complexity_after=10, complexity_before=15,
        )
        j.check(
            "9. reflect_post_apply returns VindicationResult valid advisory",
            v.advisory in ("vindicating", "neutral", "concerning", "warning")
            and -1.0 <= v.vindication_score <= 1.0,
            f"got score={v.vindication_score} advisory={v.advisory}",
        )

        # --- (10) Persists vindication row
        rows = svc.load_records()
        vind_rows = [r for r in rows if r.kind == "vindication"]
        j.check(
            "10. reflect_post_apply persists ledger row when flag on",
            len(vind_rows) >= 1,
            f"got {len(vind_rows)} vindication rows",
        )

        # --- (11/12) Oracle failure → neutral
        boom = MagicMock()
        boom.compute_blast_radius.side_effect = RuntimeError("oracle down")
        boom.get_dependencies.side_effect = RuntimeError("oracle down")
        boom.get_dependents.side_effect = RuntimeError("oracle down")
        bad_svc = CognitiveMetricsService(oracle=boom, project_root=repo)
        bad_pre = bad_svc.score_pre_apply("op-bad", ["x.py"])
        j.check(
            "11. score_pre_apply oracle failure → neutral fallback",
            bad_pre.gate == "NORMAL" and bad_pre.pre_score == 0.5,
            f"got {bad_pre}",
        )
        bad_vind = bad_svc.reflect_post_apply(
            "op-bad", ["x.py"],
            coupling_after=0, blast_radius_after=0,
            complexity_after=0, complexity_before=0,
        )
        j.check(
            "12. reflect_post_apply oracle failure → neutral fallback",
            bad_vind.advisory == "neutral" and bad_vind.vindication_score == 0.0,
            f"got {bad_vind}",
        )

        # --- (13) Helper short-circuits when singleton missing
        reset_default_service()
        ctx = OperationContext.create(
            target_files=("z.py",), description="smoke",
        )
        try:
            _score_cognitive_metrics_pre_apply_impl(ctx)
            j.check(
                "13. Orchestrator helper short-circuits cleanly w/o singleton",
                True,
            )
        except Exception as exc:  # noqa: BLE001
            j.check("13. helper short-circuit", False, repr(exc))

        # --- (14) Helper writes when wired
        set_default_service(svc)
        rows_before = len(svc.load_records())
        _score_cognitive_metrics_pre_apply_impl(ctx)
        rows_after = len(svc.load_records())
        j.check(
            "14. Orchestrator helper writes ledger row when wired + flag on",
            rows_after == rows_before + 1,
            f"rows {rows_before} → {rows_after}",
        )

        # --- (15) REPL stats
        r = REPL("/cognitive stats", service=svc)
        j.check(
            "15. /cognitive REPL stats surfaces aggregates",
            r.ok and "total rows:" in r.text and "pre_score count:" in r.text,
            f"got {r.text[:200] if r.text else 'empty'}",
        )

    reset_default_service()
    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
