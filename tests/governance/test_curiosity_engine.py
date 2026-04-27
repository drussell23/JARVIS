"""CuriosityEngine — autonomous hypothesis-generation primitive pins.

Closes the post-Phase-8 priority #1 from the brutal architectural
review: CuriosityEngine that auto-emits hypotheses from POSTMORTEM
clusters → drops them into HypothesisLedger → optionally triggers
Phase 7.6 probes → optionally routes verdicts via Item #3 bridges.

Pinned cage:
  * 3 master flags (engine + auto-probe + auto-bridge), all default
    false; sub-flags compose hierarchically (auto-probe requires
    engine; auto-bridge requires auto-probe)
  * Per-cycle bounds (MAX_HYPOTHESES_PER_CYCLE=3, MAX_PROBES=3)
  * Cluster threshold (default 3 — matches SelfGoalFormation precedent)
  * Determinism (sort by member_count DESC + sig hash tie-break)
  * Ledger append failure → structured LEDGER_WRITE_FAILED status
  * Probe exception caught + skipped (NEVER raises)
  * Bridge exception caught + skipped
  * Authority + cage invariants
"""
from __future__ import annotations

import ast
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    curiosity_engine as ce,
)
from backend.core.ouroboros.governance.adaptation.curiosity_engine import (
    CuriosityEngine,
    CuriosityResult,
    CuriosityStatus,
    DEFAULT_CLUSTER_THRESHOLD,
    GeneratedHypothesis,
    MAX_CLAIM_CHARS,
    MAX_EXPECTED_OUTCOME_CHARS,
    MAX_HYPOTHESES_PER_CYCLE,
    MAX_PROBES_PER_CYCLE,
    get_cluster_threshold,
    is_auto_bridge_enabled,
    is_auto_probe_enabled,
    is_engine_enabled,
)
from backend.core.ouroboros.governance.hypothesis_ledger import (
    Hypothesis,
    HypothesisLedger,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_PATH = (
    _REPO_ROOT
    / "backend/core/ouroboros/governance/adaptation/curiosity_engine.py"
)


# Stub cluster types matching postmortem_clusterer.ProposalCandidate
# shape (signature.failed_phase, signature.root_cause_class,
# signature.signature_hash(), member_count).


@dataclass(frozen=True)
class _StubSignature:
    failed_phase: str
    root_cause_class: str

    def signature_hash(self) -> str:
        import hashlib
        joined = f"{self.failed_phase}|{self.root_cause_class}"
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class _StubCandidate:
    signature: _StubSignature
    member_count: int


# ---------------------------------------------------------------------------
# Section A — module constants + master flags
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_max_hypotheses_per_cycle_3(self):
        assert MAX_HYPOTHESES_PER_CYCLE == 3

    def test_max_probes_per_cycle_3(self):
        assert MAX_PROBES_PER_CYCLE == 3

    def test_default_cluster_threshold_3(self):
        assert DEFAULT_CLUSTER_THRESHOLD == 3

    def test_max_claim_chars_500(self):
        assert MAX_CLAIM_CHARS == 500

    def test_max_expected_outcome_chars_300(self):
        assert MAX_EXPECTED_OUTCOME_CHARS == 300

    def test_truthy_constant_shape(self):
        assert ce._TRUTHY == ("1", "true", "yes", "on")


class TestMasterFlags:
    def test_engine_default_false(self, monkeypatch):
        monkeypatch.delenv("JARVIS_CURIOSITY_ENGINE_ENABLED", raising=False)
        assert is_engine_enabled() is False

    def test_engine_truthy(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", v)
            assert is_engine_enabled() is True, v

    def test_auto_probe_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", raising=False,
        )
        assert is_auto_probe_enabled() is False

    def test_auto_bridge_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", raising=False,
        )
        assert is_auto_bridge_enabled() is False

    def test_three_flags_independently_controllable(self, monkeypatch):
        # Engine on, auto-probe off, auto-bridge off.
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", raising=False,
        )
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", raising=False,
        )
        assert is_engine_enabled() is True
        assert is_auto_probe_enabled() is False
        assert is_auto_bridge_enabled() is False


class TestClusterThreshold:
    def test_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_CLUSTER_THRESHOLD", raising=False,
        )
        assert get_cluster_threshold() == 3

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_CLUSTER_THRESHOLD", "5")
        assert get_cluster_threshold() == 5

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CURIOSITY_CLUSTER_THRESHOLD", "not-an-int",
        )
        assert get_cluster_threshold() == 3

    def test_zero_falls_back(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_CLUSTER_THRESHOLD", "0")
        assert get_cluster_threshold() == 3


# ---------------------------------------------------------------------------
# Section B — synthesis helpers (pure functions)
# ---------------------------------------------------------------------------


class TestSynthesisHelpers:
    def test_claim_includes_phase_and_cause(self):
        c = ce._synthesize_claim(
            "GENERATE", "anthropic_provider_timeout", 5,
        )
        assert "GENERATE" in c
        assert "anthropic_provider_timeout" in c

    def test_claim_capped_at_max(self):
        big_phase = "P" * 1000
        big_cause = "C" * 1000
        c = ce._synthesize_claim(big_phase, big_cause, 99)
        assert len(c) <= MAX_CLAIM_CHARS

    def test_expected_outcome_includes_phase_and_cause(self):
        e = ce._synthesize_expected_outcome(
            "GENERATE", "timeout",
        )
        assert "GENERATE" in e
        assert "timeout" in e

    def test_expected_outcome_capped_at_max(self):
        big = "X" * 1000
        e = ce._synthesize_expected_outcome(big, big)
        assert len(e) <= MAX_EXPECTED_OUTCOME_CHARS

    def test_op_id_stable_within_second_window(self):
        ts = 1714128000.0
        sig_hash = "abcdef123456"
        op1 = ce._generate_op_id(sig_hash, ts)
        op2 = ce._generate_op_id(sig_hash, ts + 0.5)
        # Same int(ts) → same op_id
        assert op1 == op2

    def test_op_id_differs_across_seconds(self):
        sig_hash = "abcdef"
        op1 = ce._generate_op_id(sig_hash, 1000.0)
        op2 = ce._generate_op_id(sig_hash, 2000.0)
        assert op1 != op2

    def test_op_id_differs_across_signatures(self):
        op1 = ce._generate_op_id("aaaaaaaa", 1000.0)
        op2 = ce._generate_op_id("bbbbbbbb", 1000.0)
        assert op1 != op2

    def test_op_id_format(self):
        op = ce._generate_op_id("abcdef123456", 1714128000.0)
        assert op.startswith("curio-")
        assert "abcdef12" in op  # first 8 chars of sig


# ---------------------------------------------------------------------------
# Section C — run_cycle: master-flag pre-checks
# ---------------------------------------------------------------------------


def _make_clusters(specs: List[Tuple[str, str, int]]) -> List[_StubCandidate]:
    """specs = [(failed_phase, root_cause_class, member_count), ...]"""
    return [
        _StubCandidate(
            signature=_StubSignature(p, rc),
            member_count=mc,
        )
        for (p, rc, mc) in specs
    ]


@pytest.fixture
def fresh_ledger(tmp_path):
    return HypothesisLedger(
        project_root=tmp_path,
        ledger_path=tmp_path / "h.jsonl",
    )


@pytest.fixture
def fresh_engine(fresh_ledger):
    return CuriosityEngine(ledger=fresh_ledger)


class TestRunCyclePreChecks:
    def test_master_off_skips(self, monkeypatch, fresh_engine):
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_ENABLED", raising=False,
        )
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = fresh_engine.run_cycle(clusters)
        assert result.status is CuriosityStatus.SKIPPED_MASTER_OFF

    def test_empty_clusters_skip(self, monkeypatch, fresh_engine):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        result = fresh_engine.run_cycle([])
        assert result.status is CuriosityStatus.SKIPPED_NO_CLUSTERS

    def test_below_threshold_skip(self, monkeypatch, fresh_engine):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        # Threshold default 3; all clusters have member_count=2.
        clusters = _make_clusters([
            ("GENERATE", "x", 2),
            ("GENERATE", "y", 1),
        ])
        result = fresh_engine.run_cycle(clusters)
        assert (
            result.status is CuriosityStatus.SKIPPED_NO_QUALIFYING_CLUSTERS
        )


# ---------------------------------------------------------------------------
# Section D — Generation behavior
# ---------------------------------------------------------------------------


class TestGeneration:
    def test_one_qualifying_cluster_generates_one_hypothesis(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([("GENERATE", "timeout", 5)])
        result = fresh_engine.run_cycle(clusters)
        assert result.is_ok
        assert len(result.hypotheses_generated) == 1
        h = result.hypotheses_generated[0]
        assert "GENERATE" in h.claim
        assert "timeout" in h.claim
        assert h.cluster_signature_hash != ""

    def test_max_hypotheses_per_cycle_enforced(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        # 10 qualifying clusters; cap is 3.
        clusters = _make_clusters([
            ("GENERATE", f"cause{i}", 5)
            for i in range(10)
        ])
        result = fresh_engine.run_cycle(clusters)
        assert len(result.hypotheses_generated) == MAX_HYPOTHESES_PER_CYCLE

    def test_explicit_max_hypotheses_override(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([
            ("GENERATE", f"c{i}", 5) for i in range(10)
        ])
        result = fresh_engine.run_cycle(clusters, max_hypotheses=2)
        assert len(result.hypotheses_generated) == 2

    def test_sort_by_member_count_descending(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        # Mixed member counts; cap at 2; should pick the two LARGEST.
        clusters = _make_clusters([
            ("GENERATE", "small", 3),
            ("GENERATE", "large", 100),
            ("GENERATE", "medium", 50),
        ])
        result = fresh_engine.run_cycle(clusters, max_hypotheses=2)
        # Largest two by member_count: large (100), medium (50).
        causes = [
            h.claim for h in result.hypotheses_generated
        ]
        assert any("large" in c for c in causes)
        assert any("medium" in c for c in causes)
        assert not any("small" in c for c in causes)

    def test_explicit_threshold_override(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([
            ("GENERATE", "low", 2),
            ("GENERATE", "med", 5),
            ("GENERATE", "high", 100),
        ])
        # Threshold=10; only "high" qualifies.
        result = fresh_engine.run_cycle(clusters, threshold=10)
        assert len(result.hypotheses_generated) == 1
        assert "high" in result.hypotheses_generated[0].claim


# ---------------------------------------------------------------------------
# Section E — Ledger persistence
# ---------------------------------------------------------------------------


class TestLedgerPersistence:
    def test_hypothesis_appended_to_ledger(
        self, monkeypatch, fresh_engine, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([("GENERATE", "timeout", 5)])
        fresh_engine.run_cycle(clusters)
        all_h = fresh_ledger.load_all()
        assert len(all_h) == 1
        assert all_h[0].claim != ""
        # is_open() True because actual_outcome is None
        assert all_h[0].is_open()

    def test_proposed_signature_hash_threaded(
        self, monkeypatch, fresh_engine, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = fresh_engine.run_cycle(clusters)
        all_h = fresh_ledger.load_all()
        assert all_h[0].proposed_signature_hash != ""
        # It should match the GeneratedHypothesis's cluster_signature_hash
        gh = result.hypotheses_generated[0]
        assert all_h[0].proposed_signature_hash == gh.cluster_signature_hash

    def test_ledger_append_failure_structured(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        # Inject a ledger whose append always returns False.
        fake_ledger = mock.MagicMock()
        fake_ledger.append.return_value = False
        engine = CuriosityEngine(ledger=fake_ledger)
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = engine.run_cycle(clusters)
        assert result.status is CuriosityStatus.LEDGER_WRITE_FAILED

    def test_ledger_raise_caught(self, monkeypatch):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        fake_ledger = mock.MagicMock()
        fake_ledger.append.side_effect = RuntimeError("boom")
        engine = CuriosityEngine(ledger=fake_ledger)
        clusters = _make_clusters([("GENERATE", "x", 5)])
        # NEVER raises — exception caught, structured status returned.
        result = engine.run_cycle(clusters)
        assert result.status is CuriosityStatus.LEDGER_WRITE_FAILED


# ---------------------------------------------------------------------------
# Section F — Auto-probe sub-flag
# ---------------------------------------------------------------------------


class TestAutoProbe:
    def test_auto_probe_off_no_probe_invoked(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", raising=False,
        )
        fake_probe = mock.MagicMock()
        engine = CuriosityEngine(ledger=fresh_ledger, probe=fake_probe)
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = engine.run_cycle(clusters)
        assert result.is_ok
        assert result.probes_run == 0
        fake_probe.test.assert_not_called()

    def test_auto_probe_on_invokes_probe(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            ProbeResult, ProbeVerdict,
        )
        fake_probe = mock.MagicMock()
        fake_probe.test.return_value = ProbeResult(
            verdict=ProbeVerdict.INCONCLUSIVE_DIMINISHING,
            rounds=2, elapsed_s=0.1,
        )
        engine = CuriosityEngine(ledger=fresh_ledger, probe=fake_probe)
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = engine.run_cycle(clusters)
        assert result.is_ok
        assert result.probes_run == 1
        fake_probe.test.assert_called_once()

    def test_max_probes_per_cycle_capped(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            ProbeResult, ProbeVerdict,
        )
        fake_probe = mock.MagicMock()
        fake_probe.test.return_value = ProbeResult(
            verdict=ProbeVerdict.INCONCLUSIVE_DIMINISHING,
            rounds=2, elapsed_s=0.1,
        )
        engine = CuriosityEngine(ledger=fresh_ledger, probe=fake_probe)
        # 3 hypotheses (max), max_probes=2 → only 2 probes invoked.
        clusters = _make_clusters([
            ("GENERATE", f"c{i}", 5) for i in range(3)
        ])
        result = engine.run_cycle(clusters, max_probes=2)
        assert result.probes_run == 2
        assert fake_probe.test.call_count == 2

    def test_probe_exception_caught(self, monkeypatch, fresh_ledger):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        fake_probe = mock.MagicMock()
        fake_probe.test.side_effect = RuntimeError("simulated probe failure")
        engine = CuriosityEngine(ledger=fresh_ledger, probe=fake_probe)
        clusters = _make_clusters([("GENERATE", "x", 5)])
        # NEVER raises.
        result = engine.run_cycle(clusters)
        assert result.is_ok  # generation succeeded
        assert result.probes_run == 0  # probe failed → not counted


# ---------------------------------------------------------------------------
# Section G — Auto-bridge sub-flag
# ---------------------------------------------------------------------------


class TestAutoBridge:
    def test_auto_bridge_off_no_bridge_invoked(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        monkeypatch.delenv(
            "JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", raising=False,
        )
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            ProbeResult, ProbeVerdict,
        )
        fake_probe = mock.MagicMock()
        fake_probe.test.return_value = ProbeResult(
            verdict=ProbeVerdict.CONFIRMED, rounds=1, elapsed_s=0.1,
            final_evidence="found it",
        )
        fake_bridge = mock.MagicMock()
        engine = CuriosityEngine(
            ledger=fresh_ledger, probe=fake_probe,
            bridge_to_hypothesis_ledger=fake_bridge,
        )
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = engine.run_cycle(clusters)
        assert result.is_ok
        assert result.probes_run == 1
        fake_bridge.assert_not_called()

    def test_auto_bridge_on_invokes_bridge(
        self, monkeypatch, fresh_ledger,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", "1")
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            ProbeResult, ProbeVerdict,
        )
        fake_probe = mock.MagicMock()
        fake_probe.test.return_value = ProbeResult(
            verdict=ProbeVerdict.REFUTED, rounds=1, elapsed_s=0.1,
            final_evidence="not found",
        )
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe_bridge import (
            BridgeResult, BridgeStatus,
        )
        fake_bridge = mock.MagicMock(
            return_value=BridgeResult(status=BridgeStatus.OK),
        )
        engine = CuriosityEngine(
            ledger=fresh_ledger, probe=fake_probe,
            bridge_to_hypothesis_ledger=fake_bridge,
        )
        clusters = _make_clusters([("GENERATE", "x", 5)])
        result = engine.run_cycle(clusters)
        assert result.is_ok
        fake_bridge.assert_called_once()
        assert len(result.bridge_results) == 1

    def test_bridge_exception_caught(self, monkeypatch, fresh_ledger):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_BRIDGE", "1")
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            ProbeResult, ProbeVerdict,
        )
        fake_probe = mock.MagicMock()
        fake_probe.test.return_value = ProbeResult(
            verdict=ProbeVerdict.CONFIRMED, rounds=1, elapsed_s=0.1,
            final_evidence="x",
        )
        fake_bridge = mock.MagicMock(side_effect=RuntimeError("boom"))
        engine = CuriosityEngine(
            ledger=fresh_ledger, probe=fake_probe,
            bridge_to_hypothesis_ledger=fake_bridge,
        )
        clusters = _make_clusters([("GENERATE", "x", 5)])
        # NEVER raises.
        result = engine.run_cycle(clusters)
        assert result.is_ok
        assert result.probes_run == 1


# ---------------------------------------------------------------------------
# Section H — End-to-end (no mocks; real ledger + Null prober)
# ---------------------------------------------------------------------------


class TestEndToEndIntegration:
    def test_e2e_with_null_prober_terminates_inconclusive(
        self, monkeypatch, fresh_ledger,
    ):
        # Real Phase 7.6 runner + Null prober; auto-probe ON.
        # Should terminate INCONCLUSIVE_DIMINISHING (Null returns
        # empty evidence → fingerprint repeats).
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_AUTO_PROBE", "1")
        monkeypatch.setenv("JARVIS_HYPOTHESIS_PROBE_ENABLED", "1")
        from backend.core.ouroboros.governance.adaptation.hypothesis_probe import (
            HypothesisProbe,
            _NullEvidenceProber,
        )
        runner = HypothesisProbe(prober=_NullEvidenceProber())
        engine = CuriosityEngine(ledger=fresh_ledger, probe=runner)
        clusters = _make_clusters([("GENERATE", "timeout", 5)])
        result = engine.run_cycle(clusters)
        assert result.is_ok
        assert result.probes_run == 1
        # The hypothesis is now in the ledger.
        all_h = fresh_ledger.load_all()
        assert len(all_h) == 1


# ---------------------------------------------------------------------------
# Section I — Determinism + idempotency
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_clusters_same_op_id_within_second(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        clusters = _make_clusters([("GENERATE", "x", 5)])
        # Pin now_unix to deterministic value.
        r1 = fresh_engine.run_cycle(clusters, now_unix=1000.0)
        r2 = fresh_engine.run_cycle(clusters, now_unix=1000.0)
        # Same op_id → ledger dedups by hypothesis_id.
        assert (
            r1.hypotheses_generated[0].op_id
            == r2.hypotheses_generated[0].op_id
        )

    def test_alpha_tie_break_for_equal_member_counts(
        self, monkeypatch, fresh_engine,
    ):
        monkeypatch.setenv("JARVIS_CURIOSITY_ENGINE_ENABLED", "1")
        # Two clusters with identical member_count + different
        # signatures. Sort tie-breaks by signature_hash alpha.
        clusters = _make_clusters([
            ("GENERATE", "alpha", 5),
            ("GENERATE", "zulu", 5),
        ])
        # Run twice — same order both times (deterministic sort).
        r1 = fresh_engine.run_cycle(clusters, now_unix=1000.0,
                                    max_hypotheses=1)
        # Reset counter via second engine; same behavior expected.
        import tempfile
        td = Path(tempfile.mkdtemp())
        ledger2 = HypothesisLedger(
            project_root=td, ledger_path=td / "h2.jsonl",
        )
        engine2 = CuriosityEngine(ledger=ledger2)
        r2 = engine2.run_cycle(clusters, now_unix=1000.0,
                               max_hypotheses=1)
        assert (
            r1.hypotheses_generated[0].cluster_signature_hash
            == r2.hypotheses_generated[0].cluster_signature_hash
        )


# ---------------------------------------------------------------------------
# Section J — Authority + cage invariants
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        source = _ENGINE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        banned = (
            "scoped_tool_backend", "general_driver",
            "exploration_engine", "semantic_guardian",
            "orchestrator", "tool_executor", "phase_runners",
            "gate_runner", "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for b in banned:
                    assert b not in node.module, (
                        f"banned import: {node.module}"
                    )

    def test_only_stdlib_and_governance(self):
        # Engine may import hypothesis_ledger + adaptation.* (lazy);
        # nothing else from backend.
        source = _ENGINE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "hashlib", "logging", "os",
            "time", "dataclasses", "typing",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("backend."):
                    # Lazy imports of hypothesis_ledger + adaptation
                    # are allowed (inside function bodies).
                    assert (
                        "hypothesis_ledger" in node.module
                        or "adaptation" in node.module
                    ), f"unexpected backend import: {node.module}"
                else:
                    assert any(
                        node.module.startswith(p) for p in stdlib_prefixes
                    ), f"unexpected import: {node.module}"

    def test_no_subprocess_or_network(self):
        source = _ENGINE_PATH.read_text(encoding="utf-8")
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
            # No direct anthropic — provider injection via Item #3
            "import anthropic",
        ):
            assert token not in source, f"banned token: {token}"
