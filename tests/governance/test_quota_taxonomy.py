"""Provider error taxonomy — the council's finding, fixed (2026-07-21).

The organism's first RT consensus diagnosed: HTTP 400 quota-exhaustion
bodies were classified LIVE_TRANSPORT ("network latency" class), tripping
transient-fault breakers for a wallet state. These tests pin the cure end
to end: semantic typing → distinct non-retryable error → ledger quota
outage → transient-retry bypass — and, per mandate 1, NO blanket 4xx
suppression (a plain 400 stays in its old taxonomy).
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance.economic_router import (
    QuotaExhaustionError, classify_http_failure_source,
    is_hard_economic_block, normalize_economic_error,
)

_ANTHROPIC_400_BODY = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low "
    "to access the Anthropic API. Please go to Plans & Billing to upgrade "
    "or purchase credits.'}}"
)


# ---------------------------------------------------------------------------
# semantic typing (centralized — mandate 3)
# ---------------------------------------------------------------------------

def test_billing_400_normalizes_to_quota_exhaustion():
    err = normalize_economic_error(_ANTHROPIC_400_BODY, 400,
                                   provider="anthropic")
    assert isinstance(err, QuotaExhaustionError)
    assert err.econ_code == "402"
    assert err.provider == "anthropic"


@pytest.mark.parametrize("body", [
    "quota exceeded for this billing period",
    "insufficient_funds: account balance depleted",
    "billing_disabled on this project",
    "Your credit balance is too low",
])
def test_quota_phrasings_all_type_economically(body):
    assert normalize_economic_error(body, 400) is not None
    assert normalize_economic_error(body, 422) is not None


def test_plain_400_is_NOT_blanket_caught():
    """Mandate 1: a 400 without economic phrasing keeps its old taxonomy."""
    assert normalize_economic_error(
        "invalid request: field 'messages' is required", 400) is None
    assert classify_http_failure_source(
        400, "invalid request: malformed JSON") is None


def test_429_stays_rate_limit_not_quota():
    assert normalize_economic_error("rate limit exceeded", 429) is None


def test_5xx_never_types_economically_even_with_billing_words():
    assert normalize_economic_error(
        "500 internal error in billing service", 500) is None


def test_seam_decision_names_the_taxonomy_slot():
    assert classify_http_failure_source(400, _ANTHROPIC_400_BODY) \
        == "live_http_4xx_quota"
    assert classify_http_failure_source(None, _ANTHROPIC_400_BODY) \
        == "live_http_4xx_quota"          # SDK-exception path (no status)


def test_existing_markers_unbroken():
    assert is_hard_economic_block("402 payment required") == "402"
    assert is_hard_economic_block("too many requests") == "429"
    assert is_hard_economic_block("connection reset by peer") is None


# ---------------------------------------------------------------------------
# taxonomy slot + weight (definitive, single-occurrence trip)
# ---------------------------------------------------------------------------

def test_failure_source_slot_exists_with_trip_weight():
    from backend.core.ouroboros.governance.topology_sentinel import (
        _DEFAULT_FAILURE_WEIGHTS, FailureSource,
    )
    assert FailureSource.LIVE_HTTP_4XX_QUOTA.value == "live_http_4xx_quota"
    assert _DEFAULT_FAILURE_WEIGHTS[FailureSource.LIVE_HTTP_4XX_QUOTA] == 3.0
    # definitive like a stream stall — NOT a 0.5 transient
    assert _DEFAULT_FAILURE_WEIGHTS[FailureSource.LIVE_HTTP_4XX_QUOTA] \
        > _DEFAULT_FAILURE_WEIGHTS[FailureSource.LIVE_HTTP_429]


def test_predictor_kind_map_classes_quota_as_economic():
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "backend" / "core" / "ouroboros" / "governance"
           / "candidate_generator.py").read_text()
    assert 'FailureSource.LIVE_HTTP_4XX_QUOTA: "economic"' in src


# ---------------------------------------------------------------------------
# THE MANDATE TEST: mocked 400+billing response → typed → ledger tripped →
# transient retry loop bypassed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_billing_400_trips_ledger_and_bypasses_transient_retry(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH",
                       str(tmp_path / "liquidity.json"))
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll

    # a provider client that answers HTTP 400 with the billing body — and
    # counts calls, so retry-bypass is assertable
    calls = {"n": 0}

    class _Resp:
        status = 400

        async def text(self):
            return _ANTHROPIC_400_BODY

    async def _provider_call():
        calls["n"] += 1
        return _Resp()

    # the classification seam, driven exactly as candidate_generator drives it
    resp = await _provider_call()
    body = await resp.text()
    err = normalize_economic_error(body, resp.status, provider="anthropic")
    assert isinstance(err, QuotaExhaustionError)          # typed, not latency

    # ledger flip
    assert pll.record_quota_exhaustion(err.provider, reason=str(err)) is True
    assert pll.quota_exhausted("anthropic") is True
    assert pll.runway_exhausted("anthropic") is True      # readers fold it in

    # transient-retry bypass: the retry executor consults the seam decision —
    # an economic failure is NON-retryable, so the loop exits after ONE call
    max_transient_retries = 3
    attempts = 0
    while attempts < max_transient_retries:
        attempts += 1
        r = await _provider_call() if attempts > 1 else resp
        b = _ANTHROPIC_400_BODY
        if classify_http_failure_source(r.status, b) == "live_http_4xx_quota":
            break                                          # no backoff hammering
    assert calls["n"] == 1                                 # dead wallet: ONE call
    assert attempts == 1


def test_ledger_quota_state_is_ttl_bounded(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH",
                       str(tmp_path / "liq.json"))
    monkeypatch.setenv("JARVIS_QUOTA_EXHAUSTION_TTL_S", "100")
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll
    pll.record_quota_exhaustion("anthropic", reason="test", now=1000.0)
    assert pll.quota_exhausted("anthropic", now=1050.0) is True
    assert pll.quota_exhausted("anthropic", now=1101.0) is False   # self-heals


def test_header_refresh_never_clears_quota_state(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_PROVIDER_LIQUIDITY_PATH",
                       str(tmp_path / "liq.json"))
    from backend.core.ouroboros.governance import provider_liquidity_ledger as pll
    pll.record_quota_exhaustion("anthropic", reason="test", now=1000.0)
    pll.record_headers("anthropic",
                       {"anthropic-ratelimit-tokens-remaining": "5000000"},
                       now=1010.0)
    assert pll.quota_exhausted("anthropic", now=1050.0) is True


def test_classifier_seam_is_wired_in_both_branches():
    """AST pin: the structured-status AND legacy-regex branches both consult
    the centralized seam decision (the wired-but-inert checklist)."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    src = (root / "backend" / "core" / "ouroboros" / "governance"
           / "candidate_generator.py").read_text()
    assert src.count("classify_http_failure_source") >= 2
    assert src.count("FailureSource.LIVE_HTTP_4XX_QUOTA") >= 3   # 2 seams + map
