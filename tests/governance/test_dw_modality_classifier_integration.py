"""Phase 12 Slice G — classifier × modality_ledger integration.

Pins:
  §1 modality_ledger=None → behaves exactly as legacy (no filtering)
  §2 NON_CHAT models excluded from EVERY generative route (incl spec)
  §3 UNKNOWN models routed to SPECULATIVE only (Zero-Trust pattern)
  §4 CHAT_CAPABLE models follow normal classifier rules
  §5 NON_CHAT verdict overrides chat-capable metadata (server > catalog)
  §6 Exception in modality_ledger.is_non_chat doesn't block classifier
  §7 Source-level pin — _rank_for_route checks ledger BEFORE quarantine
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Optional  # noqa: F401

import pytest

from backend.core.ouroboros.governance.dw_catalog_classifier import (
    DwCatalogClassifier,
)
from backend.core.ouroboros.governance.dw_catalog_client import (
    CatalogSnapshot, ModelCard,
)
from backend.core.ouroboros.governance.dw_modality_ledger import (
    ModalityLedger,
)
from backend.core.ouroboros.governance.dw_promotion_ledger import (
    PromotionLedger,
)


@pytest.fixture
def isolated_promotion_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> PromotionLedger:
    monkeypatch.setenv(
        "JARVIS_DW_PROMOTION_LEDGER_PATH", str(tmp_path / "promo.json"),
    )
    return PromotionLedger()


@pytest.fixture
def isolated_modality_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> ModalityLedger:
    monkeypatch.setenv(
        "JARVIS_DW_MODALITY_LEDGER_PATH", str(tmp_path / "mod.json"),
    )
    return ModalityLedger()


def _card(model_id: str, *, params_b: float = 7.0,
          out_price: float = 0.30) -> ModelCard:
    return ModelCard(
        model_id=model_id,
        family=model_id.split("/")[0] if "/" in model_id else "unknown",
        parameter_count_b=params_b,
        context_window=None,
        pricing_in_per_m_usd=None,
        pricing_out_per_m_usd=out_price,
        supports_streaming=True,
        raw_metadata_json="{}",
    )


def _snapshot(*models: ModelCard) -> CatalogSnapshot:
    return CatalogSnapshot(fetched_at_unix=1.0, models=tuple(models))


# ---------------------------------------------------------------------------
# §1 — Legacy behavior preserved (modality_ledger=None)
# ---------------------------------------------------------------------------


def test_modality_ledger_none_is_legacy(
    isolated_promotion_ledger: PromotionLedger,
) -> None:
    """Without modality_ledger, classifier runs exactly as before
    Slice G — backward compat verified."""
    snap = _snapshot(
        _card("v/big-50B", params_b=50.0, out_price=1.0),
        _card("v/m-7B", params_b=7.0, out_price=0.30),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(snap, isolated_promotion_ledger)
    # Both eligible per their respective gates
    assert "v/big-50B" in outcome.for_route("complex")
    assert "v/m-7B" in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §2 — NON_CHAT models excluded from every route
# ---------------------------------------------------------------------------


def test_non_chat_excluded_from_all_routes(
    isolated_promotion_ledger: PromotionLedger,
    isolated_modality_ledger: ModalityLedger,
) -> None:
    """A model tagged NON_CHAT in the modality ledger MUST NOT appear
    in COMPLEX, STANDARD, BACKGROUND, or even SPECULATIVE.

    Server has ground-truth evidence (4xx + modality marker, OR
    explicit metadata flag) — sending real ops would be wasted work."""
    isolated_modality_ledger.record_metadata_verdict(
        "v/embed-8B", is_chat_capable=False,
    )
    snap = _snapshot(
        _card("v/embed-8B", params_b=8.0, out_price=0.05),
        _card("v/chat-7B", params_b=7.0, out_price=0.30),
    )
    classifier = DwCatalogClassifier()
    outcome = classifier.classify(
        snap, isolated_promotion_ledger,
        modality_ledger=isolated_modality_ledger,
    )
    for route in ("complex", "standard", "background", "speculative"):
        assert "v/embed-8B" not in outcome.for_route(route), (
            f"NON_CHAT model leaked into {route}"
        )
    # The chat-capable model is still in BG (its eligibility gate)
    assert "v/chat-7B" in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §3 — UNKNOWN models route to SPECULATIVE only
# ---------------------------------------------------------------------------


def test_unknown_models_route_to_speculative_only(
    isolated_promotion_ledger: PromotionLedger,
    isolated_modality_ledger: ModalityLedger,
) -> None:
    """UNKNOWN verdict (probe pending or never run) → same treatment
    as Zero-Trust ambiguous-metadata quarantine: SPECULATIVE-only."""
    isolated_modality_ledger.register_unknown("v/unknown-7B")
    snap = _snapshot(
        _card("v/unknown-7B", params_b=7.0, out_price=0.30),
    )
    outcome = DwCatalogClassifier().classify(
        snap, isolated_promotion_ledger,
        modality_ledger=isolated_modality_ledger,
    )
    assert "v/unknown-7B" not in outcome.for_route("complex")
    assert "v/unknown-7B" not in outcome.for_route("standard")
    assert "v/unknown-7B" not in outcome.for_route("background")
    assert "v/unknown-7B" in outcome.for_route("speculative")


# ---------------------------------------------------------------------------
# §4 — CHAT_CAPABLE follows normal rules
# ---------------------------------------------------------------------------


def test_chat_capable_unrestricted(
    isolated_promotion_ledger: PromotionLedger,
    isolated_modality_ledger: ModalityLedger,
) -> None:
    """CHAT_CAPABLE verdict imposes NO additional restriction —
    classifier's existing eligibility gates are the only filter."""
    isolated_modality_ledger.record_metadata_verdict(
        "v/big-50B", is_chat_capable=True,
    )
    snap = _snapshot(
        _card("v/big-50B", params_b=50.0, out_price=1.0),
    )
    outcome = DwCatalogClassifier().classify(
        snap, isolated_promotion_ledger,
        modality_ledger=isolated_modality_ledger,
    )
    assert "v/big-50B" in outcome.for_route("complex")
    assert "v/big-50B" in outcome.for_route("standard")


# ---------------------------------------------------------------------------
# §5 — NON_CHAT verdict overrides everything (server > catalog)
# ---------------------------------------------------------------------------


def test_non_chat_overrides_promotion_ledger(
    isolated_promotion_ledger: PromotionLedger,
    isolated_modality_ledger: ModalityLedger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that's PROMOTED in the promotion ledger but NON_CHAT
    in the modality ledger → modality wins. Promotion only matters
    for chat-capable models."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    isolated_promotion_ledger.register_quarantine("v/proven-7B")
    isolated_promotion_ledger.record_success("v/proven-7B", 100)
    isolated_promotion_ledger.promote("v/proven-7B")
    isolated_modality_ledger.record_metadata_verdict(
        "v/proven-7B", is_chat_capable=False,
    )
    snap = _snapshot(
        _card("v/proven-7B", params_b=7.0, out_price=0.30),
    )
    outcome = DwCatalogClassifier().classify(
        snap, isolated_promotion_ledger,
        modality_ledger=isolated_modality_ledger,
    )
    # NON_CHAT wins — promotion doesn't help
    assert "v/proven-7B" not in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §6 — Defensive: ledger exception doesn't block classifier
# ---------------------------------------------------------------------------


def test_classifier_tolerates_modality_ledger_exception(
    isolated_promotion_ledger: PromotionLedger,
) -> None:
    """If is_non_chat raises (corrupt ledger, disk error, anything),
    the classifier must NOT crash. Falls through as if model were
    CHAT_CAPABLE / UNKNOWN."""
    class _BrokenLedger:
        def is_non_chat(self, mid: str) -> bool:
            raise RuntimeError("ledger blew up")

        def is_unknown(self, mid: str) -> bool:
            raise RuntimeError("ledger blew up")
    snap = _snapshot(
        _card("v/m-7B", params_b=7.0, out_price=0.30),
    )
    outcome = DwCatalogClassifier().classify(
        snap, isolated_promotion_ledger,
        modality_ledger=_BrokenLedger(),
    )
    # Classifier did not crash; model still ranks per legacy rules
    assert "v/m-7B" in outcome.for_route("background")


# ---------------------------------------------------------------------------
# §7 — Source-level pin: modality gate fires BEFORE quarantine logic
# ---------------------------------------------------------------------------


def test_source_modality_gate_fires_before_quarantine() -> None:
    """The modality hard gate must run BEFORE the promotion-ledger
    quarantine logic — otherwise a model that's NON_CHAT could still
    be SPECULATIVE-quarantined and waste a probe slot."""
    src = inspect.getsource(DwCatalogClassifier._rank_for_route)
    modality_idx = src.index("modality_ledger.is_non_chat")
    quarantine_idx = src.index("ledger.is_quarantined")
    assert modality_idx < quarantine_idx, (
        "modality gate must fire before quarantine check so NON_CHAT "
        "models are excluded entirely (not just SPECULATIVE-quarantined)"
    )