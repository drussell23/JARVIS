"""Aegis pricing — yaml + env fallback + floor resolution."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.aegis.pricing import (
    ENV_AEGIS_POLICY_YAML_PATH,
    TokenPrice,
    cost_per_token_usd,
    cost_per_token_usd_sync,
    reset_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _isolate_pricing_cache():
    reset_cache_for_tests()
    yield
    reset_cache_for_tests()


def test_tokenprice_cost_for_arithmetic():
    p = TokenPrice(input_usd_per_token=3e-6, output_usd_per_token=15e-6)
    cost = p.cost_for(input_tokens=1000, output_tokens=500)
    # 1000 * 3e-6 + 500 * 15e-6 = 0.003 + 0.0075 = 0.0105
    assert cost == pytest.approx(0.0105)


async def test_cost_lookup_uses_yaml_when_available(monkeypatch, tmp_path):
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(
        "s2_pricing:\n"
        "  routes:\n"
        "    standard:\n"
        "      'test-model':\n"
        "        input: 0.000002\n"
        "        output: 0.000010\n"
        "  default_per_token_usd:\n"
        "    input: 0.0000003\n"
        "    output: 0.000001\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_AEGIS_POLICY_YAML_PATH, str(yaml_path))

    price = await cost_per_token_usd(route="standard", model="test-model")
    assert price.input_usd_per_token == pytest.approx(2e-6)
    assert price.output_usd_per_token == pytest.approx(1e-5)


async def test_cost_lookup_falls_back_to_yaml_default(monkeypatch, tmp_path):
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text(
        "s2_pricing:\n"
        "  routes:\n"
        "    standard:\n"
        "      'other-model':\n"
        "        input: 0.000002\n"
        "        output: 0.000010\n"
        "  default_per_token_usd:\n"
        "    input: 0.0000003\n"
        "    output: 0.000001\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_AEGIS_POLICY_YAML_PATH, str(yaml_path))

    # Asking for a model NOT in the yaml falls back to default_per_token_usd.
    price = await cost_per_token_usd(route="standard", model="unmapped")
    assert price.input_usd_per_token == pytest.approx(3e-7)
    assert price.output_usd_per_token == pytest.approx(1e-6)


async def test_cost_lookup_falls_back_to_env_for_qwen(monkeypatch, tmp_path):
    # No yaml — should hit env table for Qwen.
    monkeypatch.setenv(ENV_AEGIS_POLICY_YAML_PATH, str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("DOUBLEWORD_INPUT_COST_PER_M", "0.10")
    monkeypatch.setenv("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40")

    price = await cost_per_token_usd(route="standard", model="Qwen/Qwen3.5-397B")
    assert price.input_usd_per_token == pytest.approx(1e-7)   # $0.10 / M
    assert price.output_usd_per_token == pytest.approx(4e-7)  # $0.40 / M


async def test_cost_lookup_falls_back_to_floor_on_total_miss(monkeypatch, tmp_path):
    # No yaml, no env — must return the conservative floor (never $0).
    monkeypatch.setenv(ENV_AEGIS_POLICY_YAML_PATH, str(tmp_path / "absent.yaml"))
    monkeypatch.delenv("DOUBLEWORD_INPUT_COST_PER_M", raising=False)
    monkeypatch.delenv("DOUBLEWORD_OUTPUT_COST_PER_M", raising=False)

    price = await cost_per_token_usd(route="standard", model="totally-unknown")
    # Floor is conservative (overestimates), so > 0 always.
    assert price.input_usd_per_token > 0
    assert price.output_usd_per_token > 0


async def test_cost_lookup_never_raises_on_malformed_yaml(monkeypatch, tmp_path):
    yaml_path = tmp_path / "policy.yaml"
    yaml_path.write_text("not: valid: yaml: : :", encoding="utf-8")
    monkeypatch.setenv(ENV_AEGIS_POLICY_YAML_PATH, str(yaml_path))

    # Must not raise; falls through to floor.
    price = await cost_per_token_usd(route="standard", model="unknown")
    assert price.input_usd_per_token > 0


def test_sync_lookup_without_warmed_cache_uses_env_or_floor(monkeypatch, tmp_path):
    monkeypatch.delenv("DOUBLEWORD_INPUT_COST_PER_M", raising=False)
    monkeypatch.delenv("DOUBLEWORD_OUTPUT_COST_PER_M", raising=False)
    price = cost_per_token_usd_sync(route="standard", model="anything")
    assert price.input_usd_per_token > 0


async def test_real_policy_yaml_resolves_known_route_model(monkeypatch):
    """If the actual brain_selection_policy.yaml is reachable, a known
    route+model pair should resolve to non-floor values."""
    monkeypatch.delenv(ENV_AEGIS_POLICY_YAML_PATH, raising=False)
    price = await cost_per_token_usd(
        route="ide", model="claude-sonnet-4-20250514",
    )
    # Expect Sonnet pricing: $3/M input, $15/M output -> 3e-6 / 1.5e-5
    assert price.input_usd_per_token == pytest.approx(3e-6, rel=0.01)
    assert price.output_usd_per_token == pytest.approx(1.5e-5, rel=0.01)
