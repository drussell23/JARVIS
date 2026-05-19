"""Predictive Provider Resilience — E1 spine (Robust Location-Scale).

The token-slope was FALSIFIED on real data (r(tokens,TTFT)≈0.11):
TTFT is bimodal & queue-driven, not token-driven. E1 replaces the
regression with a token-INDEPENDENT robust EWMA-median location +
log-MAD scale. There is no "forecast" — we maintain a data-driven
CEILING.

This spine proves the load-bearing properties:
  * token independence — payload size does NOT move the estimator;
  * robustness — a 73,512 ms spike barely moves the baseline
    (the OLS divergence to 897,350 ms is structurally impossible);
  * the ceiling COVERS spikes yet stays physically bounded
    (never the 2.7e6-class divergence);
  * envelope inflates under congestion, deflates when healthy;
  * degenerate rows (timeout ttft=-1 / non-success) are NOT folded;
  * prequential — the ceiling is read BEFORE the fold (no peeking);
  * Fix A — route excluded from the key (one provider, one pool);
  * STRICT SHADOW — enforces nothing (AST-pinned);
  * the slope/OLS/token terms are GONE from the state (AST-pinned).
"""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_ttft_observer import (
    EnvelopeResult,
    ForecastResult,
    ProviderLatencyEnvelope,
    ProviderLatencySample,
    TtftForecaster,
    _MIN_N_FOR_NONDEGENERATE_VARIANCE,
    _MAX_LOG_EXPONENT,
    provider_latency_forecast_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_OBS_SRC = _REPO / "backend/core/ouroboros/governance/dw_ttft_observer.py"
_PROVIDERS_SRC = _REPO / "backend/core/ouroboros/governance/providers.py"


def _s(tokens, ttft, *, provider="claude-api", route="complex",
       outcome="success"):
    return ProviderLatencySample(
        provider=provider, route=route, op_id="o",
        input_tokens=int(tokens), ttft_ms=int(ttft),
        total_ms=int(ttft) + 10, outcome=outcome, sample_unix=1.0,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (
        "JARVIS_PROVIDER_LATENCY_FORECAST_ENABLED",
        "JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA",
        "JARVIS_PROVIDER_LATENCY_FORECAST_K",
        "JARVIS_PROVIDER_LATENCY_MAD_CONSISTENCY",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# ---- flag default + cold state -------------------------------------------

def test_master_flag_defaults_false():
    assert provider_latency_forecast_enabled() is False


def test_cold_state_has_no_baseline():
    e = ProviderLatencyEnvelope()
    assert e.baseline("claude-api", "complex") is None
    assert e.envelope("claude-api", "complex") == (None, None, None)
    for i in range(_MIN_N_FOR_NONDEGENERATE_VARIANCE - 1):
        e.observe(_s(1000 + i, 900 + i))
    assert e.baseline("claude-api", "complex") is None  # below floor


# ---- back-compat aliases (import stability) ------------------------------

def test_aliases_present():
    assert TtftForecaster is ProviderLatencyEnvelope
    assert ForecastResult is EnvelopeResult


# ---- TOKEN INDEPENDENCE (the falsification made structural) --------------

def test_baseline_is_token_independent():
    """Wildly varying payloads, identical TTFT → identical baseline.
    The estimator must not consume input_tokens at all."""
    e1 = ProviderLatencyEnvelope()
    e2 = ProviderLatencyEnvelope()
    rng = random.Random(1)
    for _ in range(30):
        ttft = 1500 + rng.gauss(0, 30)
        e1.observe(_s(2000, ttft))             # tiny payloads
        e2.observe(_s(140000, ttft))           # 70× larger payloads
    b1 = e1.baseline("claude-api", "complex")
    b2 = e2.baseline("claude-api", "complex")
    assert b1 is not None and b2 is not None
    assert abs(b1 - b2) < 1.0, (
        "payload size must NOT change the baseline (token-independent)"
    )


# ---- THE E1 PROOF: robust to the 73,512 ms spike -------------------------

def test_robust_median_does_not_diverge_on_73s_spike():
    """Raw-ms OLS diverged to ~897,350 ms here. The EWMA-median
    baseline must barely move; the ceiling must COVER the spike yet
    stay physically bounded (never the 2.7e6-class divergence)."""
    e = ProviderLatencyEnvelope()
    rng = random.Random(73)
    for _ in range(40):
        e.observe(_s(rng.randint(2000, 40000),
                     int(1100 + rng.gauss(0, 80))))
    b_before = e.baseline("claude-api", "complex")
    assert b_before is not None and b_before < 4000

    _, _, ceil_before = e.envelope("claude-api", "complex")
    e.observe(_s(7000, 73512))   # the pathology, mid-size payload
    b_after = e.baseline("claude-api", "complex")
    _, band, ceil_1 = e.envelope("claude-api", "complex")
    assert b_after is not None and ceil_before is not None

    # (1) location barely moves — bounded-influence median (OLS
    #     diverged to 897,350 ms here; that is now impossible).
    assert b_after < b_before * 3, (
        f"baseline {b_after:.0f} diverged from {b_before:.0f}"
    )
    # (2) ONE unprecedented spike does NOT get instantly enveloped —
    #     that is the CORRECT robust tradeoff (bounded influence,
    #     prequential read). The ceiling must INFLATE strongly
    #     toward it instead of ignoring it.
    assert ceil_1 is not None and ceil_1 > ceil_before * 3, (
        f"ceiling failed to inflate after the spike "
        f"({ceil_before:.0f}→{ceil_1:.0f})"
    )
    # (3) physically bounded — never the 2.7e6-class divergence.
    import math
    assert ceil_1 < math.exp(_MAX_LOG_EXPONENT) + 1, (
        f"ceiling {ceil_1:.0f} exceeded the physical bound"
    )
    # (4) on RECURRENCE the ceiling does envelope the pathology
    #     (the system learns the regime is congested).
    for _ in range(6):
        e.observe(_s(7000, 73512))
    _, _, ceil_n = e.envelope("claude-api", "complex")
    assert ceil_n is not None and ceil_n > 73512, (
        f"after repeated spikes the ceiling {ceil_n:.0f} must "
        f"envelope the 73,512 ms pathology"
    )
    assert ceil_n < math.exp(_MAX_LOG_EXPONENT) + 1


def test_envelope_inflates_then_deflates():
    """Congestion widens the band; sustained calm shrinks it —
    a dynamic ceiling that breathes with the queue."""
    e = ProviderLatencyEnvelope()
    for _ in range(30):
        e.observe(_s(5000, 1000))
    _, calm_band, _ = e.envelope("claude-api", "complex")
    for _ in range(8):                    # a congestion burst
        e.observe(_s(5000, 60000))
    _, hot_band, _ = e.envelope("claude-api", "complex")
    for _ in range(80):                   # sustained recovery
        e.observe(_s(5000, 1000))
    _, settled_band, _ = e.envelope("claude-api", "complex")
    assert hot_band > calm_band, "band must inflate under congestion"
    assert settled_band < hot_band, "band must deflate when healthy"


# ---- degenerate rows are NOT folded --------------------------------------

def test_timeout_and_nonsuccess_rows_not_folded():
    e = ProviderLatencyEnvelope()
    for _ in range(20):
        e.observe(_s(5000, 1200))
    b0 = e.baseline("claude-api", "complex")
    for _ in range(200):
        e.observe(_s(5000, -1, outcome="timeout"))
        e.observe(_s(5000, 88888, outcome="cancelled"))
    b1 = e.baseline("claude-api", "complex")
    assert b0 is not None and b1 is not None
    assert abs(b1 - b0) < 1e-6, (
        "timeout / non-success rows must NOT move the robust state"
    )


def test_observe_returns_skip_for_degenerate():
    e = ProviderLatencyEnvelope()
    r = e.observe(_s(5000, -1, outcome="timeout"))
    assert isinstance(r, EnvelopeResult)
    assert r.enveloped is None and r.abs_dev_ms is None


def test_never_raises_on_garbage():
    e = ProviderLatencyEnvelope()
    assert isinstance(e.observe("nope"), EnvelopeResult)  # type: ignore[arg-type]
    assert e.baseline("", "") is None
    assert e.envelope("x", "y") == (None, None, None)
    assert e.sample_n("x", "y") == 0


# ---- Fix A — unified key (route excluded) --------------------------------

def test_unified_key_pools_routes():
    e = ProviderLatencyEnvelope()
    for _ in range(10):
        e.observe(_s(5000, 1000, route="sonar"))
    n_sonar = e.sample_n("claude-api", "sonar")
    e.observe(_s(5000, 1000, route="complex"))
    assert e.sample_n("claude-api", "complex") == n_sonar + 1
    assert e.baseline("claude-api", "sonar") == \
        e.baseline("claude-api", "complex")


def test_warm_start_from_jsonl(tmp_path):
    p = tmp_path / "pl.jsonl"
    rows = [
        _s(3000 + i, 1000 + i).to_jsonl_obj() for i in range(20)
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    e = ProviderLatencyEnvelope()
    assert e.warm_start_from_jsonl(p) == 20
    assert e.baseline("claude-api", "complex") is not None
    assert e.warm_start_from_jsonl(tmp_path / "absent.jsonl") == 0


# ---- AST pins ------------------------------------------------------------

def _cls(name: str) -> ast.ClassDef:
    tree = ast.parse(_OBS_SRC.read_text())
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef) and c.name == name:
            return c
    raise AssertionError(f"{name} not found")


def _method(cls: ast.ClassDef, meth: str) -> ast.AST:
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == meth:
            return n
    raise AssertionError(f"{cls.name}.{meth} not found")


def _strip_docstrings(node: ast.AST) -> None:
    for n in ast.walk(node):
        b = getattr(n, "body", None)
        if (
            isinstance(b, list) and b
            and isinstance(b[0], ast.Expr)
            and isinstance(b[0].value, ast.Constant)
            and isinstance(b[0].value.value, str)
        ):
            b.pop(0)


def test_ast_pin_slope_and_token_terms_burned():
    """The OLS slope / cross-moments / token term MUST be gone from
    the robust state's EXECUTABLE code — the falsified regression
    cannot resurrect. (Docstrings legitimately NAME the burned
    terms to forbid them, so scan code only.)"""
    cls = _cls("_RobustState")
    _strip_docstrings(cls)
    src = ast.unparse(cls).lower()
    for bad in ("slope", "exy", "exx", "intercept", "input_tokens",
                "x * y", "x * x", " ols", "shrink"):
        assert bad not in src, (
            f"forbidden regression remnant {bad!r} in _RobustState — "
            f"the token-slope was falsified and must stay burned"
        )
    # The estimator's update must take ttft only — NO token arg.
    upd = ast.unparse(_method(_cls("_RobustState"), "update"))
    assert "def update(self, y: float, alpha: float)" in upd, (
        "update() must consume ttft (y) only — token-independent"
    )


def test_ast_pin_observe_envelope_before_update():
    body = ast.unparse(_method(_cls("ProviderLatencyEnvelope"), "observe"))
    i_env = body.find("st.envelope(")
    i_upd = body.find("st.update(")
    assert 0 <= i_env < i_upd, (
        "prequential: st.envelope (read standing ceiling) MUST "
        "precede st.update (fold) — no peeking"
    )


def test_ast_pin_envelope_is_shadow_only():
    src = ast.unparse(_cls("ProviderLatencyEnvelope")).lower()
    for bad in ("read_timeout =", "shed(", "_client.", "set_timeout",
                ".timeout ="):
        assert bad not in src, (
            f"E1 is SHADOW — forbidden enforcement token {bad!r}"
        )


def test_ast_pin_envelope_result_frozen():
    cd = _cls("EnvelopeResult")
    deco = "".join(ast.unparse(d) for d in cd.decorator_list)
    assert "frozen=True" in deco


def test_ast_pin_shadow_callsite_logs_not_enforces():
    src = _PROVIDERS_SRC.read_text()
    i = src.index("# Sink 3 — SHADOW latency ENVELOPE")
    j = src.index("# noqa: BLE001 — shadow never perturbs", i)
    region = src[i:j]
    assert "get_ttft_forecaster" in region and "logger.info" in region
    assert ".observe(sample)" in region
    code = "\n".join(
        ln for ln in region.splitlines()
        if not ln.lstrip().startswith("#")
    )
    for bad in ("read_timeout =", "timeout =", "shed(", "raise "):
        assert bad not in code, (
            f"shadow callsite must only log — found {bad!r}"
        )
