"""dw_catalog_client → pricing_oracle integration — regression spine.

Pins the Option α hook that closes the Static Pricing Blindspot
diagnosed in soak #6. The contract:

  * When DW's ``/models`` response includes pricing → use API pricing
    (Oracle never consulted)
  * When pricing is partially missing → fill ONLY the missing side
  * When pricing is fully missing AND a seed pattern matches → Oracle
    fills both sides
  * When pricing is fully missing AND NO pattern matches → both stay
    None (legacy behavior preserved → has_ambiguous_metadata() True)
  * Master-off → Oracle returns None for everything → legacy behavior
  * Oracle exceptions are swallowed defensively → never break catalog
    parse

§1   API pricing wins — Oracle not consulted
§2   Oracle fills both when API pricing absent
§3   Oracle fills only the missing side
§4   Oracle miss → both stay None → has_ambiguous_metadata() True
§5   Oracle hit → has_ambiguous_metadata() False (the bug fix)
§6   Master-off → Oracle short-circuits, legacy behavior restored
§7   Oracle exception → catalog parse continues, model still built
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import pricing_oracle
from backend.core.ouroboros.governance.dw_catalog_client import ModelCard
from backend.core.ouroboros.governance.pricing_oracle import (
    PricingPattern,
    register_pricing_pattern,
    reset_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_oracle():
    reset_for_tests()
    yield
    reset_for_tests()


# ===========================================================================
# §1 — API pricing wins
# ===========================================================================


def test_api_pricing_wins_over_oracle() -> None:
    """When API has pricing, Oracle is irrelevant."""
    raw = {
        "id": "Qwen-3.5-397B-A17B",
        "pricing": {"input": 5.0, "output": 10.0},  # explicit API price
    }
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # Oracle would say (0.10, 0.40) — API wins
    assert card.pricing_in_per_m_usd == 5.0
    assert card.pricing_out_per_m_usd == 10.0


def test_top_level_api_pricing_also_wins() -> None:
    raw = {
        "id": "Qwen-3.5-397B-A17B",
        "pricing_in_per_m_usd": 7.0,
        "pricing_out_per_m_usd": 14.0,
    }
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd == 7.0
    assert card.pricing_out_per_m_usd == 14.0


# ===========================================================================
# §2 — Oracle fills when API pricing absent
# ===========================================================================


def test_oracle_fills_both_when_api_silent() -> None:
    """The soak #6 root cause: DW returns id only, no pricing."""
    raw = {"id": "Qwen-3.5-397B-A17B"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # Seed pattern qwen_3_5_397b — $0.10 in / $0.40 out
    assert card.pricing_in_per_m_usd == 0.10
    assert card.pricing_out_per_m_usd == 0.40


def test_oracle_resolves_via_generic_family() -> None:
    """Unknown Qwen variant → generic family fallback."""
    raw = {"id": "qwen-some-unknown-variant-99b"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd == 0.20
    assert card.pricing_out_per_m_usd == 0.60


def test_oracle_resolves_deepseek_v3() -> None:
    raw = {"id": "deepseek-v3-chat"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd == 0.27
    assert card.pricing_out_per_m_usd == 1.10


# ===========================================================================
# §3 — Oracle fills only the missing side
# ===========================================================================


def test_oracle_fills_only_missing_input_side() -> None:
    """API supplies output but not input → Oracle fills input only."""
    raw = {
        "id": "Qwen-3.5-397B-A17B",
        "pricing": {"output": 99.0},  # only output present
    }
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # Input filled by Oracle ($0.10), output kept from API ($99.0)
    assert card.pricing_in_per_m_usd == 0.10
    assert card.pricing_out_per_m_usd == 99.0


def test_oracle_fills_only_missing_output_side() -> None:
    raw = {
        "id": "Qwen-3.5-397B-A17B",
        "pricing": {"input": 99.0},  # only input present
    }
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd == 99.0
    assert card.pricing_out_per_m_usd == 0.40


# ===========================================================================
# §4 — Oracle miss → both stay None → ambiguous (legacy)
# ===========================================================================


def test_oracle_miss_leaves_both_none() -> None:
    raw = {"id": "anthropic-private-claude-99-internal"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # No seed pattern matches anthropic-* → both None
    assert card.pricing_in_per_m_usd is None
    assert card.pricing_out_per_m_usd is None


def test_oracle_miss_preserves_ambiguous_quarantine() -> None:
    """Legacy quarantine path is preserved for unknown families."""
    raw = {"id": "unknown-vendor-private-model-x"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # parameter_count_b also None (no parseable size hint) → ambiguous
    assert card.has_ambiguous_metadata() is True


# ===========================================================================
# §5 — Oracle hit ⇒ has_ambiguous_metadata() False (THE BUG FIX)
# ===========================================================================


def test_oracle_hit_breaks_ambiguous_quarantine() -> None:
    """The whole point of Option α: Qwen 3.5 397B with NO API pricing
    was getting SPECULATIVE-quarantined. The Oracle hit must now make
    has_ambiguous_metadata() return False so BG-route admits it."""
    raw = {"id": "Qwen-3.5-397B-A17B"}
    # Even WITHOUT a parseable parameter count, pricing alone breaks
    # the ambiguous-metadata quarantine because has_ambiguous_metadata()
    # returns ``parameter_count_b is None AND pricing_out_per_m_usd is
    # None``. Oracle fills pricing → the AND gate opens.
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_out_per_m_usd is not None
    assert card.has_ambiguous_metadata() is False


# ===========================================================================
# §6 — Master-off short-circuits to legacy behavior
# ===========================================================================


def test_master_off_restores_legacy_blindspot(monkeypatch) -> None:
    """When ``JARVIS_PRICING_ORACLE_ENABLED=false``, Oracle returns
    None for everything → catalog parse falls back to ambiguous."""
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", "false")
    raw = {"id": "Qwen-3.5-397B-A17B"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # Without Oracle, no API pricing → both None → ambiguous (the bug)
    assert card.pricing_in_per_m_usd is None
    assert card.pricing_out_per_m_usd is None


def test_master_off_does_not_consult_oracle(monkeypatch) -> None:
    """Even with a custom-registered pattern, master-off skips it."""
    monkeypatch.setenv("JARVIS_PRICING_ORACLE_ENABLED", "false")
    register_pricing_pattern(
        PricingPattern("custom_test", "*test*", 1.0, 2.0),
    )
    raw = {"id": "test-model-x"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd is None
    assert card.pricing_out_per_m_usd is None


# ===========================================================================
# §7 — Oracle exception swallowed defensively
# ===========================================================================


def test_oracle_exception_does_not_break_catalog_parse(monkeypatch) -> None:
    """If resolve_pricing somehow raises, ModelCard parse must still
    succeed (defensive try/except in the hook)."""
    def boom(_):
        raise RuntimeError("simulated oracle failure")

    monkeypatch.setattr(
        pricing_oracle, "resolve_pricing", boom,
    )
    raw = {"id": "Qwen-3.5-397B-A17B"}
    # Must not raise
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    # Both still None — Oracle short-circuited via exception
    assert card.pricing_in_per_m_usd is None
    assert card.pricing_out_per_m_usd is None


# ===========================================================================
# §8 — Custom-registered patterns flow through to ModelCard
# ===========================================================================


def test_runtime_registered_pattern_flows_through() -> None:
    """Operators can register patterns at runtime and they take effect
    on the next ModelCard parse."""
    register_pricing_pattern(
        PricingPattern(
            "custom_runtime",
            "*acme-corp*",
            pricing_in_per_m_usd=0.42,
            pricing_out_per_m_usd=1.42,
        ),
    )
    raw = {"id": "acme-corp-xyz-7b"}
    card = ModelCard.from_api_dict(raw)
    assert card is not None
    assert card.pricing_in_per_m_usd == 0.42
    assert card.pricing_out_per_m_usd == 1.42
