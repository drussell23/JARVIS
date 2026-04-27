"""Item #3 — production HypothesisProbe EvidenceProber + bridges pins.

Phase 7.6 (PR #23176) shipped the bounded HypothesisProbe primitive
with an injectable EvidenceProber Protocol + Null sentinel default.
This PR adds:
  * AnthropicVenomEvidenceProber (production prober with cost caps,
    allowlist enforcement, injectable provider, never-raise cage)
  * Bridges: confirmed → AdaptationLedger.propose; terminal →
    HypothesisLedger.record_outcome

Pinned cage:
  * Provider injection — _NullVenomQueryProvider as safe default
  * Tool allowlist enforcement (matches Phase 7.6 READONLY_TOOL_ALLOWLIST)
  * Per-call cost cap ($0.05) + cumulative session budget ($1.00)
  * Bounded sizes (prompt, evidence, prior-evidence)
  * NEVER raises (provider exception → error round)
  * Master flag default false
  * Bridges: master flag default false; CONFIRMED → propose;
    terminal → record_outcome; SKIPPED_* → no-op
  * Authority + cage invariants
"""
from __future__ import annotations

import ast
import time
from dataclasses import replace
from pathlib import Path
from typing import FrozenSet, List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    anthropic_venom_evidence_prober as prober_mod,
)
from backend.core.ouroboros.governance.adaptation import (
    hypothesis_probe_bridge as bridge_mod,
)
from backend.core.ouroboros.governance.adaptation.anthropic_venom_evidence_prober import (
    AnthropicVenomEvidenceProber,
    DEFAULT_COST_CAP_PER_CALL_USD,
    DEFAULT_SESSION_BUDGET_USD,
    MAX_EVIDENCE_CHARS_RETURNED,
    MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED,
    MAX_PRIOR_EVIDENCE_ROW_CHARS,
    MAX_PROMPT_CHARS,
    VenomQueryProvider,
    VenomQueryResult,
    _NullVenomQueryProvider,
    build_default_production_prober,
    is_production_prober_enabled,
)
from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
    HypothesisProbe,
    ProbeResult,
    ProbeRoundResult,
    ProbeVerdict,
    READONLY_TOOL_ALLOWLIST,
)
from backend.core.ouroboros.governance.adaptation.hypothesis_probe_bridge import (
    BridgeResult,
    BridgeStatus,
    bridge_confirmed_to_adaptation_ledger,
    bridge_to_hypothesis_ledger,
    is_bridges_enabled,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    ProposeStatus,
    reset_surface_validators,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Section A — module constants + master flag (prober)
# ---------------------------------------------------------------------------


class TestProberConstants:
    def test_default_cost_cap_per_call_is_005(self):
        assert DEFAULT_COST_CAP_PER_CALL_USD == 0.05

    def test_default_session_budget_is_1_00(self):
        assert DEFAULT_SESSION_BUDGET_USD == 1.00

    def test_max_prompt_chars(self):
        assert MAX_PROMPT_CHARS == 4096

    def test_max_evidence_chars_returned(self):
        assert MAX_EVIDENCE_CHARS_RETURNED == 3500

    def test_max_prior_evidence_rounds(self):
        assert MAX_PRIOR_EVIDENCE_ROUNDS_INCLUDED == 3

    def test_truthy_constant_shape(self):
        assert prober_mod._TRUTHY == ("1", "true", "yes", "on")


class TestProberMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED",
            raising=False,
        )
        assert is_production_prober_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED", v,
            )
            assert is_production_prober_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED", v,
            )
            assert is_production_prober_enabled() is False, v


# ---------------------------------------------------------------------------
# Section B — Null sentinel + factory
# ---------------------------------------------------------------------------


class TestNullProvider:
    def test_null_provider_zero_cost(self):
        p = _NullVenomQueryProvider()
        r = p.query(
            prompt="x", allowed_tools=READONLY_TOOL_ALLOWLIST,
            max_cost_usd=0.05,
        )
        assert r.cost_usd == 0.0
        assert r.response_text == ""
        assert r.error is None

    def test_factory_returns_null_when_master_off(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED",
            raising=False,
        )
        # Even with explicit provider, master-off → Null sentinel.
        custom = mock.MagicMock(spec=VenomQueryProvider)
        prober = build_default_production_prober(custom)
        # Run 1 round and confirm provider was NOT called.
        prober.probe("c", "e", ())
        custom.query.assert_not_called()

    def test_factory_returns_null_when_no_provider(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED", "1",
        )
        prober = build_default_production_prober(provider=None)
        # Run 1 round → no exception, no cost.
        r = prober.probe("c", "e", ())
        assert prober.cumulative_cost_usd == 0.0

    def test_factory_wires_provider_when_master_on(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_PRODUCTION_PROBER_ENABLED", "1",
        )
        called: List[Tuple] = []

        class _Spy:
            def query(self, *, prompt, allowed_tools, max_cost_usd):
                called.append((prompt, allowed_tools, max_cost_usd))
                return VenomQueryResult(
                    response_text="VERDICT: continue",
                    cost_usd=0.001,
                )

        prober = build_default_production_prober(_Spy())
        prober.probe("c", "e", ())
        assert len(called) == 1


# ---------------------------------------------------------------------------
# Section C — Prompt building + parsing
# ---------------------------------------------------------------------------


class TestPromptBuilding:
    def test_prompt_includes_claim_and_expected(self):
        p = prober_mod._build_prompt("the claim", "the expected", ())
        assert "the claim" in p
        assert "the expected" in p

    def test_prompt_lists_allowed_tools(self):
        p = prober_mod._build_prompt("c", "e", ())
        for tool in READONLY_TOOL_ALLOWLIST:
            assert tool in p

    def test_prompt_includes_prior_evidence(self):
        p = prober_mod._build_prompt(
            "c", "e", ("prior round 1 evidence",),
        )
        assert "prior round 1 evidence" in p

    def test_prompt_caps_prior_evidence_rounds(self):
        # >3 rounds → only most-recent 3 included
        prior = tuple(f"round{i}_text" for i in range(10))
        p = prober_mod._build_prompt("c", "e", prior)
        # Most-recent 3: round7, round8, round9
        for i in (7, 8, 9):
            assert f"round{i}_text" in p
        for i in (0, 1, 2, 3):
            assert f"round{i}_text" not in p

    def test_prompt_truncates_long_prior_rows(self):
        big = "X" * (MAX_PRIOR_EVIDENCE_ROW_CHARS + 100)
        p = prober_mod._build_prompt("c", "e", (big,))
        # Big row truncated; presence of "...(truncated)" marker.
        assert "...(truncated)" in p

    def test_prompt_capped_at_max(self):
        # Build huge inputs; prompt MUST stay ≤ MAX_PROMPT_CHARS.
        big_claim = "X" * 10000
        p = prober_mod._build_prompt(
            big_claim, "e", (),
        )
        assert len(p) <= MAX_PROMPT_CHARS


class TestResponseParsing:
    def test_parse_continue_default(self):
        verdict, _ = prober_mod._parse_response(
            "Some narrative without sentinel.",
        )
        assert verdict == "continue"

    def test_parse_confirmed(self):
        verdict, _ = prober_mod._parse_response(
            "Found it at foo.py:42.\nVERDICT: confirmed",
        )
        assert verdict == "confirmed"

    def test_parse_refuted(self):
        verdict, _ = prober_mod._parse_response(
            "Searched everywhere.\nVERDICT: refuted",
        )
        assert verdict == "refuted"

    def test_parse_continue_explicit(self):
        verdict, _ = prober_mod._parse_response(
            "Need more rounds.\nVERDICT: continue",
        )
        assert verdict == "continue"

    def test_parse_last_sentinel_wins(self):
        # Multiple sentinels — final one wins.
        verdict, _ = prober_mod._parse_response(
            "VERDICT: continue\n"
            "(more investigation)\n"
            "VERDICT: confirmed"
        )
        assert verdict == "confirmed"

    def test_parse_case_insensitive(self):
        verdict, _ = prober_mod._parse_response(
            "VerDicT: ConfirmeD"
        )
        assert verdict == "confirmed"

    def test_parse_evidence_truncated(self):
        big = "X" * (MAX_EVIDENCE_CHARS_RETURNED + 100)
        _, evidence = prober_mod._parse_response(big)
        assert len(evidence) <= MAX_EVIDENCE_CHARS_RETURNED

    def test_parse_empty_response(self):
        verdict, evidence = prober_mod._parse_response("")
        assert verdict == "continue"
        assert evidence == ""


# ---------------------------------------------------------------------------
# Section D — Cost cap + session budget
# ---------------------------------------------------------------------------


class _FixedCostProvider:
    """Provider that always reports a fixed cost per call."""

    def __init__(self, cost_per_call_usd: float, response: str = "VERDICT: continue"):
        self._cost = cost_per_call_usd
        self._response = response
        self.calls = 0

    def query(self, *, prompt, allowed_tools, max_cost_usd):
        self.calls += 1
        return VenomQueryResult(
            response_text=self._response,
            cost_usd=self._cost,
        )


class TestCostAccounting:
    def test_cumulative_cost_tracked(self):
        p = AnthropicVenomEvidenceProber(
            provider=_FixedCostProvider(0.01),
        )
        p.probe("c", "e", ())
        p.probe("c", "e", ())
        p.probe("c", "e", ())
        assert abs(p.cumulative_cost_usd - 0.03) < 1e-9

    def test_per_call_cap_clips_provider_overrun(self):
        # Provider violates contract — reports cost > per-call cap.
        # Prober must clip to cap.
        p = AnthropicVenomEvidenceProber(
            provider=_FixedCostProvider(99.0),
            cost_cap_per_call_usd=0.05,
        )
        p.probe("c", "e", ())
        # Clipped to 0.05.
        assert abs(p.cumulative_cost_usd - 0.05) < 1e-9

    def test_session_budget_prevents_further_calls(self):
        provider = _FixedCostProvider(0.04)
        p = AnthropicVenomEvidenceProber(
            provider=provider,
            cost_cap_per_call_usd=0.05,
            session_budget_usd=0.10,
        )
        # Round 1: cumulative 0.04 + cap 0.05 = 0.09 < 0.10 → call OK
        # After call: cumulative=0.04
        # Round 2: cumulative 0.04 + cap 0.05 = 0.09 < 0.10 → call OK
        # After call: cumulative=0.08
        # Round 3: cumulative 0.08 + cap 0.05 = 0.13 > 0.10 → SKIP
        p.probe("c", "e", ())
        p.probe("c", "e", ())
        r3 = p.probe("c", "e", ())
        assert provider.calls == 2  # 3rd call didn't happen
        assert "session_budget" in r3.notes
        assert p.budget_exhausted is True

    def test_pre_check_fires_when_budget_exhausted(self):
        provider = _FixedCostProvider(0.05)
        p = AnthropicVenomEvidenceProber(
            provider=provider,
            cost_cap_per_call_usd=0.05,
            session_budget_usd=0.05,
        )
        # Round 1: 0.0 + 0.05 = 0.05 NOT > 0.05 → call OK; after: 0.05
        # Round 2: 0.05 + 0.05 = 0.10 > 0.05 → SKIP
        r1 = p.probe("c", "e", ())
        r2 = p.probe("c", "e", ())
        assert provider.calls == 1
        assert "session_budget" in r2.notes


# ---------------------------------------------------------------------------
# Section E — Provider exception handling
# ---------------------------------------------------------------------------


class _RaisingProvider:
    def query(self, *, prompt, allowed_tools, max_cost_usd):
        raise RuntimeError("simulated provider failure")


class TestExceptionHandling:
    def test_provider_raise_caught(self):
        p = AnthropicVenomEvidenceProber(provider=_RaisingProvider())
        r = p.probe("c", "e", ())
        assert r.verdict_signal == "continue"
        assert r.evidence == ""
        assert "RuntimeError" in r.notes
        assert "provider_error" in r.notes

    def test_provider_error_field_caught(self):
        # Provider returns error field (not raise).
        class _ErrProvider:
            def query(self, **kw):
                return VenomQueryResult(
                    response_text="",
                    cost_usd=0.0,
                    error="rate_limit",
                )
        p = AnthropicVenomEvidenceProber(provider=_ErrProvider())
        r = p.probe("c", "e", ())
        assert r.verdict_signal == "continue"
        assert "rate_limit" in r.notes


# ---------------------------------------------------------------------------
# Section F — Tool allowlist enforcement
# ---------------------------------------------------------------------------


class TestToolAllowlistEnforcement:
    def test_provider_called_with_readonly_allowlist(self):
        captured: List[FrozenSet[str]] = []

        class _Capture:
            def query(self, *, prompt, allowed_tools, max_cost_usd):
                captured.append(allowed_tools)
                return VenomQueryResult("VERDICT: continue", 0.001)

        p = AnthropicVenomEvidenceProber(provider=_Capture())
        p.probe("c", "e", ())
        assert len(captured) == 1
        assert captured[0] == READONLY_TOOL_ALLOWLIST


# ---------------------------------------------------------------------------
# Section G — End-to-end with HypothesisProbe runner
# ---------------------------------------------------------------------------


class TestRunnerIntegration:
    def test_runner_uses_production_prober(self, monkeypatch):
        # Wire the production prober into HypothesisProbe runner.
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "1")

        class _Confirm:
            def query(self, *, prompt, allowed_tools, max_cost_usd):
                return VenomQueryResult(
                    "Found at foo.py:42\nVERDICT: confirmed",
                    cost_usd=0.001,
                )

        prober = AnthropicVenomEvidenceProber(provider=_Confirm())
        runner = HypothesisProbe(prober=prober)
        result = runner.test("Is X true?", "X visible at foo.py")
        assert result.verdict == ProbeVerdict.CONFIRMED
        assert "foo.py:42" in result.final_evidence

    def test_runner_with_null_provider_terminates_diminishing(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "1")
        prober = AnthropicVenomEvidenceProber()  # Null default
        runner = HypothesisProbe(prober=prober)
        result = runner.test("c", "e")
        assert result.verdict == ProbeVerdict.INCONCLUSIVE_DIMINISHING

    def test_runner_with_refuting_provider(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "1")

        class _Refute:
            def query(self, *, prompt, allowed_tools, max_cost_usd):
                return VenomQueryResult(
                    "Searched everywhere; not found.\nVERDICT: refuted",
                    cost_usd=0.002,
                )

        prober = AnthropicVenomEvidenceProber(provider=_Refute())
        runner = HypothesisProbe(prober=prober)
        result = runner.test("c", "e")
        assert result.verdict == ProbeVerdict.REFUTED


# ---------------------------------------------------------------------------
# Section H — bridges: master flag + skip paths
# ---------------------------------------------------------------------------


class TestBridgesMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", raising=False,
        )
        assert is_bridges_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", v,
            )
            assert is_bridges_enabled() is True, v


def _confirmed_probe_result():
    return ProbeResult(
        verdict=ProbeVerdict.CONFIRMED, rounds=1, elapsed_s=0.1,
        evidence_hashes=("sha256:abc",),
        final_evidence="found it at foo.py:42",
        notes="cost=0.001",
    )


def _refuted_probe_result():
    return ProbeResult(
        verdict=ProbeVerdict.REFUTED, rounds=2, elapsed_s=0.2,
        evidence_hashes=("sha256:def",),
        final_evidence="not found anywhere",
    )


def _inconclusive_probe_result():
    return ProbeResult(
        verdict=ProbeVerdict.INCONCLUSIVE_BUDGET, rounds=5,
        elapsed_s=1.0,
        notes="call_cap=5",
    )


class TestAdaptationLedgerBridge:
    def test_master_off_skips(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", raising=False,
        )
        result = bridge_confirmed_to_adaptation_ledger(
            _confirmed_probe_result(),
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            proposal_id="adapt-bridge-1",
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
        )
        assert result.status is BridgeStatus.SKIPPED_MASTER_OFF

    def test_non_confirmed_skips(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        for verdict in (
            _refuted_probe_result(),
            _inconclusive_probe_result(),
        ):
            result = bridge_confirmed_to_adaptation_ledger(
                verdict,
                surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
                proposal_kind="add_pattern",
                proposal_id="adapt-bridge-x",
                current_state_hash="sha256:abc",
                proposed_state_hash="sha256:def",
            )
            assert result.status is BridgeStatus.SKIPPED_NOT_CONFIRMED

    def test_empty_proposal_id_invalid(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        result = bridge_confirmed_to_adaptation_ledger(
            _confirmed_probe_result(),
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            proposal_id="   ",
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
        )
        assert result.status is BridgeStatus.INVALID_INPUT


@pytest.fixture
def reset_validators_for_bridge():
    reset_surface_validators()
    yield
    reset_surface_validators()


@pytest.fixture
def fresh_ledger(tmp_path, monkeypatch, reset_validators_for_bridge):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    return AdaptationLedger(path=tmp_path / "ledger.jsonl")


class TestAdaptationLedgerBridgeOK:
    def test_confirmed_creates_proposal(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        payload = {"name": "test_pattern", "regex": "X"}
        result = bridge_confirmed_to_adaptation_ledger(
            _confirmed_probe_result(),
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            proposal_id="adapt-bridge-ok",
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload=payload,
            ledger=fresh_ledger,
        )
        assert result.status is BridgeStatus.OK
        loaded = fresh_ledger.get("adapt-bridge-ok")
        assert loaded is not None
        # Item #2 payload survives (proves end-to-end with Item #2).
        assert loaded.proposed_state_payload == payload

    def test_evidence_summary_falls_back_when_unspecified(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        result = bridge_confirmed_to_adaptation_ledger(
            _confirmed_probe_result(),
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            proposal_id="adapt-bridge-fallback",
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            ledger=fresh_ledger,
        )
        assert result.status is BridgeStatus.OK
        loaded = fresh_ledger.get("adapt-bridge-fallback")
        # Default summary mentions "hypothesis confirmed".
        assert "hypothesis confirmed" in loaded.evidence.summary


# ---------------------------------------------------------------------------
# Section I — HypothesisLedger bridge
# ---------------------------------------------------------------------------


class TestHypothesisLedgerBridge:
    def test_master_off_skips(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", raising=False,
        )
        result = bridge_to_hypothesis_ledger(
            _confirmed_probe_result(), "hyp-1", mock.MagicMock(),
        )
        assert result.status is BridgeStatus.SKIPPED_MASTER_OFF

    def test_skipped_verdicts_skip(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        skipped = ProbeResult(
            verdict=ProbeVerdict.SKIPPED_MASTER_OFF,
            rounds=0, elapsed_s=0.0,
        )
        result = bridge_to_hypothesis_ledger(
            skipped, "hyp-1", mock.MagicMock(),
        )
        assert result.status is BridgeStatus.SKIPPED_NON_TERMINAL

    def test_confirmed_records_validated_True(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        ledger = mock.MagicMock()
        ledger.record_outcome.return_value = True
        result = bridge_to_hypothesis_ledger(
            _confirmed_probe_result(), "hyp-1", ledger,
        )
        assert result.status is BridgeStatus.OK
        ledger.record_outcome.assert_called_once()
        args = ledger.record_outcome.call_args
        # validated arg = True
        assert args[0][2] is True or args.kwargs.get("validated") is True

    def test_refuted_records_validated_False(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        ledger = mock.MagicMock()
        ledger.record_outcome.return_value = True
        result = bridge_to_hypothesis_ledger(
            _refuted_probe_result(), "hyp-1", ledger,
        )
        assert result.status is BridgeStatus.OK
        # validated arg = False
        args = ledger.record_outcome.call_args
        assert args[0][2] is False or args.kwargs.get("validated") is False

    def test_inconclusive_records_validated_None(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        ledger = mock.MagicMock()
        ledger.record_outcome.return_value = True
        result = bridge_to_hypothesis_ledger(
            _inconclusive_probe_result(), "hyp-1", ledger,
        )
        assert result.status is BridgeStatus.OK
        args = ledger.record_outcome.call_args
        assert args[0][2] is None or args.kwargs.get("validated") is None

    def test_record_outcome_returns_false_hypothesis_not_found(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        ledger = mock.MagicMock()
        ledger.record_outcome.return_value = False
        result = bridge_to_hypothesis_ledger(
            _confirmed_probe_result(), "hyp-missing", ledger,
        )
        assert result.status is BridgeStatus.HYPOTHESIS_NOT_FOUND

    def test_record_outcome_raises_caught(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_HYPOTHESIS_PROBE_BRIDGES_ENABLED", "1",
        )
        ledger = mock.MagicMock()
        ledger.record_outcome.side_effect = RuntimeError("boom")
        result = bridge_to_hypothesis_ledger(
            _confirmed_probe_result(), "hyp-1", ledger,
        )
        assert result.status is BridgeStatus.LEDGER_FAILED


# ---------------------------------------------------------------------------
# Section J — Authority + cage invariants
# ---------------------------------------------------------------------------


_PROBER_PATH = Path(prober_mod.__file__)
_BRIDGE_PATH = Path(bridge_mod.__file__)


class TestAuthorityInvariants:
    def test_prober_no_banned_governance_imports(self):
        source = _PROBER_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )

    def test_prober_only_stdlib_and_adaptation(self):
        source = _PROBER_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "logging", "os", "dataclasses", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    assert "adaptation" in node.module, (
                        f"non-adaptation backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_prober_no_subprocess_or_anthropic_direct(self):
        source = _PROBER_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
            # Critical: prober does NOT import anthropic directly —
            # provider injection is the network boundary.
            "import anthropic",
            "from anthropic",
        ):
            assert token not in source, f"banned token: {token}"

    def test_bridge_no_banned_imports(self):
        source = _BRIDGE_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )

    def test_bridge_only_stdlib_and_adaptation(self):
        source = _BRIDGE_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "logging", "os", "dataclasses",
            "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    assert "adaptation" in node.module, (
                        f"non-adaptation backend import: {node.module}"
                    )
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_prober_uses_substrate_allowlist_constant(self):
        # Pin: prober imports READONLY_TOOL_ALLOWLIST from the
        # Phase 7.6 substrate (not redefined locally).
        source = _PROBER_PATH.read_text()
        assert "READONLY_TOOL_ALLOWLIST" in source
        assert (
            "from backend.core.ouroboros.governance.adaptation"
            ".hypothesis_probe import" in source
        )
