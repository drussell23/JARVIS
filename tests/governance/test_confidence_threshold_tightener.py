"""Gap #2 Slice 2 — confidence_threshold_tightener regression suite.

Covers:

  §1   AdaptationSurface enum extension
  §2   validator auto-registration
  §3   surface mismatch + kind vocabulary
  §4   sha256 hash prefix on both current + proposed
  §5   observation_count floor (env-tunable)
  §6   tighten-indicator (→) requirement in summary
  §7   payload presence + schema parity + deserialization
  §8   payload hash-recomputation match
  §9   substrate decision (APPLIED + non-empty kinds)
  §10  single-kind vs multi-dim consistency
  §11  helper functions (build_proposed_state_payload + classify)
  §12  end-to-end propose → validator wiring through AdaptationLedger
  §13  AST authority pins
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.adaptation.ledger import (
    AdaptationEvidence,
    AdaptationProposal,
    AdaptationSurface,
    MonotonicTighteningVerdict,
    OperatorDecisionStatus,
    get_surface_validator,
)
from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (
    CONFIDENCE_THRESHOLD_TIGHTENER_SCHEMA_VERSION,
    _confidence_threshold_validator,
    build_proposed_state_payload,
    classify_proposal_kind,
    confidence_threshold_observation_count_floor,
    install_surface_validator,
)
from backend.core.ouroboros.governance.verification.confidence_policy import (
    CONFIDENCE_POLICY_SCHEMA_VERSION,
    ConfidencePolicy,
    ConfidencePolicyKind,
    compute_policy_diff,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "adaptation"
    / "confidence_threshold_tightener.py"
)


def _baseline_policy() -> ConfidencePolicy:
    return ConfidencePolicy(
        floor=0.05, window_k=16, approaching_factor=1.5, enforce=False,
    )


def _replace(policy: ConfidencePolicy, **overrides) -> ConfidencePolicy:
    d = policy.to_dict()
    d.update(overrides)
    return ConfidencePolicy.from_dict(d)


def _build_proposal(
    *,
    current: ConfidencePolicy = None,
    proposed: ConfidencePolicy = None,
    kind: str = "raise_floor",
    proposed_hash: str = None,
    current_hash: str = None,
    observation_count: int = 5,
    summary: str = "floor 0.05 → 0.10; observed 5 sustained_low",
    payload_override=None,
    surface: AdaptationSurface = AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
) -> AdaptationProposal:
    """Compose an AdaptationProposal with sensible defaults that the
    validator should ACCEPT. Each test then mutates one field to
    target its specific failure path."""
    if current is None:
        current = _baseline_policy()
    if proposed is None:
        proposed = _replace(current, floor=0.10)
    if proposed_hash is None:
        proposed_hash = proposed.state_hash()
    if current_hash is None:
        current_hash = current.state_hash()
    if payload_override is None:
        payload = build_proposed_state_payload(
            current=current, proposed=proposed,
        )
    else:
        payload = payload_override
    return AdaptationProposal(
        schema_version="2.0",
        proposal_id="conf-test-1",
        surface=surface,
        proposal_kind=kind,
        evidence=AdaptationEvidence(
            window_days=1,
            observation_count=observation_count,
            summary=summary,
        ),
        current_state_hash=current_hash,
        proposed_state_hash=proposed_hash,
        monotonic_tightening_verdict=MonotonicTighteningVerdict.PASSED,
        proposed_at="2026-05-02T00:00:00Z",
        proposed_at_epoch=1.0,
        operator_decision=OperatorDecisionStatus.PENDING,
        proposed_state_payload=payload,
    )


# ============================================================================
# §1 — AdaptationSurface enum extension
# ============================================================================


class TestSurfaceEnumExtension:
    def test_new_surface_value(self):
        assert (
            AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS.value
            == "confidence_monitor.thresholds"
        )

    def test_at_least_six_surfaces(self):
        # Slice 2 adds a 6th. If the count drops we want the test
        # to catch the regression (without pinning an exact number
        # so future arcs can grow the enum freely).
        assert len(list(AdaptationSurface)) >= 6

    def test_existing_surfaces_unchanged(self):
        # Defense-in-depth: the original 5 strings stay byte-identical.
        # Any rename would silently break operator-approved YAML
        # files keyed by surface.value.
        assert (
            AdaptationSurface.SEMANTIC_GUARDIAN_PATTERNS.value
            == "semantic_guardian.patterns"
        )
        assert (
            AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS.value
            == "iron_gate.exploration_floors"
        )
        assert (
            AdaptationSurface.SCOPED_TOOL_BACKEND_MUTATION_BUDGET.value
            == "scoped_tool_backend.mutation_budget"
        )
        assert (
            AdaptationSurface.RISK_TIER_FLOOR_TIERS.value
            == "risk_tier_floor.tiers"
        )
        assert (
            AdaptationSurface.EXPLORATION_LEDGER_CATEGORY_WEIGHTS.value
            == "exploration_ledger.category_weights"
        )


# ============================================================================
# §2 — Validator auto-registration
# ============================================================================


class TestValidatorRegistration:
    """Other tests in the suite call ``reset_surface_validators()``
    which wipes ALL registrations including ours. Each test in this
    class re-installs first so the registration check is order-
    independent."""

    def test_validator_registered_at_import(self):
        install_surface_validator()  # idempotent re-registration
        v = get_surface_validator(
            AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        )
        assert v is not None
        assert v is _confidence_threshold_validator

    def test_install_is_idempotent(self):
        install_surface_validator()
        before = get_surface_validator(
            AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        )
        install_surface_validator()
        after = get_surface_validator(
            AdaptationSurface.CONFIDENCE_MONITOR_THRESHOLDS,
        )
        assert before is after


# ============================================================================
# §3 — Surface mismatch + kind vocabulary
# ============================================================================


class TestSurfaceAndKindGate:
    def test_wrong_surface_rejected(self):
        p = _build_proposal(
            surface=AdaptationSurface.IRON_GATE_EXPLORATION_FLOORS,
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "wrong_surface" in detail

    def test_kind_outside_vocabulary_rejected(self):
        p = _build_proposal(kind="totally_made_up_kind")
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "not_in_vocabulary" in detail

    def test_disabled_kind_not_in_vocabulary(self):
        # DISABLED is the master-off sentinel from Slice 1; it must
        # never appear as a proposal_kind.
        p = _build_proposal(kind="disabled")
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "not_in_vocabulary" in detail

    def test_each_concrete_kind_vocabulary_member(self):
        # Defense in depth: confirm each non-DISABLED ConfidencePolicyKind
        # value is in the vocabulary so future enum changes propagate.
        from backend.core.ouroboros.governance.adaptation.confidence_threshold_tightener import (
            _VALID_PROPOSAL_KINDS,
        )
        for k in ConfidencePolicyKind:
            if k is ConfidencePolicyKind.DISABLED:
                assert k.value not in _VALID_PROPOSAL_KINDS
            else:
                assert k.value in _VALID_PROPOSAL_KINDS
        # multi_dim sentinel is also valid
        assert "multi_dim_tighten" in _VALID_PROPOSAL_KINDS


# ============================================================================
# §4 — sha256 hash prefix on both current + proposed
# ============================================================================


class TestHashFormat:
    def test_proposed_hash_missing_sha_prefix_rejected(self):
        p = _build_proposal(proposed_hash="bad_hash")
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "proposed_hash_format" in detail

    def test_current_hash_missing_sha_prefix_rejected(self):
        p = _build_proposal(current_hash="bad_hash")
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "current_hash_format" in detail


# ============================================================================
# §5 — Observation count floor
# ============================================================================


class TestObservationFloor:
    def test_default_floor_is_three(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR",
            raising=False,
        )
        assert confidence_threshold_observation_count_floor() == 3

    def test_env_tunable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR", "10",
        )
        assert (
            confidence_threshold_observation_count_floor() == 10
        )

    def test_clamped_to_floor_one(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR", "0",
        )
        assert confidence_threshold_observation_count_floor() == 1

    def test_below_floor_rejects_proposal(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR", "10",
        )
        p = _build_proposal(observation_count=5)
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "observation_count_below_threshold" in detail

    def test_at_floor_accepted(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR", "5",
        )
        p = _build_proposal(observation_count=5)
        ok, _ = _confidence_threshold_validator(p)
        assert ok is True


# ============================================================================
# §6 — Tighten indicator
# ============================================================================


class TestTightenIndicator:
    def test_missing_arrow_rejected(self):
        p = _build_proposal(
            summary="floor 0.05 to 0.10; observed 5 events",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "missing_tighten_indicator" in detail

    def test_arrow_anywhere_in_summary_accepted(self):
        p = _build_proposal(summary="generic prose → with arrow")
        ok, _ = _confidence_threshold_validator(p)
        assert ok is True


# ============================================================================
# §7 — Payload presence + schema parity + deserialization
# ============================================================================


class TestPayloadShape:
    def test_missing_payload_rejected(self):
        p = _build_proposal(payload_override={})
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "payload_missing_or_empty" in detail

    def test_payload_proposed_not_dict(self):
        p = _build_proposal(payload_override={"proposed": "x", "current": {}})
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "proposed_not_dict" in detail

    def test_payload_current_not_dict(self):
        p = _build_proposal(payload_override={"proposed": {}, "current": []})
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "current_not_dict" in detail

    def test_schema_version_mismatch_rejected(self):
        baseline = _baseline_policy()
        proposed = _replace(baseline, floor=0.10)
        proposed_dict = proposed.to_dict()
        proposed_dict["schema_version"] = "confidence_policy.999"
        payload = {
            "current": baseline.to_dict(),
            "proposed": proposed_dict,
        }
        p = _build_proposal(payload_override=payload)
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "schema_mismatch" in detail


# ============================================================================
# §8 — Payload hash recomputation
# ============================================================================


class TestHashRecomputation:
    def test_proposed_hash_mismatch_rejected(self):
        p = _build_proposal(
            proposed_hash="sha256:" + ("0" * 64),
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "hash_mismatch:proposed" in detail

    def test_current_hash_mismatch_rejected(self):
        p = _build_proposal(
            current_hash="sha256:" + ("f" * 64),
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "hash_mismatch:current" in detail


# ============================================================================
# §9 — Substrate decision (APPLIED + non-empty kinds)
# ============================================================================


class TestSubstrateDecision:
    def test_loosen_proposal_rejected_by_substrate(self):
        # Build a proposal that LOOSENS floor (0.10 → 0.05). The
        # surface validator runs compute_policy_diff which catches
        # this and returns REJECTED_LOOSEN.
        current = _replace(_baseline_policy(), floor=0.10)
        proposed = _baseline_policy()  # floor=0.05 — loosen
        p = _build_proposal(
            current=current,
            proposed=proposed,
            kind="raise_floor",  # claimed kind irrelevant; substrate decides
            summary="floor 0.10 → 0.05 (this should be rejected)",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "policy_diff_not_applied" in detail

    def test_no_op_proposal_rejected(self):
        # Identical current + proposed → APPLIED but kinds=()
        baseline = _baseline_policy()
        p = _build_proposal(
            current=baseline,
            proposed=baseline,
            kind="raise_floor",
            summary="no-op snapshot →",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "no_op_proposal_rejected" in detail

    def test_applied_single_dim_accepted(self):
        p = _build_proposal()  # default raises floor 0.05 → 0.10
        ok, detail = _confidence_threshold_validator(p)
        assert ok is True, detail


# ============================================================================
# §10 — Single-kind vs multi-dim consistency
# ============================================================================


class TestKindConsistency:
    def test_single_kind_proposal_with_multi_dim_diff_rejected(self):
        # Proposal claims raise_floor but the diff also moves window_k
        # → kind_mismatch
        proposed = _replace(
            _baseline_policy(), floor=0.10, window_k=8,
        )
        p = _build_proposal(
            proposed=proposed,
            kind="raise_floor",
            summary="floor 0.05 → 0.10, window 16 → 8",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "single_kind_proposal_moved_many" in detail

    def test_multi_dim_proposal_with_single_kind_diff_rejected(self):
        # Proposal claims multi_dim_tighten but only floor moves
        p = _build_proposal(
            kind="multi_dim_tighten",
            summary="floor 0.05 → 0.10",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "multi_dim_only_one_kind_moved" in detail

    def test_multi_dim_proposal_accepted(self):
        proposed = _replace(
            _baseline_policy(),
            floor=0.10, window_k=8, approaching_factor=2.0,
        )
        p = _build_proposal(
            proposed=proposed,
            kind="multi_dim_tighten",
            summary="floor 0.05 → 0.10; window 16 → 8; factor 1.5 → 2.0",
        )
        ok, _ = _confidence_threshold_validator(p)
        assert ok is True

    def test_kind_mismatch_rejected(self):
        # Proposal claims widen_approaching but actually moves floor
        p = _build_proposal(
            kind="widen_approaching",
            summary="floor 0.05 → 0.10",
        )
        ok, detail = _confidence_threshold_validator(p)
        assert ok is False
        assert "kind_mismatch" in detail


# ============================================================================
# §11 — Helpers
# ============================================================================


class TestHelpers:
    def test_build_proposed_state_payload_shape(self):
        c = _baseline_policy()
        p = _replace(c, floor=0.10)
        payload = build_proposed_state_payload(current=c, proposed=p)
        assert "current" in payload
        assert "proposed" in payload
        assert payload["current"]["floor"] == 0.05
        assert payload["proposed"]["floor"] == 0.10

    def test_classify_no_op_returns_none(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        baseline = _baseline_policy()
        diff = compute_policy_diff(
            current=baseline, proposed=baseline,
        )
        assert classify_proposal_kind(diff) is None

    def test_classify_single_dim_returns_kind_value(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        baseline = _baseline_policy()
        diff = compute_policy_diff(
            current=baseline,
            proposed=_replace(baseline, floor=0.10),
        )
        assert classify_proposal_kind(diff) == "raise_floor"

    def test_classify_multi_dim_returns_sentinel(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_POLICY_ENABLED", "true",
        )
        baseline = _baseline_policy()
        diff = compute_policy_diff(
            current=baseline,
            proposed=_replace(baseline, floor=0.10, window_k=8),
        )
        assert classify_proposal_kind(diff) == "multi_dim_tighten"


# ============================================================================
# §12 — End-to-end: AdaptationLedger.propose dispatch through validator
# ============================================================================


def _propose_via_ledger(ledger, proposal: AdaptationProposal):
    """AdaptationLedger.propose takes keyword-only fields, not a
    pre-built AdaptationProposal. Project the dataclass into the
    expected kwargs."""
    return ledger.propose(
        proposal_id=proposal.proposal_id,
        surface=proposal.surface,
        proposal_kind=proposal.proposal_kind,
        evidence=proposal.evidence,
        current_state_hash=proposal.current_state_hash,
        proposed_state_hash=proposal.proposed_state_hash,
        proposed_state_payload=proposal.proposed_state_payload,
    )


class TestEndToEndPropose:
    @pytest.fixture(autouse=True)
    def _ensure_registered(self):
        """Other tests in the suite reset the surface validator
        registry. Re-install so end-to-end dispatch is reliable
        regardless of test ordering."""
        install_surface_validator()
        yield
        install_surface_validator()

    def test_validator_consulted_on_propose(self, tmp_path, monkeypatch):
        """A confidence proposal that PASSES the universal cage's
        kind allowlist (raise_floor) but FAILS our surface-specific
        validator (observation_count below floor) must surface the
        validator-sourced detail. This proves the dispatch path
        reaches the registered surface validator."""
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationLedger,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_THRESHOLD_OBSERVATION_FLOOR", "100",
        )
        ledger = AdaptationLedger(path=tmp_path / "adapt.jsonl")
        # Valid kind, but observation_count=5 < env floor=100.
        bad = _build_proposal(observation_count=5)
        result = _propose_via_ledger(ledger, bad)
        # Universal cage forwards surface validator failures through
        # WOULD_LOOSEN (the cage never trusts a surface to discriminate
        # between "structurally invalid" vs "would loosen" — it conservatively
        # treats every surface rejection as the cage rejecting it).
        assert "observation_count_below_threshold" in result.detail

    def test_valid_proposal_persists(self, tmp_path, monkeypatch):
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationLedger,
            ProposeStatus,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_ENABLED", "true",
        )
        ledger = AdaptationLedger(path=tmp_path / "adapt.jsonl")
        good = _build_proposal()
        result = _propose_via_ledger(ledger, good)
        assert result.status is ProposeStatus.OK, result.detail

    def test_loosen_proposal_rejected_via_ledger(
        self, tmp_path, monkeypatch,
    ):
        """End-to-end loosen rejection: build a payload that loosens
        floor and confirm the ledger rejects it (validator path AND
        universal cage path both block it)."""
        from backend.core.ouroboros.governance.adaptation.ledger import (
            AdaptationLedger,
            ProposeStatus,
        )
        monkeypatch.setenv(
            "JARVIS_ADAPTATION_LEDGER_ENABLED", "true",
        )
        ledger = AdaptationLedger(path=tmp_path / "adapt.jsonl")
        # Loosen floor 0.10 → 0.05
        current = _replace(_baseline_policy(), floor=0.10)
        proposed = _baseline_policy()
        bad = _build_proposal(
            current=current, proposed=proposed,
            kind="raise_floor",
            summary="floor 0.10 → 0.05 (loosen attempt)",
        )
        result = _propose_via_ledger(ledger, bad)
        assert result.status is not ProposeStatus.OK


# ============================================================================
# §13 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
    # confidence_monitor must NOT be imported here — env reads
    # are routed through ConfidencePolicy.from_environment which
    # lives in the substrate (Slice 1).
    "confidence_monitor",
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.", "Path(", "pathlib",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_forbidden_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        allowed = {
            "backend.core.ouroboros.governance.adaptation.ledger",
            "backend.core.ouroboros.governance.verification.confidence_policy",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: "
                        f"{module}"
                    )

    def test_no_filesystem_io(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_install_called_at_module_level(self, source):
        # Defense-in-depth: ensure the module-level install_ call
        # is present so import-time registration cannot regress.
        tree = ast.parse(source)
        module_level_calls = [
            n for n in tree.body
            if isinstance(n, ast.Expr)
            and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name)
            and n.value.func.id == "install_surface_validator"
        ]
        assert len(module_level_calls) == 1, (
            "exactly one module-level install_surface_validator() "
            "call required (got "
            f"{len(module_level_calls)})"
        )

    def test_schema_version_string(self):
        assert (
            CONFIDENCE_THRESHOLD_TIGHTENER_SCHEMA_VERSION
            == "confidence_threshold_tightener.1"
        )
