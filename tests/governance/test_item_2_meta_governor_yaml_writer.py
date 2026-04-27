"""Item #2 — MetaGovernor YAML writer pins.

Closes the producer-side gap: /adapt approve now materializes the
proposal's `proposed_state_payload` into the live gate's adapted
YAML, end-to-end with the consumer wiring (5/5 wired in PRs
#23414+#23452+#23493+#23525 + Phase 7.1 in #22992).

Pinned cage:
  * Schema extension backward-compat: pre-Item-#2 rows (without
    `proposed_state_payload`) load as None; new rows serialize+
    deserialize the payload round-trip.
  * Per-surface materializers: 5 surfaces × correct YAML path +
    top-level key + provenance enrichment.
  * Atomic-rename writer with cross-process flock.
  * Master-off byte-identical: writer no-op when
    JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED=false; ledger approve
    still works.
  * Skip paths: not-approved / no-payload / unknown-surface.
  * Authority + cage invariants.
  * meta_governor wiring: approve calls writer; writer failures
    don't roll back the ledger approval.
"""
from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from backend.core.ouroboros.governance.adaptation import (
    yaml_writer as yw,
)
from backend.core.ouroboros.governance.adaptation.ledger import (
    ADAPTATION_SCHEMA_VERSION,
    ADAPTATION_SCHEMA_VERSIONS_READABLE,
    AdaptationEvidence,
    AdaptationLedger,
    AdaptationProposal,
    AdaptationSurface,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    ProposeStatus,
    reset_surface_validators,
)
from backend.core.ouroboros.governance.adaptation.yaml_writer import (
    MAX_EXISTING_YAML_BYTES,
    WriteResult,
    WriteStatus,
    is_writer_enabled,
    write_proposal_to_yaml,
)


_REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Section A — Schema extension backward-compat
# ---------------------------------------------------------------------------


class TestSchemaExtension:
    def test_schema_version_bumped_to_2_0(self):
        assert ADAPTATION_SCHEMA_VERSION == "2.0"

    def test_readable_versions_include_1_0_and_2_0(self):
        assert ADAPTATION_SCHEMA_VERSIONS_READABLE == ("1.0", "2.0")

    def test_proposal_payload_field_default_None(self):
        p = AdaptationProposal(
            schema_version="2.0",
            proposal_id="adapt-test",
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=3,
                source_event_ids=("e1",), summary="test summary",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-04-26T00:00:00Z",
            proposed_at_epoch=1.0,
        )
        assert p.proposed_state_payload is None

    def test_to_dict_omits_payload_when_None(self):
        p = AdaptationProposal(
            schema_version="2.0",
            proposal_id="adapt-test",
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=3,
                source_event_ids=("e1",), summary="test",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-04-26T00:00:00Z",
            proposed_at_epoch=1.0,
        )
        d = p.to_dict()
        assert "proposed_state_payload" not in d

    def test_to_dict_includes_payload_when_populated(self):
        payload = {"name": "test_pattern", "regex": "X", "severity": "warn"}
        p = AdaptationProposal(
            schema_version="2.0",
            proposal_id="adapt-test",
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=3,
                source_event_ids=("e1",), summary="test",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-04-26T00:00:00Z",
            proposed_at_epoch=1.0,
            proposed_state_payload=payload,
        )
        d = p.to_dict()
        assert d["proposed_state_payload"] == payload

    def test_round_trip_preserves_payload(self):
        payload = {"floor": 2.0, "category": "comprehension"}
        p = AdaptationProposal(
            schema_version="2.0",
            proposal_id="adapt-test",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=5,
                source_event_ids=("e1",), summary="test",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            monotonic_tightening_verdict=(
                MonotonicTighteningVerdict.PASSED
            ),
            proposed_at="2026-04-26T00:00:00Z",
            proposed_at_epoch=1.0,
            proposed_state_payload=payload,
        )
        as_dict = p.to_dict()
        recovered = AdaptationProposal.from_dict(as_dict)
        assert recovered.proposed_state_payload == payload

    def test_from_dict_pre_extension_row_payload_None(self):
        # Simulate a pre-Item-#2 ledger row (no payload field).
        old_row = {
            "schema_version": "1.0",
            "proposal_id": "adapt-old",
            "surface": "semantic_guardian.patterns",
            "proposal_kind": "add_pattern",
            "evidence": {
                "window_days": 7, "observation_count": 3,
                "source_event_ids": [], "summary": "old",
            },
            "current_state_hash": "sha256:abc",
            "proposed_state_hash": "sha256:def",
            "monotonic_tightening_verdict": "passed",
            "proposed_at": "2026-04-25T00:00:00Z",
            "proposed_at_epoch": 1.0,
            "operator_decision": "pending",
        }
        p = AdaptationProposal.from_dict(old_row)
        assert p.proposed_state_payload is None
        assert p.schema_version == "1.0"

    def test_from_dict_garbage_payload_loaded_as_None(self):
        # Defensive: payload field present but not a Mapping →
        # safely defaults to None.
        bad_row = {
            "schema_version": "2.0",
            "proposal_id": "adapt-bad",
            "surface": "semantic_guardian.patterns",
            "proposal_kind": "add_pattern",
            "evidence": {
                "window_days": 7, "observation_count": 3,
                "source_event_ids": [], "summary": "bad",
            },
            "current_state_hash": "sha256:abc",
            "proposed_state_hash": "sha256:def",
            "monotonic_tightening_verdict": "passed",
            "proposed_at": "2026-04-26T00:00:00Z",
            "proposed_at_epoch": 1.0,
            "operator_decision": "pending",
            "proposed_state_payload": "not a mapping",
        }
        p = AdaptationProposal.from_dict(bad_row)
        assert p.proposed_state_payload is None


# ---------------------------------------------------------------------------
# Section B — propose() accepts payload kwarg
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_validators():
    reset_surface_validators()
    yield
    reset_surface_validators()


@pytest.fixture
def fresh_ledger(tmp_path, monkeypatch, reset_validators):
    monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
    return AdaptationLedger(path=tmp_path / "ledger.jsonl")


class TestProposeWithPayload:
    def test_propose_without_payload_works(self, fresh_ledger):
        # Pre-Item-#2 caller pattern (kwarg omitted).
        result = fresh_ledger.propose(
            proposal_id="adapt-test-no-payload",
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            proposal_kind="add_pattern",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=3,
                source_event_ids=("e1",), summary="test",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
        )
        assert result.status == ProposeStatus.OK
        loaded = fresh_ledger.get(result.proposal_id)
        assert loaded is not None
        assert loaded.proposed_state_payload is None

    def test_propose_with_payload_stored(self, fresh_ledger):
        payload = {"category": "comprehension", "floor": 2.0}
        result = fresh_ledger.propose(
            proposal_id="adapt-test-with-payload",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=5,
                source_event_ids=("e1",), summary="floor → up",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload=payload,
        )
        assert result.status == ProposeStatus.OK
        loaded = fresh_ledger.get(result.proposal_id)
        assert loaded is not None
        assert loaded.proposed_state_payload == payload

    def test_propose_payload_garbage_rejected(self, fresh_ledger):
        result = fresh_ledger.propose(
            proposal_id="adapt-test-bad-payload",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=5,
                source_event_ids=("e1",), summary="garbage",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload="not a mapping",  # type: ignore[arg-type]
        )
        assert result.status == ProposeStatus.INVALID_PROPOSAL
        assert "payload_must_be_mapping" in result.detail

    def test_payload_survives_approve_state_transition(
        self, fresh_ledger,
    ):
        payload = {"order": 2, "budget": 1}
        propose_result = fresh_ledger.propose(
            proposal_id="adapt-budget-1",
            surface=AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
            proposal_kind="lower_budget",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=10,
                source_event_ids=("e1",),
                summary="budget → down to 1",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload=payload,
        )
        assert propose_result.status == ProposeStatus.OK
        approve_result = fresh_ledger.approve(
            "adapt-budget-1", operator="op",
        )
        assert approve_result.status.value == "OK"
        # After approve, the latest record must STILL carry the payload.
        loaded = fresh_ledger.get("adapt-budget-1")
        assert loaded is not None
        assert loaded.operator_decision is OperatorDecisionStatus.APPROVED
        assert loaded.proposed_state_payload == payload


# ---------------------------------------------------------------------------
# Section C — yaml_writer module constants + master flag
# ---------------------------------------------------------------------------


class TestWriterMasterFlag:
    def test_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
            raising=False,
        )
        assert is_writer_enabled() is False

    def test_truthy_variants(self, monkeypatch):
        for v in ("1", "true", "TRUE", "Yes", "ON"):
            monkeypatch.setenv(
                "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", v,
            )
            assert is_writer_enabled() is True, v

    def test_falsy_variants(self, monkeypatch):
        for v in ("0", "false", "no", "off", "", " "):
            monkeypatch.setenv(
                "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", v,
            )
            assert is_writer_enabled() is False, v

    def test_max_existing_yaml_bytes_is_4MiB(self):
        assert MAX_EXISTING_YAML_BYTES == 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# Section D — write_proposal_to_yaml: pre-checks (skip paths)
# ---------------------------------------------------------------------------


def _make_approved_proposal(
    surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
    payload=None,
):
    if payload is None:
        payload = {"category": "comprehension", "floor": 2.0}
    return AdaptationProposal(
        schema_version="2.0",
        proposal_id="adapt-test-1",
        surface=surface,
        proposal_kind="raise_floor",
        evidence=AdaptationEvidence(
            window_days=7, observation_count=5,
            source_event_ids=("e1",), summary="test → up",
        ),
        current_state_hash="sha256:abc",
        proposed_state_hash="sha256:def",
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="2026-04-26T00:00:00Z",
        proposed_at_epoch=1.0,
        operator_decision=OperatorDecisionStatus.APPROVED,
        operator_decision_at="2026-04-26T01:00:00Z",
        operator_decision_by="alice",
        applied_at="2026-04-26T01:00:00Z",
        proposed_state_payload=payload,
    )


class TestWriterSkipPaths:
    def test_master_off_skips(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED",
            raising=False,
        )
        result = write_proposal_to_yaml(_make_approved_proposal())
        assert result.status == WriteStatus.SKIPPED_MASTER_OFF
        assert result.is_skipped

    def test_pending_proposal_skips(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
        )
        p = _make_approved_proposal()
        # Replace with PENDING status.
        from dataclasses import replace
        pending = replace(
            p, operator_decision=OperatorDecisionStatus.PENDING,
        )
        result = write_proposal_to_yaml(pending)
        assert result.status == WriteStatus.SKIPPED_NOT_APPROVED

    def test_rejected_proposal_skips(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
        )
        from dataclasses import replace
        p = replace(
            _make_approved_proposal(),
            operator_decision=OperatorDecisionStatus.REJECTED,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.SKIPPED_NOT_APPROVED

    def test_no_payload_skips(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
        )
        from dataclasses import replace
        p = replace(
            _make_approved_proposal(),
            proposed_state_payload=None,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.SKIPPED_NO_PAYLOAD


# ---------------------------------------------------------------------------
# Section E — Per-surface materialization (5 surfaces)
# ---------------------------------------------------------------------------


def _enable_writer(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_META_GOVERNOR_YAML_WRITER_ENABLED", "1",
    )


def _read_yaml(path):
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestPerSurfaceMaterialization:
    def test_semantic_guardian_patterns_writes_correctly(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "patterns.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_GUARDIAN_PATTERNS_PATH", str(yaml_path),
        )
        payload = {
            "name": "stale_token_in_log", "regex": "TODO\\(removed\\)",
            "severity": "warn", "message": "stale token",
        }
        p = _make_approved_proposal(
            surface=AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        assert "patterns" in doc
        assert len(doc["patterns"]) == 1
        entry = doc["patterns"][0]
        assert entry["name"] == "stale_token_in_log"
        # Provenance enriched.
        assert entry["proposal_id"] == "adapt-test-1"
        assert entry["approved_at"] == "2026-04-26T01:00:00Z"
        assert entry["approved_by"] == "alice"

    def test_iron_gate_floors_writes_correctly(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        payload = {"category": "discovery", "floor": 1.5}
        p = _make_approved_proposal(
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        assert doc["floors"][0]["category"] == "discovery"
        assert doc["floors"][0]["floor"] == 1.5

    def test_mutation_budgets_writes_correctly(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "budgets.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_MUTATION_BUDGETS_PATH", str(yaml_path),
        )
        payload = {"order": 2, "budget": 1}
        p = _make_approved_proposal(
            surface=AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        assert doc["budgets"][0]["order"] == 2
        assert doc["budgets"][0]["budget"] == 1

    def test_risk_tier_ladder_writes_correctly(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "tiers.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_RISK_TIERS_PATH", str(yaml_path),
        )
        payload = {
            "tier_name": "NOTIFY_APPLY_HARDENED_NETWORK",
            "insert_after": "NOTIFY_APPLY",
            "failure_class": "network_egress",
        }
        p = _make_approved_proposal(
            surface=AdaptationSurface.RISK_TIER_FLOOR_TIERS,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        assert (
            doc["tiers"][0]["tier_name"]
            == "NOTIFY_APPLY_HARDENED_NETWORK"
        )

    def test_category_weights_writes_correctly(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "weights.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_CATEGORY_WEIGHTS_PATH", str(yaml_path),
        )
        payload = {
            "high_value_category": "comprehension",
            "low_value_category": "discovery",
            "new_weights": {
                "comprehension": 1.20, "discovery": 0.90,
                "call_graph": 1.0, "structure": 1.0, "history": 1.0,
            },
        }
        p = _make_approved_proposal(
            surface=AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        assert "rebalances" in doc
        assert (
            doc["rebalances"][0]["new_weights"]["comprehension"]
            == 1.20
        )


# ---------------------------------------------------------------------------
# Section F — Append semantics (latest-wins via loader)
# ---------------------------------------------------------------------------


class TestAppendSemantics:
    def test_second_write_appends_not_replaces(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        from dataclasses import replace
        p1 = _make_approved_proposal(
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            payload={"category": "comprehension", "floor": 2.0},
        )
        p2 = replace(
            p1, proposal_id="adapt-test-2",
            proposed_state_payload={
                "category": "discovery", "floor": 1.5,
            },
        )
        write_proposal_to_yaml(p1)
        write_proposal_to_yaml(p2)
        doc = _read_yaml(yaml_path)
        assert len(doc["floors"]) == 2
        assert doc["floors"][0]["category"] == "comprehension"
        assert doc["floors"][1]["category"] == "discovery"


# ---------------------------------------------------------------------------
# Section G — Existing-file edge cases
# ---------------------------------------------------------------------------


class TestExistingFileEdgeCases:
    def test_oversize_existing_file_refuses(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        yaml_path.write_text("x", encoding="utf-8")
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        with mock.patch.object(
            Path, "stat",
            return_value=mock.Mock(
                st_size=MAX_EXISTING_YAML_BYTES + 1,
            ),
        ):
            result = write_proposal_to_yaml(_make_approved_proposal())
        assert result.status == WriteStatus.EXISTING_OVERSIZE

    def test_corrupted_yaml_returns_parse_error(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        yaml_path.write_text(
            "floors: [oh no\n  - missing close", encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        result = write_proposal_to_yaml(_make_approved_proposal())
        assert result.status == WriteStatus.EXISTING_PARSE_ERROR

    def test_non_mapping_existing_returns_error(
        self, monkeypatch, tmp_path,
    ):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        yaml_path.write_text(
            "- a\n- list\n- not\n- a\n- mapping\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        result = write_proposal_to_yaml(_make_approved_proposal())
        assert result.status == WriteStatus.EXISTING_NON_MAPPING

    def test_no_pyyaml_returns_error(self, monkeypatch, tmp_path):
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        yaml_path.write_text("floors: []\n", encoding="utf-8")
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        # Force ImportError on `import yaml`.
        sentinel = object()
        original_yaml = sys.modules.pop("yaml", sentinel)
        try:
            import builtins
            real_import = builtins.__import__

            def fake_import(name, *a, **k):
                if name == "yaml":
                    raise ImportError("forced for test")
                return real_import(name, *a, **k)

            with mock.patch.object(builtins, "__import__", side_effect=fake_import):
                result = write_proposal_to_yaml(
                    _make_approved_proposal(),
                )
                assert result.status == WriteStatus.NO_PYYAML
        finally:
            if original_yaml is not sentinel:
                sys.modules["yaml"] = original_yaml  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Section H — Provenance enrichment edge cases
# ---------------------------------------------------------------------------


class TestProvenanceEnrichment:
    def test_payload_provenance_takes_precedence(
        self, monkeypatch, tmp_path,
    ):
        # If miner already populates approved_by in payload, writer
        # should not overwrite.
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        payload = {
            "category": "comprehension", "floor": 2.0,
            "approved_by": "miner_specified",  # pre-set
        }
        p = _make_approved_proposal(
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            payload=payload,
        )
        result = write_proposal_to_yaml(p)
        assert result.status == WriteStatus.OK
        doc = _read_yaml(yaml_path)
        # Payload value preserved (not overwritten by enrichment).
        assert doc["floors"][0]["approved_by"] == "miner_specified"


# ---------------------------------------------------------------------------
# Section I — meta_governor wiring
# ---------------------------------------------------------------------------


class TestMetaGovernorWiring:
    def test_approve_calls_writer(self, monkeypatch, tmp_path):
        from backend.core.ouroboros.governance.adaptation import (
            meta_governor as mg,
        )
        from backend.core.ouroboros.governance.adaptation import (
            yaml_writer as ywmod,
        )
        # Setup ledger with a proposal that has payload.
        monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_ADAPT_REPL_ENABLED", "1")
        _enable_writer(monkeypatch)
        yaml_path = tmp_path / "floors.yaml"
        monkeypatch.setenv(
            "JARVIS_ADAPTED_IRON_GATE_FLOORS_PATH", str(yaml_path),
        )
        ledger = AdaptationLedger(path=tmp_path / "ledger.jsonl")
        propose_result = ledger.propose(
            proposal_id="adapt-wiring-test",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=5,
                source_event_ids=("e1",), summary="floor → up",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload={
                "category": "comprehension", "floor": 2.0,
            },
        )
        assert propose_result.status == ProposeStatus.OK
        # Now invoke /adapt approve via dispatch.
        dispatch = mg.dispatch_adapt(
            ["approve", "adapt-wiring-test", "--reason", "test"],
            ledger=ledger,
            operator="testop",
            reader=lambda prompt: "test reason",
        )
        assert dispatch.status.value == "OK"
        # YAML file MUST exist with the materialized payload.
        assert yaml_path.exists()
        doc = _read_yaml(yaml_path)
        assert doc["floors"][0]["category"] == "comprehension"
        # Output should include yaml_write_status=ok.
        assert "yaml_write_status=ok" in (dispatch.output or "")

    def test_writer_failure_does_not_roll_back_approval(
        self, monkeypatch, tmp_path,
    ):
        # Critical invariant: if YAML write fails, ledger approval
        # must still be persisted.
        from backend.core.ouroboros.governance.adaptation import (
            meta_governor as mg,
        )
        from backend.core.ouroboros.governance.adaptation import (
            yaml_writer as ywmod,
        )
        monkeypatch.setenv("JARVIS_ADAPTATION_LEDGER_ENABLED", "1")
        monkeypatch.setenv("JARVIS_ADAPT_REPL_ENABLED", "1")
        _enable_writer(monkeypatch)
        ledger = AdaptationLedger(path=tmp_path / "ledger.jsonl")
        propose_result = ledger.propose(
            proposal_id="adapt-no-rollback",
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
            proposal_kind="raise_floor",
            evidence=AdaptationEvidence(
                window_days=7, observation_count=5,
                source_event_ids=("e1",), summary="will fail",
            ),
            current_state_hash="sha256:abc",
            proposed_state_hash="sha256:def",
            proposed_state_payload={
                "category": "comprehension", "floor": 2.0,
            },
        )
        # Patch writer to raise.
        with mock.patch.object(
            ywmod, "write_proposal_to_yaml",
            side_effect=RuntimeError("simulated write failure"),
        ):
            dispatch = mg.dispatch_adapt(
                ["approve", "adapt-no-rollback", "--reason", "test"],
                ledger=ledger,
                operator="op",
                reader=lambda prompt: "test reason",
            )
        # Dispatch reports OK (ledger approval succeeded).
        assert dispatch.status.value == "OK"
        # Ledger has the APPROVED record.
        loaded = ledger.get("adapt-no-rollback")
        assert loaded.operator_decision is OperatorDecisionStatus.APPROVED


# ---------------------------------------------------------------------------
# Section J — Authority + cage invariants
# ---------------------------------------------------------------------------


_WRITER_PATH = Path(yw.__file__)


class TestAuthorityInvariants:
    def test_no_banned_governance_imports(self):
        source = _WRITER_PATH.read_text()
        tree = ast.parse(source)
        banned_substrings = (
            "scoped_tool_backend",
            "general_driver",
            "exploration_engine",
            "semantic_guardian",
            "orchestrator",
            "tool_executor",
            "phase_runners",
            "gate_runner",
            "risk_tier_floor",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for banned in banned_substrings:
                    assert banned not in node.module, (
                        f"banned import: {node.module}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for banned in banned_substrings:
                        assert banned not in alias.name, (
                            f"banned import: {alias.name}"
                        )

    def test_only_stdlib_and_adaptation(self):
        source = _WRITER_PATH.read_text()
        tree = ast.parse(source)
        stdlib_prefixes = (
            "__future__", "enum", "logging", "os", "tempfile",
            "dataclasses", "pathlib", "typing",
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
                    ) or node.module == "yaml", (
                        f"unexpected import: {node.module}"
                    )

    def test_no_subprocess_or_network_tokens(self):
        source = _WRITER_PATH.read_text()
        for token in (
            "subprocess", "requests", "urllib", "socket",
            "http.client", "asyncio.create_subprocess",
        ):
            assert token not in source, f"banned token: {token}"

    def test_writer_uses_atomic_rename(self):
        # Defense-in-depth: writer must NOT do plain `path.write_text`
        # on the target — must use temp + rename for atomicity.
        source = _WRITER_PATH.read_text()
        # `path.write_text(...)` would be a smell; we use mkstemp + os.replace.
        assert "tempfile.mkstemp" in source
        assert "os.replace" in source

    def test_writer_uses_flock_for_cross_process(self):
        source = _WRITER_PATH.read_text()
        assert "flock_exclusive" in source
