"""Gap #2 Slice 5 — Confidence Threshold Tuner graduation regression suite.

Closes the cage end-to-end. Verifies:

  §1   Three master flags graduated default-true (with hot-revert)
  §2   FlagRegistry seeds installed (5 new flags)
  §3   shipped_code_invariants pins (4 new) registered + clean
  §4   yaml_writer sibling entry point + APPLIED SSE wiring
  §5   End-to-end propose → approve → YAML materialize → loader
       reads → confidence_monitor accessor returns adapted value
  §6   End-to-end reject path leaves no YAML residue
  §7   Hot-revert: panel disabled returns 403; loader disabled
       falls through to hardcoded default
  §8   Cage close discipline: substrate predicate parity between
       Slice 1 and Slice 2 validator
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import pytest

aiohttp = pytest.importorskip("aiohttp")
yaml = pytest.importorskip("yaml")
from aiohttp.test_utils import make_mocked_request

from backend.core.ouroboros.governance.adaptation.adapted_confidence_loader import (
    is_loader_enabled,
    load_adapted_thresholds,
)
from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (
    install_surface_validator,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationLedger,
    AdaptationSurface,
    OperatorDecisionStatus,
)
from backend.core.ouroboros.governance.adaptation.yaml_writer import (
    WriteStatus,
    write_confidence_proposal_to_yaml,
)
from backend.core.ouroboros.governance.flag_registry_seed import (
    SEED_SPECS,
)
from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_CONFIDENCE_POLICY_APPLIED,
    EVENT_TYPE_CONFIDENCE_POLICY_APPROVED,
    EVENT_TYPE_CONFIDENCE_POLICY_PROPOSED,
    EVENT_TYPE_CONFIDENCE_POLICY_REJECTED,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.ide_policy_router import (
    IDEPolicyRouter,
    ide_policy_router_enabled,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    list_shipped_code_invariants,
    validate_invariant,
)
from backend.core.ouroboros.governance.verification.confidence_monitor import (
    confidence_approaching_factor,
    confidence_floor,
    confidence_monitor_enforce,
    confidence_window_k,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    ConfidencePolicy,
    confidence_policy_enabled,
)


def _baseline() -> ConfidencePolicy:
    return ConfidencePolicy(
        floor=0.05, window_k=16, approaching_factor=1.5, enforce=False,
    )


def _make_request(
    path: str,
    *,
    method: str = "POST",
    headers=None,
    match_info=None,
    body: bytes = b"",
    remote: str = "127.0.0.1",
):
    headers = headers or {}
    req = make_mocked_request(method, path, headers=headers, payload=None)
    if match_info:
        req.match_info.update(match_info)
    req._transport_peername = (remote, 0)  # type: ignore[attr-defined]

    async def _read():
        return body
    req.read = _read  # type: ignore[assignment]
    return req


def _build_router(tmp_path: Path):
    install_surface_validator()
    ledger = AdaptationLedger(path=tmp_path / "adapt.jsonl")
    broker = StreamEventBroker()
    return IDEPolicyRouter(
        host="127.0.0.1", ledger=ledger, broker=broker,
    ), ledger, broker


# ============================================================================
# §1 — Three master flags graduated default-true
# ============================================================================


class TestMasterFlagGraduation:
    def test_substrate_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", raising=False,
        )
        assert confidence_policy_enabled() is True

    def test_loader_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", raising=False,
        )
        assert is_loader_enabled() is True

    def test_router_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", raising=False,
        )
        assert ide_policy_router_enabled() is True

    def test_substrate_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "false",
        )
        assert confidence_policy_enabled() is False

    def test_loader_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "false",
        )
        assert is_loader_enabled() is False

    def test_router_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "false",
        )
        assert ide_policy_router_enabled() is False


# ============================================================================
# §2 — FlagRegistry seeds installed (5 new flags)
# ============================================================================


class TestFlagRegistrySeeds:
    @pytest.fixture(scope="class")
    def seed_names(self):
        return {s.name for s in SEED_SPECS}

    def test_substrate_master_seeded(self, seed_names):
        assert "JARVIS_CONFIDENCE_POLICY_ENABLED" in seed_names

    def test_loader_master_seeded(self, seed_names):
        assert "JARVIS_CONFIDENCE_LOAD_ADAPTED" in seed_names

    def test_router_master_seeded(self, seed_names):
        assert "JARVIS_IDE_POLICY_ROUTER_ENABLED" in seed_names

    def test_observation_floor_seeded(self, seed_names):
        assert (
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR"
            in seed_names
        )

    def test_router_rate_limit_seeded(self, seed_names):
        assert (
            "JARVIS_IDE_POLICY_ROUTER_RATE_LIMIT_PER_MIN"
            in seed_names
        )

    def test_three_masters_default_true_in_seeds(self):
        masters = {
            "JARVIS_CONFIDENCE_POLICY_ENABLED",
            "JARVIS_CONFIDENCE_LOAD_ADAPTED",
            "JARVIS_IDE_POLICY_ROUTER_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.default is True, (
                    f"{spec.name} graduation requires default=True "
                    f"in FlagRegistry seed (got {spec.default!r})"
                )


# ============================================================================
# §3 — shipped_code_invariants pins (4 new) registered + clean
# ============================================================================


class TestShippedCodeInvariants:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "true",
        )

    def test_four_gap2_pins_registered(self):
        names = {
            inv.invariant_name
            for inv in list_shipped_code_invariants()
        }
        expected = {
            "gap2_confidence_policy_substrate",
            "gap2_confidence_threshold_tightener_surface",
            "gap2_confidence_monitor_loader_bridge",
            "gap2_ide_policy_router_authority",
        }
        missing = expected - names
        assert not missing, (
            f"missing Gap #2 pins: {missing}"
        )

    def test_substrate_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap2_confidence_policy_substrate"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]

    def test_tightener_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap2_confidence_threshold_tightener_surface"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]

    def test_loader_bridge_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap2_confidence_monitor_loader_bridge"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]

    def test_router_authority_pin_clean(self):
        for inv in list_shipped_code_invariants():
            if inv.invariant_name == (
                "gap2_ide_policy_router_authority"
            ):
                violations = validate_invariant(inv)
                assert not violations, [
                    v.detail for v in violations
                ]


# ============================================================================
# §4 — YAML writer sibling entry point + APPLIED SSE wiring
# ============================================================================


def _build_approved_proposal(
    proposal_id: str,
    proposed: ConfidencePolicy,
    current: Optional[ConfidencePolicy] = None,
):
    """Compose an in-memory APPROVED AdaptationProposal for the
    YAML writer."""
    from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (  # noqa: E501
        build_proposed_state_payload,
    )
    from backend.core.ouroboros.governance.adaptation.ledger import (
        AdaptationEvidence,
        AdaptationProposal,
        MonotonicTighteningVerdict,
    )
    if current is None:
        current = _baseline()
    return AdaptationProposal(
        schema_version="2.0",
        proposal_id=proposal_id,
        surface=AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        proposal_kind="raise_floor",
        evidence=AdaptationEvidence(
            window_days=1, observation_count=5,
            summary="floor 0.05 → 0.10",
        ),
        current_state_hash=current.state_hash(),
        proposed_state_hash=proposed.state_hash(),
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="2026-05-02T00:00:00Z",
        proposed_at_epoch=1.0,
        operator_decision=OperatorDecisionStatus.APPROVED,
        operator_decision_at="2026-05-02T00:01:00Z",
        operator_decision_by="alice",
        proposed_state_payload=build_proposed_state_payload(
            current=current, proposed=proposed,
        ),
    )


class TestYamlWriterSibling:
    def test_writer_master_off_skips(self, monkeypatch, tmp_path):
        monkeypatch.delenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
            raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )
        proposal = _build_approved_proposal(
            "p-1",
            ConfidencePolicy(0.10, 16, 1.5, False),
        )
        result = write_confidence_proposal_to_yaml(proposal)
        assert result.status is WriteStatus.SKIPPED_MASTER_OFF

    def test_writer_wrong_surface_returns_unknown(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationEvidence,
            AdaptationProposal,
            MonotonicTighteningVerdict,
        )
        wrong = AdaptationProposal(
            schema_version="2.0",
            proposal_id="p-2",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=1, observation_count=5,
                summary="x → y",
            ),
            current_state_hash="sha256:a",
            proposed_state_hash="sha256:b",
            monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
            proposed_at="2026-05-02T00:00:00Z",
            proposed_at_epoch=1.0,
            operator_decision=OperatorDecisionStatus.APPROVED,
            proposed_state_payload={"x": 1},
        )
        result = write_confidence_proposal_to_yaml(wrong)
        assert result.status is WriteStatus.UNKNOWN_SURFACE

    def test_writer_not_approved_skipped(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )
        proposal = _build_approved_proposal(
            "p-3",
            ConfidencePolicy(0.10, 16, 1.5, False),
        )
        # Override decision to PENDING
        from dataclasses import replace
        pending = replace(
            proposal,
            operator_decision=OperatorDecisionStatus.PENDING,
        )
        result = write_confidence_proposal_to_yaml(pending)
        assert result.status is WriteStatus.SKIPPED_NOT_APPROVED

    def test_writer_materializes_yaml(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "true",
        )
        target = tmp_path / "adapted.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(target),
        )
        proposal = _build_approved_proposal(
            "p-4",
            ConfidencePolicy(0.10, 16, 1.5, False),
        )
        result = write_confidence_proposal_to_yaml(proposal)
        assert result.status is WriteStatus.OK
        assert target.exists()
        loaded = yaml.safe_load(target.read_text())
        assert loaded["thresholds"]["floor"] == 0.10
        assert loaded["proposal_id"] == "p-4"
        assert loaded["approved_by"] == "alice"


# ============================================================================
# §5 — End-to-end cage close (propose → approve → materialize → read)
# ============================================================================


class TestCageCloseEndToEnd:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )
        # Critical: clear any explicit env knob so the loader path
        # is exercised when we read the live confidence_floor()
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_FLOOR", raising=False,
        )

    def _propose_approve(
        self, router, proposed: ConfidencePolicy,
    ) -> str:
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": proposed.to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10 (5 events)",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        assert resp.status == 201, resp.body
        pid = json.loads(resp.body)["proposal_id"]
        # Approve
        approve_req = _make_request(
            f"/policy/confidence/proposals/{pid}/approve",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        resp2 = asyncio.run(router._handle_approve(approve_req))
        assert resp2.status == 200, resp2.body
        return pid

    def test_propose_approve_materializes_yaml(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        # The fixture sets path; router's writer will pick it up
        os.environ["JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH"] = (
            str(target)
        )
        self._propose_approve(
            router, ConfidencePolicy(0.10, 16, 1.5, False),
        )
        assert target.exists()
        loaded = yaml.safe_load(target.read_text())
        assert loaded["thresholds"]["floor"] == 0.10

    def test_loader_picks_up_materialized_yaml(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        os.environ["JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH"] = (
            str(target)
        )
        self._propose_approve(
            router, ConfidencePolicy(0.10, 16, 1.5, False),
        )
        loaded = load_adapted_thresholds()
        assert loaded.floor == 0.10

    def test_confidence_monitor_returns_adapted_value(
        self, tmp_path,
    ):
        """The full close-the-cage-loop: after propose+approve,
        the runtime confidence_floor() returns the adapted value
        (env unset → loader supplies → monitor returns)."""
        router, _, _ = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        os.environ["JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH"] = (
            str(target)
        )
        self._propose_approve(
            router, ConfidencePolicy(0.10, 16, 1.5, False),
        )
        assert confidence_floor() == 0.10

    def test_applied_sse_event_emitted(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        os.environ["JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH"] = (
            str(target)
        )
        self._propose_approve(
            router, ConfidencePolicy(0.10, 16, 1.5, False),
        )
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_CONFIDENCE_POLICY_APPLIED in types

    def test_applied_event_carries_yaml_path(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        os.environ["JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH"] = (
            str(target)
        )
        self._propose_approve(
            router, ConfidencePolicy(0.10, 16, 1.5, False),
        )
        applied = [
            e for e in broker._history
            if e.event_type == EVENT_TYPE_CONFIDENCE_POLICY_APPLIED
        ]
        assert applied
        assert applied[0].payload.get("write_status") == "ok"
        assert str(target) in applied[0].payload.get("yaml_path", "")


# ============================================================================
# §6 — Reject path leaves no YAML residue + emits no APPLIED event
# ============================================================================


class TestRejectPath:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )

    def test_reject_does_not_materialize_yaml(self, tmp_path):
        router, _, _ = _build_router(tmp_path)
        target = tmp_path / "adapted.yaml"
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": ConfidencePolicy(0.10, 16, 1.5, False).to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        pid = json.loads(resp.body)["proposal_id"]
        # Reject
        reject_req = _make_request(
            f"/policy/confidence/proposals/{pid}/reject",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({
                "operator": "alice", "reason": "too aggressive",
            }).encode("utf-8"),
        )
        asyncio.run(router._handle_reject(reject_req))
        # YAML must NOT have been written
        assert not target.exists()

    def test_reject_emits_no_applied_event(self, tmp_path):
        router, _, broker = _build_router(tmp_path)
        body = json.dumps({
            "current": _baseline().to_dict(),
            "proposed": ConfidencePolicy(0.10, 16, 1.5, False).to_dict(),
            "operator": "alice",
            "evidence_summary": "floor 0.05 → 0.10",
            "observation_count": 5,
        }).encode("utf-8")
        req = _make_request(
            "/policy/confidence/proposals",
            method="POST", body=body,
        )
        resp = asyncio.run(router._handle_propose(req))
        pid = json.loads(resp.body)["proposal_id"]
        reject_req = _make_request(
            f"/policy/confidence/proposals/{pid}/reject",
            method="POST",
            match_info={"proposal_id": pid},
            body=json.dumps({"operator": "alice"}).encode("utf-8"),
        )
        asyncio.run(router._handle_reject(reject_req))
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_CONFIDENCE_POLICY_APPLIED not in types
        # But REJECTED event MUST fire
        assert EVENT_TYPE_CONFIDENCE_POLICY_REJECTED in types


# ============================================================================
# §7 — Hot-revert behaviors
# ============================================================================


class TestHotRevert:
    def test_router_hot_revert_returns_403(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_IDE_POLICY_ROUTER_ENABLED", "false",
        )
        router, _, _ = _build_router(tmp_path)
        req = _make_request("/policy/confidence", method="GET")
        resp = asyncio.run(router._handle_snapshot(req))
        assert resp.status == 403

    def test_loader_hot_revert_falls_through_to_default(
        self, monkeypatch, tmp_path,
    ):
        # Materialize a YAML with an adapted floor
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CONFIDENCE_THRESHOLDS_PATH",
            str(tmp_path / "adapted.yaml"),
        )
        target = tmp_path / "adapted.yaml"
        target.write_text(yaml.safe_dump({
            "schema_version": 1,
            "thresholds": {"floor": 0.10},
        }), encoding="utf-8")
        # Hot-revert the loader
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_LOAD_ADAPTED", "false",
        )
        # Env unset → no env override
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_FLOOR", raising=False,
        )
        # Loader returns None → monitor falls through to baseline
        assert confidence_floor() == 0.05


# ============================================================================
# §8 — Substrate predicate parity (Slice 1 ⇔ Slice 2 validator)
# ============================================================================


class TestSubstratePredicateParity:
    """The Slice 2 surface validator runs ``compute_policy_diff``
    (Slice 1) inside its decision tree. Predicate parity means the
    cage cannot diverge from the substrate's tightening direction."""

    def test_validator_uses_compute_policy_diff_directly(self):
        """Source-level pin: the tightener MUST import
        compute_policy_diff (predicate parity guarantee)."""
        src = Path(
            "backend/core/ouroboros/governance/adaptation/"
            "confidence_threshold_tightener.py"
        ).read_text()
        assert "from backend.core.ouroboros.governance.verification.confidence_policy import" in src
        assert "compute_policy_diff" in src

    def test_router_uses_compute_policy_diff_directly(self):
        """Source-level pin: the HTTP router MUST also import
        compute_policy_diff (so its propose-time pre-check matches
        the cage substrate). Two independent call sites, one
        predicate."""
        src = Path(
            "backend/core/ouroboros/governance/ide_policy_router.py"
        ).read_text()
        assert "compute_policy_diff" in src

    def test_three_layers_share_verdict_canonical_strings(self):
        """Slice 1 substrate stamps MonotonicTighteningVerdict on
        every PolicyDiff. Slice 2 validator forwards that verdict
        verbatim. Slice 4 router's response surfaces the same
        canonical string. This pin guarantees cross-surface audit
        queries match by value."""
        from backend.core.ouroboros.governance.adaptation.ledger import (
            MonotonicTighteningVerdict,
        )
        from backend.core.ouroboros.governance.verification.confidence_policy import (
            ConfidencePolicyOutcome,
            compute_policy_diff,
        )
        diff = compute_policy_diff(
            current=_baseline(),
            proposed=ConfidencePolicy(0.10, 16, 1.5, False),
            enabled_override=True,
        )
        assert diff.outcome is ConfidencePolicyOutcome.APPLIED
        assert (
            diff.monotonic_tightening_verdict
            == MonotonicTighteningVerdict.PASSED.value
        )
