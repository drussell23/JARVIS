"""M10 Slice 3 — ProposalSynthesizer tests (PRD §32.4.2).

Pins:
  § 1 — Master flag gate (DISABLED verdict)
  § 2 — Closed-taxonomy SynthesisVerdict (7 values)
  § 3 — Frozen result containers
  § 4 — Env knobs — clamping + defaults
  § 5 — Mandatory AST self-pin (NO_SELF_PIN gate)
  § 6 — K-way Quorum consensus (SYNTHESIZED on majority)
  § 7 — Quorum disagreement (no majority signature)
  § 8 — Provider error isolation
  § 9 — INSUFFICIENT_CONTEXT short-circuit
  § 10 — SKIPPED_KIND (ProposalKind.DISABLED)
  § 11 — Timeout per-call enforcement
  § 12 — Risk tier forced to APPROVAL_REQUIRED
  § 13 — Cost accumulation
  § 14 — Authority floor (no orchestrator/iron_gate imports)
  § 15 — Public exports
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest


def _enable(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )


def _make_record(
    *,
    proposal_id="m10-test-1",
    kind=None,
    detection_evidence=("signal-A", "op-kind-X"),
):
    from backend.core.ouroboros.governance.m10.primitives import (
        M10ProposalRecord, ProposalKind,
    )
    return M10ProposalRecord(
        proposal_id=proposal_id,
        kind=kind or ProposalKind.NEW_SENSOR,
        detection_evidence=tuple(detection_evidence),
    )


# ---------------------------------------------------------------------------
# Stub providers — caller-injected for clean unit testing
# ---------------------------------------------------------------------------


def _make_stub(*, code_text, ast_pin="my_pin",
               class_name="X", module_path="backend/x.py",
               cost=0.005):
    from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
        SynthesisCandidate,
    )

    class _Stub:
        async def synthesize_one(self, **_):
            return SynthesisCandidate(
                code_text=code_text,
                class_name=class_name,
                module_path=module_path,
                ast_pin_name=ast_pin,
                cost_usd=cost,
            )
    return _Stub()


# ---------------------------------------------------------------------------
# § 1 — Master flag gate
# ---------------------------------------------------------------------------


class TestMasterFlagGate:
    @pytest.mark.asyncio
    async def test_disabled_returns_disabled_verdict(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_make_stub(code_text="def f(): pass"),
        )
        assert result.verdict is SynthesisVerdict.DISABLED


# ---------------------------------------------------------------------------
# § 2 — Closed taxonomy
# ---------------------------------------------------------------------------


class TestSynthesisVerdict:
    def test_exactly_seven_values(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesisVerdict,
        )
        values = {m.value for m in SynthesisVerdict}
        assert values == {
            "synthesized",
            "quorum_disagreement",
            "no_self_pin",
            "provider_error",
            "insufficient_context",
            "disabled",
            "skipped_kind",
        }

    def test_str_subclass(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesisVerdict,
        )
        assert issubclass(SynthesisVerdict, str)


# ---------------------------------------------------------------------------
# § 3 — Frozen result containers
# ---------------------------------------------------------------------------


class TestFrozenContainers:
    def test_candidate_is_frozen(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesisCandidate,
        )
        c = SynthesisCandidate(code_text="def f(): pass")
        with pytest.raises(Exception):
            c.code_text = "x"  # type: ignore[misc]

    def test_proposal_is_frozen(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesizedProposal, SynthesisVerdict,
        )
        from backend.core.ouroboros.governance.m10.primitives import (
            ProposalKind,
        )
        p = SynthesizedProposal(
            proposal_id="x",
            kind=ProposalKind.NEW_SENSOR,
            verdict=SynthesisVerdict.DISABLED,
        )
        with pytest.raises(Exception):
            p.verdict = SynthesisVerdict.SYNTHESIZED  # type: ignore[misc]

    def test_to_dict_complete(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            SynthesizedProposal, SynthesisVerdict,
        )
        from backend.core.ouroboros.governance.m10.primitives import (
            ProposalKind,
        )
        p = SynthesizedProposal(
            proposal_id="x",
            kind=ProposalKind.NEW_SENSOR,
            verdict=SynthesisVerdict.SYNTHESIZED,
            code_text="def f(): pass",
            class_name="C", module_path="m.py",
            ast_pin_name="pin", cost_usd=0.015,
            candidate_signatures=("a", "b", "c"),
        )
        d = p.to_dict()
        for key in (
            "schema_version", "proposal_id", "kind",
            "verdict", "code_text_len", "class_name",
            "module_path", "ast_pin_name",
            "consensus_signature", "candidate_count",
            "candidate_signatures", "cost_usd",
            "forced_risk_tier", "elapsed_s", "diagnostic",
        ):
            assert key in d
        assert d["forced_risk_tier"] == "approval_required"


# ---------------------------------------------------------------------------
# § 4 — Env knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_quorum_k_default_3(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_SYNTHESIS_QUORUM_K", raising=False,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            m10_synthesis_quorum_k,
        )
        assert m10_synthesis_quorum_k() == 3

    def test_majority_default_2(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_SYNTHESIS_MAJORITY_THRESHOLD",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            m10_synthesis_majority_threshold,
        )
        assert m10_synthesis_majority_threshold() == 2

    def test_per_call_timeout_default_60(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_SYNTHESIS_PER_CALL_TIMEOUT_S",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            m10_synthesis_per_call_timeout_s,
        )
        assert m10_synthesis_per_call_timeout_s() == 60.0

    def test_evidence_chars_default_4096(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_M10_SYNTHESIS_MAX_EVIDENCE_CHARS",
            raising=False,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            m10_synthesis_max_evidence_chars,
        )
        assert m10_synthesis_max_evidence_chars() == 4096

    def test_clamping(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_M10_SYNTHESIS_QUORUM_K", "0",
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            m10_synthesis_quorum_k,
        )
        assert m10_synthesis_quorum_k() == 1  # floor


# ---------------------------------------------------------------------------
# § 5 — Mandatory AST self-pin gate
# ---------------------------------------------------------------------------


class TestMandatoryASTPin:
    @pytest.mark.asyncio
    async def test_no_pin_rejected(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_make_stub(
                code_text="def foo(): return 1",
                ast_pin="",  # MISSING
            ),
        )
        assert result.verdict is SynthesisVerdict.NO_SELF_PIN

    @pytest.mark.asyncio
    async def test_whitespace_only_pin_rejected(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_make_stub(
                code_text="def foo(): return 1",
                ast_pin="   ",  # whitespace
            ),
        )
        assert result.verdict is SynthesisVerdict.NO_SELF_PIN


# ---------------------------------------------------------------------------
# § 6 — Quorum consensus (SYNTHESIZED)
# ---------------------------------------------------------------------------


class TestQuorumConsensus:
    @pytest.mark.asyncio
    async def test_3_of_3_consensus(self, monkeypatch):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_make_stub(
                code_text="def foo(): return 1",
                ast_pin="my_pin",
            ),
        )
        assert result.verdict is SynthesisVerdict.SYNTHESIZED
        assert result.candidate_count == 3
        assert len(result.consensus_signature) == 64
        assert result.ast_pin_name == "my_pin"

    @pytest.mark.asyncio
    async def test_2_of_3_majority_consensus(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisCandidate,
            SynthesisVerdict,
        )

        class _MajorityStub:
            async def synthesize_one(
                self, *, prompt, kind, proposal_id,
            ):
                idx = int(proposal_id.split("-")[-1])
                if idx in (0, 1):
                    return SynthesisCandidate(
                        code_text=(
                            "def shared_path():\n"
                            "    pass"
                        ),
                        class_name="A", module_path="a.py",
                        ast_pin_name="shared_pin",
                        cost_usd=0.005,
                    )
                # The 3rd candidate has structurally different
                # AST (different function name)
                return SynthesisCandidate(
                    code_text=(
                        "def different_function():\n"
                        "    return None"
                    ),
                    class_name="B", module_path="b.py",
                    ast_pin_name="diff_pin",
                    cost_usd=0.005,
                )

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_MajorityStub(),
        )
        assert result.verdict is SynthesisVerdict.SYNTHESIZED
        # Consensus picks the majority (idx 0 or 1)
        assert result.class_name == "A"
        assert result.ast_pin_name == "shared_pin"


# ---------------------------------------------------------------------------
# § 7 — Quorum disagreement (no majority)
# ---------------------------------------------------------------------------


class TestQuorumDisagreement:
    @pytest.mark.asyncio
    async def test_3_distinct_signatures_no_consensus(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisCandidate,
            SynthesisVerdict,
        )

        class _DiverseStub:
            async def synthesize_one(
                self, *, prompt, kind, proposal_id,
            ):
                idx = int(proposal_id.split("-")[-1])
                # Each candidate has a structurally different AST
                bodies = [
                    "def alpha(): pass",
                    "class Beta: pass",
                    "GAMMA = []",
                ]
                return SynthesisCandidate(
                    code_text=bodies[idx % 3],
                    class_name=f"X{idx}",
                    module_path=f"x{idx}.py",
                    ast_pin_name=f"pin_{idx}",
                    cost_usd=0.005,
                )

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_DiverseStub(),
        )
        assert result.verdict is (
            SynthesisVerdict.QUORUM_DISAGREEMENT
        )
        assert result.candidate_count == 3
        # 3 distinct signatures
        non_empty = [s for s in result.candidate_signatures if s]
        assert len(set(non_empty)) == 3


# ---------------------------------------------------------------------------
# § 8 — Provider error isolation
# ---------------------------------------------------------------------------


class TestProviderError:
    @pytest.mark.asyncio
    async def test_all_calls_raise_returns_provider_error(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )

        class _RaisingStub:
            async def synthesize_one(self, **_):
                raise RuntimeError("synthetic provider failure")

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_RaisingStub(),
        )
        assert result.verdict is (
            SynthesisVerdict.PROVIDER_ERROR
        )
        assert result.candidate_count == 3

    @pytest.mark.asyncio
    async def test_none_provider_returns_provider_error(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(), provider=None,
        )
        assert result.verdict is (
            SynthesisVerdict.PROVIDER_ERROR
        )

    @pytest.mark.asyncio
    async def test_empty_outputs_returns_provider_error(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisCandidate,
            SynthesisVerdict,
        )

        class _EmptyStub:
            async def synthesize_one(self, **_):
                return SynthesisCandidate(
                    code_text="",  # empty
                    error="empty_output",
                )

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(), provider=_EmptyStub(),
        )
        assert result.verdict is (
            SynthesisVerdict.PROVIDER_ERROR
        )


# ---------------------------------------------------------------------------
# § 9 — INSUFFICIENT_CONTEXT short-circuit
# ---------------------------------------------------------------------------


class TestInsufficientContext:
    @pytest.mark.asyncio
    async def test_empty_evidence_short_circuits(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(detection_evidence=()),
            provider=_make_stub(code_text="def x(): pass"),
        )
        assert result.verdict is (
            SynthesisVerdict.INSUFFICIENT_CONTEXT
        )


# ---------------------------------------------------------------------------
# § 10 — SKIPPED_KIND
# ---------------------------------------------------------------------------


class TestSkippedKind:
    @pytest.mark.asyncio
    async def test_disabled_kind_short_circuits(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.primitives import (
            ProposalKind,
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(kind=ProposalKind.DISABLED),
            provider=_make_stub(code_text="def x(): pass"),
        )
        assert result.verdict is (
            SynthesisVerdict.SKIPPED_KIND
        )


# ---------------------------------------------------------------------------
# § 11 — Per-call timeout
# ---------------------------------------------------------------------------


class TestPerCallTimeout:
    @pytest.mark.asyncio
    async def test_timeout_treated_as_failed_candidate(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        # Sub-second timeout for fast test
        monkeypatch.setenv(
            "JARVIS_M10_SYNTHESIS_PER_CALL_TIMEOUT_S", "1.0",
        )
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisVerdict,
        )

        class _SlowStub:
            async def synthesize_one(self, **_):
                await asyncio.sleep(5.0)  # exceeds timeout
                # Never reached
                from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
                    SynthesisCandidate,
                )
                return SynthesisCandidate(
                    code_text="def x(): pass",
                    ast_pin_name="pin",
                )

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(), provider=_SlowStub(),
        )
        # All candidates timed out → PROVIDER_ERROR
        assert result.verdict is (
            SynthesisVerdict.PROVIDER_ERROR
        )


# ---------------------------------------------------------------------------
# § 12 — Risk tier forced to APPROVAL_REQUIRED
# ---------------------------------------------------------------------------


class TestRiskTierForced:
    @pytest.mark.asyncio
    async def test_synthesized_proposal_has_approval_required(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            M10_FORCED_RISK_TIER,
            ProposalSynthesizer, SynthesisVerdict,
        )
        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(),
            provider=_make_stub(
                code_text="def f(): pass", ast_pin="pin",
            ),
        )
        assert result.verdict is SynthesisVerdict.SYNTHESIZED
        assert (
            result.forced_risk_tier
            == M10_FORCED_RISK_TIER
            == "approval_required"
        )

    def test_constant_value_is_approval_required(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            M10_FORCED_RISK_TIER,
        )
        assert M10_FORCED_RISK_TIER == "approval_required"


# ---------------------------------------------------------------------------
# § 13 — Cost accumulation
# ---------------------------------------------------------------------------


class TestCostAccumulation:
    @pytest.mark.asyncio
    async def test_cost_sums_across_candidates(
        self, monkeypatch,
    ):
        _enable(monkeypatch)
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            ProposalSynthesizer, SynthesisCandidate,
        )

        class _CostStub:
            async def synthesize_one(self, **_):
                return SynthesisCandidate(
                    code_text="def f(): pass",
                    ast_pin_name="pin",
                    cost_usd=0.007,
                )

        s = ProposalSynthesizer()
        result = await s.synthesize(
            _make_record(), provider=_CostStub(),
        )
        # K=3 default; 3 × $0.007 = $0.021
        assert result.cost_usd == pytest.approx(0.021)


# ---------------------------------------------------------------------------
# § 14 — Authority floor
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.auto_action_router",
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.graduation_orchestrator",
    )

    def test_synthesizer_module_floor(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "m10" / "proposal_synthesizer.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"proposal_synthesizer.py must NOT import "
                f"{forbidden}"
            )


# ---------------------------------------------------------------------------
# § 15 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.m10 import (
            proposal_synthesizer as ps,
        )
        expected = sorted([
            "M10_FORCED_RISK_TIER",
            "M10_SYNTHESIZER_SCHEMA_VERSION",
            "ProposalSynthesizer",
            "SynthesisCandidate",
            "SynthesisProviderProtocol",
            "SynthesisVerdict",
            "SynthesizedProposal",
            "get_default_synthesizer",
            "m10_synthesis_majority_threshold",
            "m10_synthesis_max_evidence_chars",
            "m10_synthesis_per_call_timeout_s",
            "m10_synthesis_quorum_k",
            "reset_default_synthesizer_for_tests",
        ])
        assert sorted(ps.__all__) == expected

    def test_default_synthesizer_singleton(self):
        from backend.core.ouroboros.governance.m10.proposal_synthesizer import (  # noqa: E501
            get_default_synthesizer,
            reset_default_synthesizer_for_tests,
        )
        reset_default_synthesizer_for_tests()
        a = get_default_synthesizer()
        b = get_default_synthesizer()
        assert a is b
        reset_default_synthesizer_for_tests()
        c = get_default_synthesizer()
        assert c is not a
