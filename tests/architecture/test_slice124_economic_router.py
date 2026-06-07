"""Slice 124 — Autonomous Economic Failover Router.

Proves the bounded economic decision: micro read-only ops cascade to the cheap
tier on a hard 402/429; massive ops queue; mutating ops still require opt-in;
no hardcoded model (cheap tier resolved from env).
"""

from __future__ import annotations

from backend.core.ouroboros.governance import economic_router as ER
from backend.core.ouroboros.governance.economic_router import EconomicAction as A


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_ECONOMIC_ROUTER_ENABLED", raising=False)
    assert ER.economic_router_enabled() is False
    monkeypatch.setenv("JARVIS_ECONOMIC_ROUTER_ENABLED", "1")
    assert ER.economic_router_enabled() is True


class TestErrorClassification:
    def test_402_variants(self):
        for t in ["http_402", "Account balance too low. Please add credits.",
                  "402 insufficient", "payment required"]:
            assert ER.is_hard_economic_block(t) == "402"

    def test_429_variants(self):
        for t in ["http 429", "rate limit exceeded", "Too Many Requests"]:
            assert ER.is_hard_economic_block(t) == "429"

    def test_non_economic(self):
        for t in ["live_transport:RuntimeError", "TransferEncodingError", "", None, "parse error"]:
            assert ER.is_hard_economic_block(t) is None


class TestTokenEstimate:
    def test_chars_to_tokens(self):
        assert ER.estimate_tokens(4000) == 1000
        assert ER.estimate_tokens(0) == 0


class TestNoHardcodedModel:
    def test_cheap_model_resolved_from_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ECONOMIC_FAILOVER_MODEL", raising=False)
        assert ER.economic_failover_model() == ""  # unset → empty (caller defaults)
        monkeypatch.setenv("JARVIS_ECONOMIC_FAILOVER_MODEL", "claude-haiku-4-5-20251001")
        assert ER.economic_failover_model() == "claude-haiku-4-5-20251001"


class TestDecision:
    def _enable(self, monkeypatch, micro=1500, model="claude-haiku-4-5-20251001"):
        monkeypatch.setenv("JARVIS_ECONOMIC_ROUTER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_ECONOMIC_MICRO_OP_TOKENS", str(micro))
        monkeypatch.setenv("JARVIS_ECONOMIC_FAILOVER_MODEL", model)
        monkeypatch.delenv("JARVIS_BACKGROUND_ALLOW_FALLBACK", raising=False)

    def test_disabled_is_noop(self, monkeypatch):
        monkeypatch.delenv("JARVIS_ECONOMIC_ROUTER_ENABLED", raising=False)
        d = ER.decide(route="background", error_text="http_402", prompt_chars=400, is_read_only=True)
        assert d.action is A.NO_OP

    def test_micro_readonly_402_cascades_cheap(self, monkeypatch):
        self._enable(monkeypatch)
        d = ER.decide(route="background", error_text="balance too low", prompt_chars=4000, is_read_only=True)
        assert d.action is A.CASCADE_CHEAP
        assert d.model == "claude-haiku-4-5-20251001"  # the cheap tier, from env
        assert d.tokens == 1000

    def test_massive_op_queues_even_if_readonly(self, monkeypatch):
        self._enable(monkeypatch, micro=1500)
        d = ER.decide(route="background", error_text="http_402", prompt_chars=40000, is_read_only=True)
        assert d.action is A.QUEUE  # 10k tokens > 1500 → don't pay Claude prices
        assert d.tokens == 10000

    def test_mutating_micro_op_queues_without_optin(self, monkeypatch):
        self._enable(monkeypatch)
        d = ER.decide(route="background", error_text="http_402", prompt_chars=400, is_read_only=False)
        assert d.action is A.QUEUE  # mutation safety preserved

    def test_mutating_micro_op_cascades_with_optin(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setenv("JARVIS_BACKGROUND_ALLOW_FALLBACK", "1")
        d = ER.decide(route="background", error_text="http_402", prompt_chars=400, is_read_only=False)
        assert d.action is A.CASCADE_CHEAP

    def test_non_economic_error_is_noop(self, monkeypatch):
        self._enable(monkeypatch)
        d = ER.decide(route="background", error_text="live_transport:RuntimeError", prompt_chars=400, is_read_only=True)
        assert d.action is A.NO_OP  # transport blip → existing logic owns it

    def test_standard_route_not_managed(self, monkeypatch):
        self._enable(monkeypatch)
        d = ER.decide(route="standard", error_text="http_402", prompt_chars=400, is_read_only=True)
        assert d.action is A.NO_OP  # STANDARD already cascades by policy

    def test_cheap_model_empty_when_env_unset(self, monkeypatch):
        monkeypatch.setenv("JARVIS_ECONOMIC_ROUTER_ENABLED", "1")
        monkeypatch.delenv("JARVIS_ECONOMIC_FAILOVER_MODEL", raising=False)
        d = ER.decide(route="background", error_text="http_402", prompt_chars=400, is_read_only=True)
        assert d.action is A.CASCADE_CHEAP
        assert d.model == ""  # caller composes the default fallback provider
