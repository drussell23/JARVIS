"""Predictive Provider Resilience — Slice 1 (TTFT Forecaster, SHADOW).

Mathematical-correctness spine. These tests PROVE the EMA-weighted
streaming-moment regression actually tracks a known law (the
synthetic analogue of "the forecaster's EMA curve tracks reality").
The MAE-against-real-traffic proof comes from the harvest soak; this
spine proves the math is sound *before* trusting it on real data —
the same epistemic ordering as Slice 0.

Pins:
  * cold model refuses to extrapolate (predict → None) below the
    non-degenerate-variance floor;
  * convergence: on y = a + b·x + noise the recovered slope/intercept
    and the prequential MAE collapse toward the noise floor;
  * prequential honesty: the model NEVER sees a sample before it has
    predicted it (predict-then-update) — proven structurally + behaviourally;
  * adaptivity: a regime shift (slope change) is tracked, not memorised;
  * degenerate rows (timeout ttft=-1 / zero-token / non-success) are
    refused entry to the regression so they cannot poison the slope;
  * STRICT SHADOW: the forecaster + its callsite enforce nothing
    (no timeout return, no client mutation, no shedding) — AST-pinned.
"""
from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.dw_ttft_observer import (
    ForecastResult,
    ProviderLatencySample,
    TtftForecaster,
    _MIN_N_FOR_NONDEGENERATE_VARIANCE,
    _forecast_alpha,
    provider_latency_forecast_enabled,
)

_REPO = Path(__file__).resolve().parents[2]
_OBS_SRC = _REPO / "backend/core/ouroboros/governance/dw_ttft_observer.py"
_PROVIDERS_SRC = _REPO / "backend/core/ouroboros/governance/providers.py"


def _s(x, y, *, provider="claude-api", route="complex", outcome="success"):
    return ProviderLatencySample(
        provider=provider, route=route, op_id="o",
        input_tokens=int(x), ttft_ms=int(y), total_ms=int(y) + 10,
        outcome=outcome, sample_unix=1.0,
    )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (
        "JARVIS_PROVIDER_LATENCY_FORECAST_ENABLED",
        "JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


# --------------------------------------------------------------------------
# Cold model + flag default
# --------------------------------------------------------------------------

def test_master_flag_defaults_false():
    assert provider_latency_forecast_enabled() is False


def test_cold_model_refuses_to_extrapolate():
    fc = TtftForecaster()
    assert fc.forecast("claude-api", "complex", 5000) is None
    # Below the variance floor it must still refuse.
    for i in range(_MIN_N_FOR_NONDEGENERATE_VARIANCE - 1):
        fc.observe(_s(1000 + i, 500 + i))
    assert fc.forecast("claude-api", "complex", 5000) is None


# --------------------------------------------------------------------------
# THE PROOF — convergence to a known linear law (prequential)
# --------------------------------------------------------------------------

def test_ema_ols_recovers_known_law_and_mae_collapses(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "0.05")
    rng = random.Random(20260519)
    A, B, SIGMA = 220.0, 0.045, 8.0  # true: ttft = 220 + 0.045*tok + N(0,8)
    fc = TtftForecaster()

    errs = []
    for _ in range(600):
        x = rng.uniform(400, 60000)
        y = A + B * x + rng.gauss(0.0, SIGMA)
        r = fc.observe(_s(x, y))
        if r.abs_err_ms is not None:
            errs.append(r.abs_err_ms)

    # 1. Recovered line matches the generative law. Predict at two
    #    points → infer slope/intercept from the model itself.
    p_lo = fc.forecast("claude-api", "complex", 1000)
    p_hi = fc.forecast("claude-api", "complex", 50000)
    assert p_lo is not None and p_hi is not None
    slope = (p_hi - p_lo) / (50000 - 1000)
    assert abs(slope - B) < 0.2 * B, (
        f"recovered slope {slope:.5f} must track true {B} (±20%)"
    )

    # 2. Prequential MAE over the last 200 predictions collapses
    #    toward the irreducible noise floor (~0.8·σ for |N(0,σ)|).
    tail = errs[-200:]
    tail_mae = sum(tail) / len(tail)
    assert tail_mae < 4.0 * SIGMA, (
        f"tail MAE {tail_mae:.1f} must approach the noise floor "
        f"(<4σ={4*SIGMA}) — the curve tracks reality"
    )
    # 3. The model demonstrably LEARNED: early error ≫ late error.
    early = sum(errs[:50]) / 50
    assert tail_mae < early, "MAE must shrink as the model learns"


def test_prequential_predict_before_update_behaviourally():
    """The model must score a sample with the PRE-update line — it
    never peeks at the point it is being tested on."""
    fc = TtftForecaster()
    for x in (1000, 2000, 3000, 4000, 5000):
        fc.observe(_s(x, 100 + 0.01 * x))
    pred_before = fc.forecast("claude-api", "complex", 9_000_000)
    # A massive outlier: if observe() folded BEFORE predicting, the
    # returned prediction would already be dragged toward it.
    r = fc.observe(_s(9_000_000, 999_999))
    assert r.predicted_ms is not None
    assert abs(r.predicted_ms - pred_before) < 1e-6, (
        "observe() must predict with the standing model, then update"
    )


# --------------------------------------------------------------------------
# Adaptivity — tracks a regime shift, doesn't memorise
# --------------------------------------------------------------------------

def test_tracks_regime_shift(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "0.3")
    rng = random.Random(7)
    fc = TtftForecaster()
    # Regime A: slope 0.02
    for _ in range(200):
        x = rng.uniform(500, 40000)
        fc.observe(_s(x, 150 + 0.02 * x + rng.gauss(0, 5)))
    # Regime B: slope triples (provider degraded)
    post = []
    for _ in range(200):
        x = rng.uniform(500, 40000)
        r = fc.observe(_s(x, 150 + 0.06 * x + rng.gauss(0, 5)))
        if r.abs_err_ms is not None:
            post.append(r.abs_err_ms)
    # After adapting, the late-window error is far below the
    # immediate post-shift shock → it followed reality, not memory.
    shock = sum(post[:25]) / 25
    settled = sum(post[-50:]) / 50
    assert settled < shock, "EMA must re-track the new regime"


# --------------------------------------------------------------------------
# Degenerate-row rejection (dataset-integrity guarantee)
# --------------------------------------------------------------------------

def test_degenerate_rows_never_poison_the_slope():
    fc = TtftForecaster()
    for x in range(1000, 9000, 500):
        fc.observe(_s(x, 100 + 0.01 * x))
    good = fc.forecast("claude-api", "complex", 30000)
    # Flood with timeout/zero-token/non-success garbage.
    for _ in range(500):
        fc.observe(_s(50000, -1, outcome="timeout"))
        fc.observe(_s(0, 99999, outcome="success"))
        fc.observe(_s(50000, 88888, outcome="cancelled"))
    after = fc.forecast("claude-api", "complex", 30000)
    assert good is not None and after is not None
    assert abs(after - good) < 1e-6, (
        "timeout/zero-token/non-success rows must be refused entry "
        "to the regression — they cannot move the slope"
    )


def test_observe_returns_skip_row_for_degenerate():
    fc = TtftForecaster()
    r = fc.observe(_s(50000, -1, outcome="timeout"))
    assert isinstance(r, ForecastResult)
    assert r.predicted_ms is None  # not scored, not fitted


def test_never_raises_on_garbage():
    fc = TtftForecaster()
    assert isinstance(fc.observe("nope"), ForecastResult)  # type: ignore[arg-type]
    assert fc.forecast("", "", 0) is None
    assert fc.mae("x", "y") is None
    assert fc.sample_n("x", "y") == 0


def test_warm_start_from_jsonl(tmp_path):
    p = tmp_path / "pl.jsonl"
    rows = [
        _s(1000 + 500 * i, 200 + 0.03 * (1000 + 500 * i)).to_jsonl_obj()
        for i in range(20)
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows))
    fc = TtftForecaster()
    n = fc.warm_start_from_jsonl(p)
    assert n == 20
    assert fc.forecast("claude-api", "complex", 25000) is not None
    assert fc.warm_start_from_jsonl(tmp_path / "absent.jsonl") == 0


def test_alpha_env_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "nan?")
    assert _forecast_alpha() == 0.2
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "0")
    assert _forecast_alpha() == 0.2
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "1.5")
    assert _forecast_alpha() == 0.2
    monkeypatch.setenv("JARVIS_PROVIDER_LATENCY_FORECAST_ALPHA", "0.4")
    assert _forecast_alpha() == 0.4


# --------------------------------------------------------------------------
# AST pins — STRICT SHADOW + prequential ordering structural
# --------------------------------------------------------------------------

def _cls_method(src: Path, cls: str, meth: str) -> ast.FunctionDef:
    tree = ast.parse(src.read_text())
    for c in ast.walk(tree):
        if isinstance(c, ast.ClassDef) and c.name == cls:
            for n in c.body:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == meth:
                    return n  # type: ignore[return-value]
    raise AssertionError(f"{cls}.{meth} not found")


def test_ast_pin_observe_predicts_before_updates():
    body = ast.unparse(_cls_method(_OBS_SRC, "TtftForecaster", "observe"))
    i_pred = body.find("st.predict(")
    i_upd = body.find("st.update(")
    assert 0 <= i_pred < i_upd, (
        "prequential contract: st.predict MUST appear before "
        "st.update in observe() (predict-then-update)"
    )


def _strip_docstrings(node: ast.AST) -> None:
    """Remove docstring Expr nodes in-place (class + every nested
    function) so prose explaining the contract can't trip a
    code-token pin — only executable code is scanned."""
    for n in ast.walk(node):
        body = getattr(n, "body", None)
        if (
            isinstance(body, list) and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body.pop(0)


def test_ast_pin_forecaster_enforces_nothing():
    """STRICT SHADOW — the forecaster class must not mutate clients,
    return timeouts, or trigger shedding in EXECUTABLE code."""
    tree = ast.parse(_OBS_SRC.read_text())
    cls = next(
        c for c in ast.walk(tree)
        if isinstance(c, ast.ClassDef) and c.name == "TtftForecaster"
    )
    _strip_docstrings(cls)
    src = ast.unparse(cls).lower()
    # Code-shaped enforcement patterns (not bare prose words).
    for bad in ("read_timeout", "http_timeout", "shed(", "shed =",
                "knapsack", "_client.", "client.create",
                "set_timeout", ".timeout ="):
        assert bad not in src, (
            f"Slice 1 is SHADOW — forbidden enforcement token {bad!r}"
        )


def test_ast_pin_predict_guarded_by_variance_floor():
    body = ast.unparse(_cls_method(_OBS_SRC, "_RegState", "predict"))
    assert "_MIN_N_FOR_NONDEGENERATE_VARIANCE" in body, (
        "predict() must refuse below the non-degenerate-variance "
        "floor (regression validity, not a hardcoded gate)"
    )


def test_ast_pin_forecast_result_is_frozen():
    tree = ast.parse(_OBS_SRC.read_text())
    cd = next(
        c for c in ast.walk(tree)
        if isinstance(c, ast.ClassDef) and c.name == "ForecastResult"
    )
    deco = "".join(ast.unparse(d) for d in cd.decorator_list)
    assert "frozen=True" in deco


def test_ast_pin_shadow_callsite_logs_not_enforces():
    src = _PROVIDERS_SRC.read_text()
    i = src.index("# Sink 3 — Slice 1 SHADOW forecast")
    j = src.index("# noqa: BLE001 — shadow never perturbs", i)
    region = src[i:j]
    assert "get_ttft_forecaster" in region and "logger.info" in region
    assert ".observe(sample)" in region
    # Scan executable lines only — strip comment-only lines so the
    # block's own contract prose ("triggers NO shedding") can't trip.
    code = "\n".join(
        ln for ln in region.splitlines()
        if not ln.lstrip().startswith("#")
    )
    for bad in ("read_timeout =", "timeout =", "shed(", "shed =",
                "= _r.predicted", "raise "):
        assert bad not in code, (
            f"shadow callsite must only log — found enforcement {bad!r}"
        )
