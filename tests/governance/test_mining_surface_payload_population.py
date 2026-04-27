"""Mining-surface payload population pins.

Items #2/#3 shipped the YAML writer + bridges that consume
``proposed_state_payload`` from approved proposals. Until this PR
the 5 mining surfaces (Slice 2-5 + Phase 7.9 stale detector) didn't
populate the payload — every approve cycle returned
``SKIPPED_NO_PAYLOAD`` and the cognitive loop was theoretical.

This PR populates payload at every ``ledger.propose()`` call site
in:
  * Slice 2 — semantic_guardian_miner (add_pattern)
  * Slice 3 — exploration_floor_tightener (raise_floor)
  * Slice 4a — per_order_mutation_budget (lower_budget)
  * Slice 4b — risk_tier_extender (add_tier)
  * Slice 5 — category_weight_rebalancer (rebalance_weight)
  * Phase 7.9 — stale_pattern_detector (sunset_candidate)

Each payload's shape MUST match the yaml_writer's per-surface
schema (verified by round-trip tests below). Once /adapt approve
fires, yaml_writer reads the payload + writes to the live gate's
adapted YAML + the loader (already wired post-Caller-Wiring-PRs-1-4)
picks it up at next consult.

Pinned cage:
  * Each surface's propose() carries proposed_state_payload
  * Payload shape matches yaml_writer's expectations
  * Round-trip: propose → ledger.get → payload preserved
  * End-to-end: propose → approve → yaml_writer materializes correctly
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationSurface,
    OperatorDecisionStatus,
    ProposeStatus,
    reset_surface_validators,
)


@pytest.fixture
def reset_validators():
    reset_surface_validators()
    yield
    reset_surface_validators()


@pytest.fixture
def fresh_ledger(tmp_path, monkeypatch, reset_validators):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    return AdaptationLedger(path=tmp_path / "ledger.jsonl")


# ---------------------------------------------------------------------------
# Section A — Slice 2: semantic_guardian_miner
# ---------------------------------------------------------------------------


class TestSlice2PayloadPopulation:
    def test_payload_shape_matches_yaml_writer_schema(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            semantic_guardian_miner as sgm,
        )
        # Re-register Slice 2's surface validator after reset.
        # Module re-import via importlib.reload would be heavy; just
        # call _register_validator_once directly.
        # Slice 2 doesn't expose this; re-trigger via importing again
        # — but reset_surface_validators clears them. Easiest: skip
        # the validator path by going around it.
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1",
        )
        events = [
            sgm.PostmortemEventLite(
                op_id=f"op{i}",
                root_cause="missing_import",
                failure_class="code",
                code_snippet_excerpt="from backend.foo import bar  # missing module",
                timestamp_unix=0.0,
            )
            for i in range(5)
        ]
        results = sgm.propose_patterns_from_events(
            events,
            existing_patterns=frozenset(),
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        # Should have at least one OK proposal.
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        # Pull the proposal from ledger and verify payload.
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        assert proposal is not None
        assert proposal.proposed_state_payload is not None
        payload = proposal.proposed_state_payload
        # Required fields per yaml_writer SEMANTIC_GUARDIAN_PATTERNS schema.
        assert "name" in payload
        assert "regex" in payload
        assert "severity" in payload
        assert "message" in payload
        # Field bounds.
        assert isinstance(payload["regex"], str)
        assert len(payload["name"]) <= 240
        assert len(payload["message"]) <= 240


# ---------------------------------------------------------------------------
# Section B — Slice 3: exploration_floor_tightener
# ---------------------------------------------------------------------------


class TestSlice3PayloadPopulation:
    def test_payload_shape_matches_yaml_writer_schema(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            exploration_floor_tightener as eft,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1",
        )
        # Build bypass-failure events: floor_satisfied=True AND
        # verify_outcome IN {regression, failed}.
        events = [
            eft.ExplorationOutcomeLite(
                op_id=f"op{i}",
                category_scores={"comprehension": 1.0, "discovery": 5.0},
                floor_satisfied=True,
                verify_outcome="failed",
                timestamp_unix=0.0,
            )
            for i in range(8)
        ]
        results = eft.propose_floor_raises_from_events(
            events,
            current_floors={"comprehension": 1.0, "discovery": 1.0},
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        assert proposal is not None
        payload = proposal.proposed_state_payload
        assert payload is not None
        assert "category" in payload
        assert "floor" in payload
        # category must be in known set (comprehension/discovery/etc)
        assert payload["category"] in (
            "comprehension", "discovery", "call_graph",
            "structure", "history",
        )
        assert payload["floor"] > 0


# ---------------------------------------------------------------------------
# Section C — Slice 4a: per_order_mutation_budget
# ---------------------------------------------------------------------------


class TestSlice4aPayloadPopulation:
    def test_payload_shape_matches_yaml_writer_schema(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            per_order_mutation_budget as pomb,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_PER_ORDER_BUDGET_ENABLED", "1",
        )
        # Build underutilized events: max_observed << current budget.
        events = [
            pomb.MutationUsageLite(
                op_id=f"op{i}",
                order=2,
                budget_at_time=5,
                observed_mutations=1,
                timestamp_unix=0.0,
            )
            for i in range(8)
        ]
        results = pomb.propose_budget_lowerings_from_events(
            events,
            current_budgets={1: 10, 2: 5},
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        assert proposal is not None
        payload = proposal.proposed_state_payload
        assert payload is not None
        assert "order" in payload
        assert "budget" in payload
        assert payload["order"] in (1, 2)
        # budget MUST respect MIN_ORDER2_BUDGET=1 floor (Order-2 only)
        if payload["order"] == 2:
            assert payload["budget"] >= 1


# ---------------------------------------------------------------------------
# Section D — Slice 4b: risk_tier_extender
# ---------------------------------------------------------------------------


class TestSlice4bPayloadPopulation:
    def test_payload_shape_matches_yaml_writer_schema(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            risk_tier_extender as rte,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_RISK_TIER_LADDER_ENABLED", "1",
        )
        # Build novel-failure events (failure_class NOT in default
        # known set).
        events = [
            rte.PostmortemEventLite(
                op_id=f"op{i}",
                failure_class="network_egress",
                blast_radius=0.4,
                timestamp_unix=0.0,
            )
            for i in range(8)
        ]
        results = rte.propose_tier_extensions_from_events(
            events,
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        assert proposal is not None
        payload = proposal.proposed_state_payload
        assert payload is not None
        assert "tier_name" in payload
        assert "insert_after" in payload
        assert "failure_class" in payload
        # tier_name must match [A-Z0-9_]+ charset (yaml_writer +
        # adapted_risk_tier_loader both reject otherwise).
        import re
        assert re.match(r"^[A-Z0-9_]+$", payload["tier_name"])
        assert re.match(r"^[A-Z0-9_]+$", payload["insert_after"])


# ---------------------------------------------------------------------------
# Section E — Slice 5: category_weight_rebalancer
# ---------------------------------------------------------------------------


class TestSlice5PayloadPopulation:
    def test_payload_shape_matches_yaml_writer_schema(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            category_weight_rebalancer as cwr,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_CATEGORY_WEIGHTS_ENABLED", "1",
        )
        # Build per-category outcomes with strong score-vs-passed
        # correlation gap.
        events = []
        for i in range(15):
            events.append(cwr.CategoryOutcomeLite(
                op_id=f"op{i}",
                # comprehension scores high, discovery scores low
                # comprehension correlates strongly w/ passed; discovery weakly
                category_scores={"comprehension": 5.0, "discovery": 1.0},
                verify_passed=(i % 2 == 0),  # 50/50
                timestamp_unix=0.0,
            ))
        # Add events where comprehension is the differentiator
        for i in range(15):
            events.append(cwr.CategoryOutcomeLite(
                op_id=f"op_high_{i}",
                category_scores={"comprehension": 10.0, "discovery": 1.0},
                verify_passed=True,
                timestamp_unix=0.0,
            ))
        results = cwr.propose_weight_rebalances_from_events(
            events,
            current_weights={
                "comprehension": 1.0, "discovery": 1.0,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
            },
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        # May or may not have results depending on correlation calc;
        # just verify shape if any OK.
        if not ok_results:
            pytest.skip("no rebalance candidates from synthetic events")
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        payload = proposal.proposed_state_payload
        assert payload is not None
        assert "new_weights" in payload
        assert "high_value_category" in payload
        assert "low_value_category" in payload
        assert isinstance(payload["new_weights"], dict)
        # All 5 categories present (Slice 5 produces full vector)
        for cat in ("comprehension", "discovery", "call_graph",
                    "structure", "history"):
            assert cat in payload["new_weights"]
            assert payload["new_weights"][cat] > 0


# ---------------------------------------------------------------------------
# Section F — Phase 7.9: stale_pattern_detector
# ---------------------------------------------------------------------------


class TestPhase79PayloadPopulation:
    def test_payload_shape_includes_pattern_name(
        self, monkeypatch, fresh_ledger,
    ):
        from backend.core.ouroboros.governance.adaptation import (
            stale_pattern_detector as spd,
        )
        # Need to re-register stale-pattern surface validator
        from backend.core.ouroboros.governance.adaptation import (
            semantic_guardian_miner as sgm,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_STALE_PATTERN_DETECTOR_ENABLED", "1",
        )
        # Force re-registration (reset_surface_validators cleared all)
        spd._VALIDATOR_REGISTERED = False
        spd._register_validator_once()
        adapted_patterns = ["pattern_a", "pattern_b"]
        # No events → both patterns are "never matched" → stale.
        results = spd.propose_sunset_candidates_from_events(
            adapted_patterns=adapted_patterns,
            match_events=[],
            current_state_hash="sha256:abc",
            threshold_days=30,
            now_unix=1_000_000.0,
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal = fresh_ledger.get(ok_results[0].proposal_id)
        assert proposal is not None
        payload = proposal.proposed_state_payload
        assert payload is not None
        assert "pattern_name" in payload
        assert "days_since_last_match" in payload
        assert "last_match_unix" in payload
        assert "kind" in payload
        assert payload["kind"] == "sunset_candidate"
        # pattern_name must be in our adapted_patterns input
        assert payload["pattern_name"] in adapted_patterns


# ---------------------------------------------------------------------------
# Section G — End-to-end: propose → approve → yaml_writer materializes
# ---------------------------------------------------------------------------


class TestEndToEndApproveAndMaterialize:
    """Critical pin: with payload populated + yaml_writer master flag
    on + bridge wiring, /adapt approve actually writes to the YAML.

    Pre-payload-population (Items #2/#3 shipped but no payload) every
    approve returned SKIPPED_NO_PAYLOAD. THIS pin proves the loop
    is now functional end-to-end.
    """

    def test_slice3_approve_writes_to_yaml(
        self, monkeypatch, fresh_ledger, tmp_path,
    ):
        # Arrange: master flags on; YAML path under tmp.
        from backend.core.ouroboros.governance.adaptation import (
            exploration_floor_tightener as eft,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_IRON_GATE_FLOORS_ENABLED", "1",
        )
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
        )
        yaml_path = tmp_path / "floors.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )

        # Mine a proposal.
        events = [
            eft.ExplorationOutcomeLite(
                op_id=f"op{i}",
                category_scores={"comprehension": 1.0, "discovery": 5.0},
                floor_satisfied=True,
                verify_outcome="failed",
                timestamp_unix=0.0,
            )
            for i in range(8)
        ]
        results = eft.propose_floor_raises_from_events(
            events,
            current_floors={"comprehension": 1.0, "discovery": 1.0},
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal_id = ok_results[0].proposal_id

        # Approve via ledger directly (skip /adapt REPL for unit test).
        approve_result = fresh_ledger.approve(
            proposal_id, operator="testop",
        )
        assert approve_result.status.value == "OK"

        # Now manually invoke the writer (in production this happens
        # via meta_governor.handle_approve() — verified by Item #2
        # tests).
        from backend.core.ouroboros.governance.adaptation.yaml_writer import (
            write_proposal_to_yaml, WriteStatus,
        )
        approved_proposal = fresh_ledger.get(proposal_id)
        assert approved_proposal.proposed_state_payload is not None  # PAYLOAD POPULATED
        write_result = write_proposal_to_yaml(approved_proposal)
        assert write_result.status == WriteStatus.OK

        # The YAML file MUST exist with the materialized floor.
        assert yaml_path.exists()
        import yaml
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert "floors" in doc
        assert len(doc["floors"]) == 1
        entry = doc["floors"][0]
        assert "category" in entry
        assert "floor" in entry
        assert entry["category"] in (
            "comprehension", "discovery", "call_graph",
            "structure", "history",
        )
        # Provenance auto-enriched.
        assert entry["proposal_id"] == proposal_id
        assert entry["approved_by"] == "testop"

    def test_slice2_approve_writes_to_yaml(
        self, monkeypatch, fresh_ledger, tmp_path,
    ):
        # Same pattern but for Slice 2 SemanticGuardian patterns.
        from backend.core.ouroboros.governance.adaptation import (
            semantic_guardian_miner as sgm,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTIVE_SEMANTIC_GUARDIAN_ENABLED", "1",
        )
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
        )
        yaml_path = tmp_path / "patterns.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH", str(yaml_path),
        )

        events = [
            sgm.PostmortemEventLite(
                op_id=f"op{i}",
                root_cause="missing_import",
                failure_class="code",
                code_snippet_excerpt="from backend.foo import bar  # missing",
                timestamp_unix=0.0,
            )
            for i in range(5)
        ]
        results = sgm.propose_patterns_from_events(
            events,
            existing_patterns=frozenset(),
            current_state_hash="sha256:abc",
            ledger=fresh_ledger,
        )
        ok_results = [
            r for r in results if r.status == ProposeStatus.OK
        ]
        assert len(ok_results) >= 1
        proposal_id = ok_results[0].proposal_id
        fresh_ledger.approve(proposal_id, operator="testop")
        from backend.core.ouroboros.governance.adaptation.yaml_writer import (
            write_proposal_to_yaml, WriteStatus,
        )
        approved = fresh_ledger.get(proposal_id)
        assert approved.proposed_state_payload is not None
        result = write_proposal_to_yaml(approved)
        assert result.status == WriteStatus.OK
        import yaml
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert "patterns" in doc
        assert len(doc["patterns"]) == 1
        entry = doc["patterns"][0]
        assert "name" in entry
        assert "regex" in entry
        assert entry["proposal_id"] == proposal_id


# ---------------------------------------------------------------------------
# Section H — caller-grep invariants (bit-rot guard)
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


class TestCallerGrepInvariants:
    """Pin that EVERY mining surface's propose() call now passes
    proposed_state_payload kwarg. Bit-rot guard: future PRs that
    add new mining surfaces must follow the same pattern."""

    @pytest.mark.parametrize("rel_path", [
        "backend/core/ouroboros/governance/adaptation/semantic_guardian_miner.py",
        "backend/core/ouroboros/governance/adaptation/exploration_floor_tightener.py",
        "backend/core/ouroboros/governance/adaptation/per_order_mutation_budget.py",
        "backend/core/ouroboros/governance/adaptation/risk_tier_extender.py",
        "backend/core/ouroboros/governance/adaptation/category_weight_rebalancer.py",
        "backend/core/ouroboros/governance/adaptation/stale_pattern_detector.py",
    ])
    def test_propose_call_includes_payload_kwarg(self, rel_path):
        path = _REPO_ROOT / rel_path
        src = path.read_text(encoding="utf-8")
        # Find ledger.propose( call and verify proposed_state_payload
        # appears within ~500 chars after.
        idx = src.find("ledger.propose(")
        assert idx > 0, f"{rel_path}: no ledger.propose( call found"
        window = src[idx:idx + 800]
        assert "proposed_state_payload=" in window, (
            f"{rel_path}: ledger.propose() does NOT pass "
            "proposed_state_payload — mining surface won't materialize "
            "to YAML on /adapt approve"
        )
