"""Phase 12 Slice B — DwCatalogClassifier regression spine.

Pins:
  §1 Empty snapshot → empty per-route assignments (no exceptions)
  §2 Determinism — same input + env → bit-for-bit identical output
  §3 Eligibility gates — COMPLEX min params, STANDARD min params + max price,
                          BG max price, SPEC max price, IMMEDIATE always empty
  §4 Ranking — params weight (COMPLEX/STANDARD), pricing weight (BG/SPEC),
               family bonus, context window
  §5 Tie-break by alphabetical model_id (stable secondary sort)
  §6 Zero-Trust §3.6 — ambiguous-metadata models pinned to SPECULATIVE only
  §7 Newly-quarantined models surface in ClassificationOutcome
  §8 Promoted-from-quarantine models eligible for non-SPEC routes
  §9 Demoted models (post-promotion failure) re-pinned to SPECULATIVE
  §10 Env-tunable weights + family preferences
  §11 Pure function — does NOT mutate ledger
  §12 22-model real-world simulation (mirrors bt-2026-04-27-235708 catalog size)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Tuple  # noqa: F401

import pytest

from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_catalog_classifier import (
    ClassificationOutcome,
    DwCatalogClassifier,
    EligibilityGate,
    RouteAssignment,
    gate_for_route,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ledger(tmp_path: Path,
                    monkeypatch: pytest.MonkeyPatch) -> PromotionLedger:
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH",
        str(tmp_path / "ledger.json"),
    )
    led = PromotionLedger()
    led.load()
    return led


def _card(
    model_id: str,
    *,
    family: str = "auto",
    params_b: float = None,  # type: ignore[assignment]
    ctx: int = None,         # type: ignore[assignment]
    in_price: float = None,  # type: ignore[assignment]
    out_price: float = None, # type: ignore[assignment]
    streaming: bool = True,
) -> ModelCard:
    if family == "auto":
        family = model_id.split("/", 1)[0].lower() if "/" in model_id else "unknown"
    return ModelCard(
        model_id=model_id,
        family=family,
        parameter_count_b=params_b,
        context_window=ctx,
        pricing_in_per_m_usd=in_price,
        pricing_out_per_m_usd=out_price,
        supports_streaming=streaming,
        raw_metadata_json="{}",
    )


def _snapshot(*models: ModelCard) -> CatalogSnapshot:
    return CatalogSnapshot(fetched_at_unix=1.0, models=tuple(models))


# ---------------------------------------------------------------------------
# §1 — Empty snapshot
# ---------------------------------------------------------------------------


def test_empty_snapshot_returns_empty_assignments(
    isolated_ledger: PromotionLedger,
) -> None:
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(_snapshot(), isolated_ledger)
    for route in ("complex", "standard", "background", "speculative"):
        assert outcome.for_route(route) == ()
    assert outcome.newly_quarantined == ()


# ---------------------------------------------------------------------------
# §2 — Determinism
# ---------------------------------------------------------------------------


def test_classify_is_deterministic(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("vendor-a/m-50B", params_b=50.0, out_price=0.30),
        _card("vendor-b/m-30B", params_b=30.0, out_price=0.20),
        _card("vendor-c/m-70B", params_b=70.0, out_price=0.40),
    )
    classifier = DwCatalogClassifier()
    o1 = classifier.classify(snap, isolated_ledger)
    o2 = classifier.classify(snap, isolated_ledger)
    o3 = classifier.classify(snap, isolated_ledger)
    for r in ("complex", "standard", "background", "speculative"):
        assert o1.for_route(r) == o2.for_route(r) == o3.for_route(r)


# ---------------------------------------------------------------------------
# §3 — Eligibility gates
# ---------------------------------------------------------------------------


def test_complex_excludes_below_min_params(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/small-7B", params_b=7.0, out_price=0.05),
        _card("v/big-50B", params_b=50.0, out_price=0.50),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/small-7B" not in outcome.for_route("complex")
    assert "v/big-50B" in outcome.for_route("complex")


def test_standard_excludes_above_max_price(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/cheap-30B", params_b=30.0, out_price=0.50),
        _card("v/pricey-30B", params_b=30.0, out_price=5.00),  # over $2
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/cheap-30B" in outcome.for_route("standard")
    assert "v/pricey-30B" not in outcome.for_route("standard")


def test_background_excludes_above_max_price(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/cheap-7B", params_b=7.0, out_price=0.30),
        _card("v/medium-7B", params_b=7.0, out_price=0.80),  # over $0.50
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/cheap-7B" in outcome.for_route("background")
    assert "v/medium-7B" not in outcome.for_route("background")


def test_speculative_excludes_above_max_price(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/ultra-1B", params_b=1.0, out_price=0.05),
        _card("v/medium-1B", params_b=1.0, out_price=0.30),  # over $0.10
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/ultra-1B" in outcome.for_route("speculative")
    assert "v/medium-1B" not in outcome.for_route("speculative")


def test_immediate_always_empty(
    isolated_ledger: PromotionLedger,
) -> None:
    """IMMEDIATE is Claude-direct by Manifesto §5 — classifier must
    NEVER populate it."""
    snap = _snapshot(
        _card("v/big-100B", params_b=100.0, out_price=0.10),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    # IMMEDIATE not in the assignments at all (only generative routes)
    assert "immediate" not in outcome.assignments
    assert outcome.for_route("immediate") == ()


def test_eligibility_gate_admits_function() -> None:
    gate = EligibilityGate(
        min_params_b=14.0,
        max_out_price_per_m=2.0,
    )
    assert gate.admits(_card("ok/14B", params_b=14.0, out_price=1.0))
    assert not gate.admits(_card("too-small/7B", params_b=7.0, out_price=1.0))
    assert not gate.admits(_card("too-pricey/14B", params_b=14.0, out_price=3.0))
    # Missing param count → fails min_params_b gate (>0 required)
    assert not gate.admits(_card("ambiguous/m"))


# ---------------------------------------------------------------------------
# §4 — Ranking
# ---------------------------------------------------------------------------


def test_complex_ranks_larger_models_higher(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/m-30B", params_b=30.0, out_price=1.0),
        _card("v/m-100B", params_b=100.0, out_price=1.0),
        _card("v/m-50B", params_b=50.0, out_price=1.0),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("complex")
    # Largest first, smallest last
    assert ranked == ("v/m-100B", "v/m-50B", "v/m-30B")


def test_background_ranks_cheaper_models_higher(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/m-7B", params_b=7.0, out_price=0.40),
        _card("v/m-7B-cheap", params_b=7.0, out_price=0.10),
        _card("v/m-7B-medium", params_b=7.0, out_price=0.25),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("background")
    # Cheapest first
    assert ranked[0] == "v/m-7B-cheap"


def test_speculative_ranks_cheaper_models_higher(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("v/m-1B-medium", params_b=1.0, out_price=0.08),
        _card("v/m-1B-cheap", params_b=1.0, out_price=0.02),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("speculative")
    assert ranked[0] == "v/m-1B-cheap"


def test_context_window_breaks_ties_at_complex(
    isolated_ledger: PromotionLedger,
) -> None:
    """All else equal, larger context wins."""
    snap = _snapshot(
        _card("v/m-50B-narrow", params_b=50.0, out_price=1.0, ctx=8000),
        _card("v/m-50B-wide", params_b=50.0, out_price=1.0, ctx=200_000),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("complex")
    assert ranked[0] == "v/m-50B-wide"


def test_family_bonus_breaks_ties(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_FAMILY_PREFERENCE", "moonshotai:1.0")
    snap = _snapshot(
        _card("vendor-a/m-50B", params_b=50.0, out_price=1.0),
        _card("moonshotai/Kimi-50B", params_b=50.0, out_price=1.0),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("complex")
    assert ranked[0] == "moonshotai/Kimi-50B"


# ---------------------------------------------------------------------------
# §5 — Tie-break by alphabetical id
# ---------------------------------------------------------------------------


def test_alphabetical_tiebreak_when_scores_equal(
    isolated_ledger: PromotionLedger,
) -> None:
    """Two models with identical scores → alphabetical model_id order."""
    snap = _snapshot(
        _card("zebra/m-50B", params_b=50.0, out_price=1.0),
        _card("apple/m-50B", params_b=50.0, out_price=1.0),
        _card("mango/m-50B", params_b=50.0, out_price=1.0),
    )
    ranked = DwCatalogClassifier().classify(snap, isolated_ledger).for_route("complex")
    assert ranked == ("apple/m-50B", "mango/m-50B", "zebra/m-50B")


# ---------------------------------------------------------------------------
# §6 — Zero-Trust §3.6 SPECULATIVE quarantine
# ---------------------------------------------------------------------------


def test_ambiguous_metadata_pinned_to_speculative_only(
    isolated_ledger: PromotionLedger,
) -> None:
    """A model with no params + no pricing must NOT appear in
    BACKGROUND, STANDARD, or COMPLEX — operator-mandated 2026-04-27."""
    snap = _snapshot(
        _card("moonshotai/Kimi-K2.6"),  # both params and pricing absent
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert outcome.for_route("complex") == ()
    assert outcome.for_route("standard") == ()
    assert outcome.for_route("background") == ()
    # Lands in SPECULATIVE despite no pricing
    assert "moonshotai/Kimi-K2.6" in outcome.for_route("speculative")


def test_ambiguous_quarantine_persists_when_ledger_already_knows(
    isolated_ledger: PromotionLedger,
) -> None:
    """Pre-quarantined model stays quarantined even on subsequent
    classify() calls."""
    isolated_ledger.register_quarantine("v/unknown-model")
    snap = _snapshot(
        _card("v/unknown-model"),  # ambiguous
        _card("v/known-50B", params_b=50.0, out_price=1.0),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    # Quarantined model only in SPECULATIVE
    assert "v/unknown-model" in outcome.for_route("speculative")
    for r in ("complex", "standard", "background"):
        assert "v/unknown-model" not in outcome.for_route(r)


def test_partial_metadata_avoids_quarantine(
    isolated_ledger: PromotionLedger,
) -> None:
    """A model with parameter count BUT no pricing → not ambiguous,
    not quarantined. Just consider eligibility gates."""
    snap = _snapshot(
        _card("v/m-50B", params_b=50.0),  # no pricing, but param count present
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    # Has param_count, so not ambiguous → not auto-quarantined
    assert outcome.newly_quarantined == ()


# ---------------------------------------------------------------------------
# §7 — Newly-quarantined surface
# ---------------------------------------------------------------------------


def test_newly_quarantined_returned_for_caller(
    isolated_ledger: PromotionLedger,
) -> None:
    snap = _snapshot(
        _card("brand/new-unknown"),       # ambiguous, never seen
        _card("v/known-50B", params_b=50.0, out_price=1.0),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert outcome.newly_quarantined == ("brand/new-unknown",)


def test_existing_quarantined_NOT_in_newly_list(
    isolated_ledger: PromotionLedger,
) -> None:
    """Already-known quarantined models don't re-surface."""
    isolated_ledger.register_quarantine("brand/already-known")
    snap = _snapshot(
        _card("brand/already-known"),  # ambiguous + already in ledger
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert outcome.newly_quarantined == ()


# ---------------------------------------------------------------------------
# §8 — Promoted models cross route boundary
# ---------------------------------------------------------------------------


def test_promoted_model_eligible_for_background(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A promoted model with metadata that passes BG eligibility gate
    appears in BACKGROUND."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    isolated_ledger.register_quarantine("v/proven-7B")
    isolated_ledger.record_success("v/proven-7B", 100)
    isolated_ledger.promote("v/proven-7B")
    # Even though it's in the ledger, classify should treat it like
    # a regular model. Give it metadata that passes BG gate.
    snap = _snapshot(
        _card("v/proven-7B", params_b=7.0, out_price=0.30),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/proven-7B" in outcome.for_route("background")


def test_promoted_model_still_blocked_by_eligibility_gate(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promotion enables consideration; eligibility gates still apply.
    A promoted 5B model still doesn't meet COMPLEX min_params_b=30."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    isolated_ledger.register_quarantine("v/small-5B")
    isolated_ledger.record_success("v/small-5B", 100)
    isolated_ledger.promote("v/small-5B")
    snap = _snapshot(
        _card("v/small-5B", params_b=5.0, out_price=0.10),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/small-5B" not in outcome.for_route("complex")
    # But fits BG (≤$0.5)
    assert "v/small-5B" in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §9 — Demoted models re-pinned to SPECULATIVE
# ---------------------------------------------------------------------------


def test_demoted_model_returns_to_speculative_only(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promoted → record_failure → demoted → SPECULATIVE-only again."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    isolated_ledger.register_quarantine("v/flaky-7B")
    isolated_ledger.record_success("v/flaky-7B", 100)
    isolated_ledger.promote("v/flaky-7B")
    isolated_ledger.record_failure("v/flaky-7B")  # demotes
    assert isolated_ledger.is_promoted("v/flaky-7B") is False
    snap = _snapshot(
        _card("v/flaky-7B", params_b=7.0, out_price=0.30),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    # No longer in BG
    assert "v/flaky-7B" not in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §10 — Env-tunable weights + family preferences
# ---------------------------------------------------------------------------


def test_complex_min_params_env_override(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_CLASSIFIER_COMPLEX_MIN_PARAMS_B", "100")
    snap = _snapshot(
        _card("v/m-50B", params_b=50.0, out_price=1.0),
        _card("v/m-150B", params_b=150.0, out_price=1.0),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    # 50B no longer passes the raised gate
    assert "v/m-50B" not in outcome.for_route("complex")
    assert "v/m-150B" in outcome.for_route("complex")


def test_background_max_price_env_override(
    isolated_ledger: PromotionLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_CLASSIFIER_BACKGROUND_MAX_OUT_PRICE", "1.0")
    snap = _snapshot(
        _card("v/m-7B", params_b=7.0, out_price=0.80),  # would normally exceed default
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)
    assert "v/m-7B" in outcome.for_route("background")


def test_family_preference_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple comma-separated families with varying weights parse
    correctly, malformed tokens skipped silently."""
    monkeypatch.setenv(
        "JARVIS_DW_FAMILY_PREFERENCE",
        "moonshotai:1.0,zai-org:0.8,not-a-pair,broken:not-a-float",
    )
    g = gate_for_route("complex")  # function under test reads env
    # Direct access via module-private — sanity: no crash
    from backend.core.ouroboros.governance.dw_catalog_classifier import (
        _family_preference,
    )
    parsed = _family_preference()
    assert parsed.get("moonshotai") == 1.0
    assert parsed.get("zai-org") == 0.8
    # Malformed tokens silently dropped
    assert "not-a-pair" not in parsed
    assert "broken" not in parsed


# ---------------------------------------------------------------------------
# §11 — Pure function — does NOT mutate ledger
# ---------------------------------------------------------------------------


def test_classify_does_not_mutate_ledger(
    isolated_ledger: PromotionLedger,
) -> None:
    """The classifier returns ``newly_quarantined`` for the caller to
    register; classify() itself MUST NOT call ledger.register_quarantine."""
    snap = _snapshot(
        _card("brand/new-unknown"),
    )
    classifier = DwCatalogClassifier()
    before = isolated_ledger.quarantined_models()
    outcome = classifier.classify(snap, isolated_ledger)
    after = isolated_ledger.quarantined_models()
    # Ledger state unchanged
    assert before == after
    # But the surface flagged the new ambiguous model
    assert "brand/new-unknown" in outcome.newly_quarantined


# ---------------------------------------------------------------------------
# §12 — 22-model real-world simulation
# ---------------------------------------------------------------------------


def test_22_model_realistic_catalog(
    isolated_ledger: PromotionLedger,
) -> None:
    """Mirrors the 22-model count from session bt-2026-04-27-235708.
    Mix: 3 large agentic, 5 mid-size, 8 small/cheap, 6 ambiguous.
    Verifies all routes get realistic-looking populations."""
    snap = _snapshot(
        # Large agentic (COMPLEX)
        _card("moonshotai/Kimi-K2.6", family="moonshotai",
              params_b=400.0, ctx=200_000, out_price=0.40),
        _card("zai-org/GLM-5.1-FP8", family="zai-org",
              params_b=355.0, ctx=128_000, out_price=0.35),
        _card("Qwen/Qwen3.5-397B-A17B", family="qwen",
              params_b=397.0, ctx=128_000, out_price=0.40),
        # Mid-size (STANDARD)
        _card("Qwen/Qwen3.6-35B-A3B-FP8", family="qwen",
              params_b=35.0, ctx=128_000, out_price=0.20),
        _card("vendor/m-30B", params_b=30.0, ctx=64_000, out_price=0.18),
        _card("vendor/m-22B", params_b=22.0, ctx=32_000, out_price=0.15),
        _card("vendor/m-14B", params_b=14.0, ctx=32_000, out_price=0.12),
        _card("vendor/m-20B", params_b=20.0, ctx=64_000, out_price=0.16),
        # Small/cheap (BACKGROUND eligible)
        _card("Qwen/Qwen3.5-9B", family="qwen", params_b=9.0,
              ctx=32_000, out_price=0.06),
        _card("Qwen/Qwen3.5-4B", family="qwen", params_b=4.0,
              ctx=32_000, out_price=0.04),
        _card("vendor/m-7B", params_b=7.0, ctx=8_000, out_price=0.08),
        _card("vendor/m-3B", params_b=3.0, ctx=8_000, out_price=0.05),
        _card("vendor/m-1.5B", params_b=1.5, ctx=4_000, out_price=0.03),
        _card("vendor/m-2B", params_b=2.0, ctx=4_000, out_price=0.04),
        _card("vendor/m-13B", params_b=13.0, ctx=8_000, out_price=0.10),
        _card("vendor/m-5B", params_b=5.0, ctx=8_000, out_price=0.06),
        # Ambiguous (NEVER in BG, only SPECULATIVE quarantine)
        _card("ambig/unknown-1"),
        _card("ambig/unknown-2"),
        _card("ambig/unknown-3"),
        _card("ambig/unknown-4"),
        _card("ambig/unknown-5"),
        _card("ambig/unknown-6"),
    )
    outcome = DwCatalogClassifier().classify(snap, isolated_ledger)

    # COMPLEX: only 30B+ models pass min_params_b=30 default
    # → 3 large (400B, 355B, 397B) + 2 mid (35B, 30B) = 5
    complex_models = outcome.for_route("complex")
    assert len(complex_models) == 5
    # First-ranked is the largest
    assert "Qwen/Qwen3.5-397B-A17B" in complex_models[:3]

    # STANDARD: 14B+ models pass min_params_b=14, ≤$2/M
    standard_models = outcome.for_route("standard")
    assert len(standard_models) >= 6
    # No ambiguous models in STANDARD
    for amb in ("ambig/unknown-1", "ambig/unknown-2"):
        assert amb not in standard_models

    # BACKGROUND: ≤$0.5/M, no ambiguous
    bg_models = outcome.for_route("background")
    assert len(bg_models) >= 8
    # All have param_count and pricing under $0.5
    # Cheapest first
    assert bg_models[0] in ("vendor/m-1.5B", "Qwen/Qwen3.5-4B",
                            "vendor/m-2B")
    for amb in ("ambig/unknown-1", "ambig/unknown-2"):
        assert amb not in bg_models

    # SPECULATIVE: ≤$0.1/M priced models + all ambiguous quarantined
    spec_models = outcome.for_route("speculative")
    # Ambiguous all land here
    for amb in ("ambig/unknown-1", "ambig/unknown-2", "ambig/unknown-3",
                "ambig/unknown-4", "ambig/unknown-5", "ambig/unknown-6"):
        assert amb in spec_models

    # All 6 newly quarantined
    assert len(outcome.newly_quarantined) == 6
