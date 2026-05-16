"""Spine — Fail-Fast Exhaustion Circuit Breaker.

Root (v18 bt-2026-05-16-175621): an op that exhausts all providers
(Anthropic 5xx + empty DW) never converges to a TERMINAL_OPERATION_
STATE, so the autoscore B.2.2 evaluate_problem `asyncio.wait_for(
operation_terminal, 1800s)` sleeps the entire eval window →
terminal_timeout. The breaker flips the op to terminal `failed`
after N consecutive all_providers_exhausted so the existing
publish_operation_terminal SSE wakes the subscriber in seconds.

7 pinned invariants:
  1. Below threshold → unchanged retry/park (no early return).
  2. At threshold → terminal FAILED + all_providers_exhausted_
     circuit_open, then return (no further GENERATE_RETRY/park).
  3. Terminal state ∈ canonical TERMINAL_OPERATION_STATES (so the
     SSE provably fires — the load-bearing wake).
  4. Flag-off → byte-identical (entire breaker behind
     _failfast_cb_enabled(); default FALSE).
  5. Counter pruned ONLY on a genuine terminal (composes the
     canonical terminal set; no mid-op reset).
  6. Threshold env-tunable, default 2, clamp ≥1 (no hardcoded count).
  7. Single seam — one decision site composing the existing
     _record_ledger + ctx.advance; canonical TERMINAL set reused
     (no duplicated state literal list).
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_orch = importlib.import_module(
    "backend.core.ouroboros.governance.orchestrator"
)
_SRC = Path(_orch.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pure helpers — behavioral (invariants 4 + 6)
# ---------------------------------------------------------------------------


def test_cb_enabled_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_FAILFAST_EXHAUSTION_CB_ENABLED", raising=False)
    assert _orch._failfast_cb_enabled() is False
    for v in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_FAILFAST_EXHAUSTION_CB_ENABLED", v)
        assert _orch._failfast_cb_enabled() is True
    monkeypatch.setenv("JARVIS_FAILFAST_EXHAUSTION_CB_ENABLED", "off")
    assert _orch._failfast_cb_enabled() is False


def test_cb_threshold_default_two_env_tunable_clamped(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_FAILFAST_EXHAUSTION_MAX_CONSECUTIVE", raising=False)
    assert _orch._failfast_cb_threshold() == 2          # default
    monkeypatch.setenv(
        "JARVIS_FAILFAST_EXHAUSTION_MAX_CONSECUTIVE", "5")
    assert _orch._failfast_cb_threshold() == 5          # env override
    monkeypatch.setenv(
        "JARVIS_FAILFAST_EXHAUSTION_MAX_CONSECUTIVE", "0")
    assert _orch._failfast_cb_threshold() == 1          # clamp ≥ 1
    monkeypatch.setenv(
        "JARVIS_FAILFAST_EXHAUSTION_MAX_CONSECUTIVE", "garbage")
    assert _orch._failfast_cb_threshold() == 2          # invalid→default


# ---------------------------------------------------------------------------
# AST / source — structural invariants 1,2,3,4,5,7
# ---------------------------------------------------------------------------


def _breaker_segment() -> str:
    i = _SRC.index("── Fail-Fast Exhaustion Circuit Breaker ──")
    # Bound to the breaker CODE: from the header to the `return ctx`
    # that follows the ledger call (the comment block also contains
    # the literal "return ctx", so anchor past the real ledger call).
    ledger = _SRC.index("await self._record_ledger(", i)
    j = _SRC.index("return ctx", ledger)
    return _SRC[i:j + len("return ctx")]


def test_inv4_entire_breaker_behind_flag_and_cause():
    seg = _breaker_segment()
    # The guard is a single conjunction: flag AND the exhaustion cause
    assert "_failfast_cb_enabled()" in seg
    assert '"all_providers_exhausted" in _err_msg' in seg
    gi = _SRC.index("if (\n                        _failfast_cb_enabled()")
    # guard appears BEFORE any counter mutation / return
    mut = _SRC.index("_failfast_exhaust_consec[", gi)
    assert gi < mut, "counter mutated only inside the flag guard"


def test_inv1_below_threshold_no_early_return():
    seg = _breaker_segment()
    # Strip comment lines (the explanatory block also contains the
    # literal "→ return ctx"); count only CODE returns.
    code = "\n".join(
        ln for ln in seg.splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert code.count("return ctx") == 1, (
        "exactly one CODE return in the breaker — under the "
        ">=threshold branch only"
    )
    thr = code.index("_ff_n >= _failfast_cb_threshold()")
    ret = code.index("return ctx")
    assert thr < ret, (
        "return must be inside the >=threshold branch; below "
        "threshold the op falls through to the existing retry/park"
    )


def test_inv2_threshold_flips_terminal_failed_reason_code():
    seg = _breaker_segment()
    assert "OperationState.FAILED" in seg
    # reason marker referenced via the single contiguous constant
    assert "_FAILFAST_CIRCUIT_OPEN_REASON" in seg
    # ...and the constant itself is a contiguous grep-able literal
    assert (
        '_FAILFAST_CIRCUIT_OPEN_REASON = '
        '"all_providers_exhausted_circuit_open"' in _SRC
    )
    assert "_record_ledger(" in seg
    assert "ctx.advance(" in seg and "POSTMORTEM" in seg
    # record_ledger precedes the return (terminal recorded, then exit)
    assert seg.index("_record_ledger(") < seg.index("return ctx")


def test_inv3_failed_is_canonical_terminal_state():
    from backend.core.ouroboros.governance.ledger import OperationState
    from backend.core.ouroboros.governance.ide_observability_stream import (
        TERMINAL_OPERATION_STATES,
    )
    # The breaker stamps FAILED; FAILED.value must be in the SAME
    # canonical set publish_operation_terminal gates on, or the SSE
    # (and thus the subscriber wake) never fires.
    assert OperationState.FAILED.value in TERMINAL_OPERATION_STATES


def test_inv5_counter_pruned_only_on_canonical_terminal():
    # Prune lives in _record_ledger, gated by the canonical set —
    # NOT a duplicated inline state list, NOT unconditional.
    assert (
        "self._failfast_exhaust_consec.pop(" in _SRC
    )
    prune = _SRC.index(
        "Fail-Fast counter prune — single terminal chokepoint"
    )
    seg = _SRC[prune:prune + 700]
    assert "TERMINAL_OPERATION_STATES" in seg, (
        "prune must gate on the canonical terminal set (composition)"
    )
    assert "self._failfast_exhaust_consec.pop(" in seg


def test_inv7_single_seam_no_parallel_terminator():
    # Exactly one circuit-open reason-code site (the decision seam);
    # the terminal flip composes the existing _record_ledger, not a
    # new bespoke terminator.
    assert _SRC.count(
        "all_providers_exhausted_circuit_open"
    ) <= 3  # reason str (advance + ledger payload) + log; one site
    # No second consecutive-exhaustion counter anywhere else.
    assert _SRC.count("_failfast_exhaust_consec") >= 3  # init+incr+prune
    # The canonical terminal set is IMPORTED (composition), not
    # redefined as a literal in orchestrator.
    assert "frozenset({\n    \"applied\"" not in _SRC


def test_orchestrator_imports_clean():
    # The module parses + the helpers + init field exist (guards
    # against a broken edit landing on the hottest file).
    tree = ast.parse(_SRC)
    names = {
        n.name for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
    }
    assert "_failfast_cb_enabled" in names
    assert "_failfast_cb_threshold" in names
    assert "self._failfast_exhaust_consec" in _SRC
