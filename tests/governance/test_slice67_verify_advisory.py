"""Slice 67 — swe_bench_pro advisory VERIFY regression gate (companion to S66).

Soak bt-2026-06-02-? : Slice 66 made VALIDATE advisory → the op REACHED APPLY
(outcome='applied'). But the post-APPLY VERIFY regression gate then ran the
scoped tests LOCALLY (pass_rate=0.00, no qutebrowser env) → fired → marked the
op FAILED **and rolled the patch back** (rollback_occurred=True) → eval=unresolved
→ scorer skipped → container engine never reached.

Same root cause as S66, one phase later (VERIFY not VALIDATE). Fix mirrors S66:
for swe_bench_pro ops the local VERIFY regression gate is ADVISORY — clearing
the verify error keeps the patch APPLIED (no rollback, op ends state=applied →
eval=resolved) so the autoscore layer captures it and the ONE-SHOT held-out
container scoring (Slice 65) is the authoritative judge. Non-swe_bench ops keep
the strict regression gate (byte-identical). Syntax/build already gated upstream.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.orchestrator import _swe_bench_verify_advisory


def test_swe_bench_verify_error_cleared():
    assert _swe_bench_verify_advisory("swe_bench_pro", "scoped verify: 21/21 failing", "op1") is None


def test_non_swe_bench_verify_error_kept():
    err = "scoped verify: 3/10 failing"
    assert _swe_bench_verify_advisory("opportunity_miner", err, "op1") == err


def test_empty_source_keeps_error():
    err = "regression"
    assert _swe_bench_verify_advisory("", err, "op1") == err


def test_no_error_passthrough():
    assert _swe_bench_verify_advisory("swe_bench_pro", None, "op1") is None
    assert _swe_bench_verify_advisory("opportunity_miner", None, "op1") is None
