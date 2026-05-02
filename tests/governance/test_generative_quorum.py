"""Move 6 Slice 1 — Generative Quorum primitive regression spine.

Coverage tracks:

  * Env knobs — defaults, floors, ceilings, garbage tolerance
  * Closed taxonomy — ConsensusOutcome 5-value pin
  * Frozen-dataclass shape + serialization round-trip
  * compute_consensus full decision tree (every input → exactly
    one outcome) — empty / None / single / K-2 unanimous /
    K-3 unanimous / K-3 majority / K-3 disagreement / K-5 partial
    + canonical_signature / accepted_roll_id stability
  * Empty-signature defensive paths (Slice 2 returns "" on
    syntax error)
  * Defensive contract — never raises on garbage input
  * Authority invariants — AST-pinned (stdlib only — Slice 1 is
    pure-data)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import (
    generative_quorum as quorum_mod,
)
from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
    GENERATIVE_QUORUM_SCHEMA_VERSION,
    CandidateRoll,
    ConsensusOutcome,
    ConsensusVerdict,
    agreement_threshold,
    compute_consensus,
    quorum_enabled,
    quorum_k,
)


# ---------------------------------------------------------------------------
# 1. Env knobs — defaults + floors + ceilings + garbage
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_quorum_enabled_default_true_post_graduation(
        self, monkeypatch,
    ):
        # Q4 Priority #1 graduation (2026-05-02): master flag
        # default-true; operator authorized after empirical
        # verification that K× cost is structurally bounded.
        monkeypatch.delenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", raising=False,
        )
        assert quorum_enabled() is True

    @pytest.mark.parametrize(
        "value,expected",
        [
            # Whitespace / unset = current default (now True post
            # Q4 Priority #1 graduation).
            ("", True), ("   ", True),
            # Explicit falsy variants — instant rollback path.
            ("0", False), ("false", False),
            ("no", False), ("garbage", False),
            # Explicit truthy variants.
            ("1", True), ("true", True), ("YES", True),
            ("on", True),
        ],
    )
    def test_quorum_enabled_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", value,
        )
        assert quorum_enabled() is expected

    def test_quorum_k_default(self, monkeypatch):
        monkeypatch.delenv("JARVIS_QUORUM_K", raising=False)
        assert quorum_k() == 3

    def test_quorum_k_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_QUORUM_K", "1")
        assert quorum_k() == 2  # floor

    def test_quorum_k_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_QUORUM_K", "100")
        assert quorum_k() == 5  # ceiling

    def test_quorum_k_garbage_falls_to_default(self, monkeypatch):
        monkeypatch.setenv("JARVIS_QUORUM_K", "garbage")
        assert quorum_k() == 3

    def test_agreement_threshold_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_QUORUM_AGREEMENT_THRESHOLD", raising=False,
        )
        assert agreement_threshold() == 2

    def test_agreement_threshold_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_QUORUM_AGREEMENT_THRESHOLD", "1",
        )
        assert agreement_threshold() == 2  # floor

    def test_schema_version_pinned(self):
        assert GENERATIVE_QUORUM_SCHEMA_VERSION == \
            "generative_quorum.1"


# ---------------------------------------------------------------------------
# 2. ConsensusOutcome closed-taxonomy pin
# ---------------------------------------------------------------------------


class TestConsensusOutcomeTaxonomy:
    def test_taxonomy_pinned_at_5_values(self):
        # Closed 5-value taxonomy. Adding a value requires
        # explicit graduation work; this catches silent additions.
        expected = {
            "consensus", "majority_consensus", "disagreement",
            "disabled", "failed",
        }
        assert {o.value for o in ConsensusOutcome} == expected


# ---------------------------------------------------------------------------
# 3. Frozen dataclass shape + serialization
# ---------------------------------------------------------------------------


class TestDataclassShape:
    def test_candidate_roll_is_frozen(self):
        r = CandidateRoll(
            roll_id="r1", candidate_diff="d",
            ast_signature="abc",
        )
        with pytest.raises((AttributeError, Exception)):
            r.candidate_diff = "x"  # type: ignore[misc]

    def test_consensus_verdict_is_frozen(self):
        v = ConsensusVerdict(
            outcome=ConsensusOutcome.CONSENSUS,
            agreement_count=3,
            distinct_count=1,
            total_rolls=3,
            canonical_signature="abc",
            accepted_roll_id="r1",
            detail="x",
        )
        with pytest.raises((AttributeError, Exception)):
            v.detail = "y"  # type: ignore[misc]

    def test_candidate_roll_to_dict(self):
        r = CandidateRoll(
            roll_id="r1",
            candidate_diff="diff text",
            ast_signature="sha256-hex",
            cost_estimate_usd=0.03,
            seed=42,
        )
        d = r.to_dict()
        assert d["roll_id"] == "r1"
        assert d["seed"] == 42
        assert d["schema_version"] == \
            GENERATIVE_QUORUM_SCHEMA_VERSION

    def test_consensus_verdict_to_dict(self):
        v = ConsensusVerdict(
            outcome=ConsensusOutcome.MAJORITY_CONSENSUS,
            agreement_count=2,
            distinct_count=2,
            total_rolls=3,
            canonical_signature="abc",
            accepted_roll_id="r1",
            detail="majority",
        )
        d = v.to_dict()
        assert d["outcome"] == "majority_consensus"
        assert d["agreement_count"] == 2

    def test_consensus_verdict_is_actionable(self):
        for actionable in (
            ConsensusOutcome.CONSENSUS,
            ConsensusOutcome.MAJORITY_CONSENSUS,
        ):
            v = ConsensusVerdict(
                outcome=actionable,
                agreement_count=2, distinct_count=1,
                total_rolls=2, canonical_signature="x",
                accepted_roll_id="r1", detail="x",
            )
            assert v.is_actionable() is True

        for non_actionable in (
            ConsensusOutcome.DISAGREEMENT,
            ConsensusOutcome.DISABLED,
            ConsensusOutcome.FAILED,
        ):
            v = ConsensusVerdict(
                outcome=non_actionable,
                agreement_count=0, distinct_count=0,
                total_rolls=0, canonical_signature=None,
                accepted_roll_id=None, detail="x",
            )
            assert v.is_actionable() is False

    def test_consensus_verdict_is_unanimous(self):
        unanimous = ConsensusVerdict(
            outcome=ConsensusOutcome.CONSENSUS,
            agreement_count=3, distinct_count=1,
            total_rolls=3, canonical_signature="x",
            accepted_roll_id="r1", detail="x",
        )
        assert unanimous.is_unanimous() is True
        # MAJORITY_CONSENSUS is actionable but NOT unanimous
        majority = ConsensusVerdict(
            outcome=ConsensusOutcome.MAJORITY_CONSENSUS,
            agreement_count=2, distinct_count=2,
            total_rolls=3, canonical_signature="x",
            accepted_roll_id="r1", detail="x",
        )
        assert majority.is_unanimous() is False
        assert majority.is_actionable() is True


# ---------------------------------------------------------------------------
# 4. CandidateRoll round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_round_trip_preserves_equality(self):
        r = CandidateRoll(
            roll_id="r1",
            candidate_diff="diff",
            ast_signature="abc",
            cost_estimate_usd=0.05,
            seed=42,
        )
        restored = CandidateRoll.from_dict(r.to_dict())
        assert restored == r

    def test_round_trip_with_no_seed(self):
        r = CandidateRoll(
            roll_id="r1",
            candidate_diff="diff",
            ast_signature="abc",
        )
        restored = CandidateRoll.from_dict(r.to_dict())
        assert restored == r
        assert restored.seed is None

    def test_schema_mismatch_returns_none(self):
        r = CandidateRoll(
            roll_id="r1", candidate_diff="d", ast_signature="a",
        )
        d = r.to_dict()
        d["schema_version"] = "wrong.0"
        assert CandidateRoll.from_dict(d) is None

    def test_missing_required_field_returns_none(self):
        r = CandidateRoll(
            roll_id="r1", candidate_diff="d", ast_signature="a",
        )
        d = r.to_dict()
        del d["roll_id"]
        assert CandidateRoll.from_dict(d) is None

    def test_malformed_seed_returns_none(self):
        bad = {
            "roll_id": "r1",
            "candidate_diff": "d",
            "ast_signature": "a",
            "cost_estimate_usd": 0.0,
            "seed": "not_an_int",
            "schema_version": GENERATIVE_QUORUM_SCHEMA_VERSION,
        }
        assert CandidateRoll.from_dict(bad) is None


# ---------------------------------------------------------------------------
# 5. compute_consensus — full decision tree
# ---------------------------------------------------------------------------


def _r(roll_id: str, signature: str = "abc") -> CandidateRoll:
    return CandidateRoll(
        roll_id=roll_id,
        candidate_diff="diff",
        ast_signature=signature,
    )


class TestComputeConsensus:
    def test_none_returns_failed(self):
        v = compute_consensus(None)
        assert v.outcome is ConsensusOutcome.FAILED
        assert "None" in v.detail

    def test_empty_returns_failed(self):
        v = compute_consensus([])
        assert v.outcome is ConsensusOutcome.FAILED
        assert "insufficient" in v.detail

    def test_single_roll_returns_failed(self):
        v = compute_consensus([_r("r1")])
        assert v.outcome is ConsensusOutcome.FAILED
        assert "got 1" in v.detail

    def test_two_unanimous_returns_consensus(self):
        v = compute_consensus([_r("r1", "abc"), _r("r2", "abc")])
        assert v.outcome is ConsensusOutcome.CONSENSUS
        assert v.agreement_count == 2
        assert v.canonical_signature == "abc"
        assert v.accepted_roll_id == "r1"
        assert v.is_unanimous() is True

    def test_two_disagree_returns_disagreement(self):
        # K=2, distinct → DISAGREEMENT (largest=1 < threshold=2)
        v = compute_consensus(
            [_r("r1", "a"), _r("r2", "b")],
        )
        assert v.outcome is ConsensusOutcome.DISAGREEMENT
        assert v.distinct_count == 2

    def test_three_unanimous_returns_consensus(self):
        v = compute_consensus([
            _r("r1", "abc"), _r("r2", "abc"), _r("r3", "abc"),
        ])
        assert v.outcome is ConsensusOutcome.CONSENSUS
        assert v.agreement_count == 3
        assert v.is_unanimous() is True

    def test_three_majority_2_of_3_returns_majority(self):
        v = compute_consensus([
            _r("r1", "abc"), _r("r2", "abc"), _r("r3", "xyz"),
        ])
        assert v.outcome is ConsensusOutcome.MAJORITY_CONSENSUS
        assert v.agreement_count == 2
        assert v.distinct_count == 2
        assert v.canonical_signature == "abc"
        assert v.accepted_roll_id == "r1"
        assert v.is_unanimous() is False
        assert v.is_actionable() is True

    def test_three_distinct_returns_disagreement(self):
        v = compute_consensus([
            _r("r1", "a"), _r("r2", "b"), _r("r3", "c"),
        ])
        assert v.outcome is ConsensusOutcome.DISAGREEMENT
        assert v.agreement_count == 1
        assert v.distinct_count == 3
        assert v.canonical_signature is None
        assert v.accepted_roll_id is None

    def test_five_with_threshold_3_majority(self):
        # K=5, cluster=3 (signature "abc" wins)
        v = compute_consensus([
            _r("r1", "abc"), _r("r2", "abc"), _r("r3", "abc"),
            _r("r4", "xyz"), _r("r5", "qrs"),
        ], threshold=3)
        assert v.outcome is ConsensusOutcome.MAJORITY_CONSENSUS
        assert v.agreement_count == 3

    def test_five_with_threshold_3_disagreement(self):
        # K=5, no cluster reaches 3
        v = compute_consensus([
            _r("r1", "a"), _r("r2", "a"), _r("r3", "b"),
            _r("r4", "b"), _r("r5", "c"),
        ], threshold=3)
        assert v.outcome is ConsensusOutcome.DISAGREEMENT
        assert v.agreement_count == 2  # largest cluster

    def test_canonical_from_first_roll_in_largest_cluster(self):
        # Determinism: when multiple rolls share signature,
        # accepted_roll_id is the first one encountered.
        v = compute_consensus([
            _r("r3", "abc"), _r("r1", "abc"), _r("r2", "xyz"),
        ])
        assert v.outcome is ConsensusOutcome.MAJORITY_CONSENSUS
        # First roll with signature "abc" was r3 (input order)
        assert v.accepted_roll_id == "r3"

    def test_all_empty_signatures_returns_disagreement(self):
        # Slice 2 returns "" on syntax error — treat as no-signal
        v = compute_consensus([
            _r("r1", ""), _r("r2", ""), _r("r3", ""),
        ])
        assert v.outcome is ConsensusOutcome.DISAGREEMENT
        assert "empty signatures" in v.detail.lower()

    def test_mixed_empty_and_valid_signatures(self):
        # 2 empty + 1 valid: cluster=1 (only the valid one)
        # < threshold=2 → DISAGREEMENT
        v = compute_consensus([
            _r("r1", "abc"), _r("r2", ""), _r("r3", ""),
        ])
        assert v.outcome is ConsensusOutcome.DISAGREEMENT

    def test_threshold_defaults_from_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_QUORUM_AGREEMENT_THRESHOLD", "2",
        )
        v = compute_consensus([
            _r("r1", "abc"), _r("r2", "abc"), _r("r3", "xyz"),
        ])
        assert v.outcome is ConsensusOutcome.MAJORITY_CONSENSUS

    def test_threshold_below_floor_clamped(self):
        # Caller passes threshold=1, but floor is 2
        v = compute_consensus([
            _r("r1", "a"), _r("r2", "b"),
        ], threshold=1)
        # cluster=1 < clamped_threshold=2 → DISAGREEMENT (NOT
        # majority despite threshold=1 caller request)
        assert v.outcome is ConsensusOutcome.DISAGREEMENT

    def test_filters_non_candidate_roll_inputs(self):
        # Only CandidateRoll instances counted
        rolls = [
            _r("r1", "abc"),
            {"not": "a roll"},  # type: ignore[list-item]
            "neither is this",  # type: ignore[list-item]
            _r("r2", "abc"),
        ]
        v = compute_consensus(rolls)
        # Only 2 valid rolls → CONSENSUS
        assert v.outcome is ConsensusOutcome.CONSENSUS
        assert v.total_rolls == 2

    def test_non_iterable_returns_failed(self):
        v = compute_consensus(42)  # type: ignore[arg-type]
        # 42 isn't iterable — caught by TypeError or filtered to []
        assert v.outcome is ConsensusOutcome.FAILED

    def test_never_raises_on_pathological_input(self):
        # Pass a generator that raises mid-iteration
        def _bad_gen():
            yield _r("r1")
            raise RuntimeError("simulated mid-iter error")
            yield _r("r2")  # unreachable

        # Must NOT propagate
        v = compute_consensus(_bad_gen())
        assert isinstance(v, ConsensusVerdict)


# ---------------------------------------------------------------------------
# 6. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
)


_FORBIDDEN_MUTATION_TOOL_NAMES = (
    "edit_file",
    "write_file",
    "delete_file",
    "run_tests",
    "bash",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
                / "generative_quorum.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"generative_quorum imports forbidden modules: "
            f"{offenders}"
        )

    def test_no_mutation_tool_name_references_in_code(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.id == fb:
                        offenders.append(node.id)
            elif isinstance(node, ast.Attribute):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.attr == fb:
                        offenders.append(node.attr)
        assert offenders == [], (
            f"generative_quorum references mutation tool names "
            f"in code: {offenders}"
        )

    def test_no_governance_imports(self):
        # Slice 1 is pure-data; no governance modules consumed
        # yet (Slices 2-5 add ast_canonical / candidate_generator /
        # cost_contract_assertion as needed).
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith(
                    "backend.core.ouroboros.governance",
                ), (
                    f"Slice 1 must be stdlib-only (no governance "
                    f"imports); found: {mod}"
                )

    def test_no_disk_writes(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"forbidden disk-write token: {tok!r}"
            )

    def test_no_async_functions(self):
        # Slice 1 is pure-sync. Async runner ships in Slice 3.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        async_defs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert async_defs == [], (
            f"Slice 1 must be pure-sync; async found: {async_defs}"
        )

    def test_public_api_exported(self):
        expected = {
            "CandidateRoll",
            "ConsensusOutcome",
            "ConsensusVerdict",
            "GENERATIVE_QUORUM_SCHEMA_VERSION",
            "agreement_threshold",
            "compute_consensus",
            "quorum_enabled",
            "quorum_k",
        }
        assert set(quorum_mod.__all__) == expected

    def test_module_is_pure_stdlib(self):
        # Verify imports list is reasonable
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        stdlib_top = {
            "enum", "logging", "os", "collections",
            "dataclasses", "typing", "__future__",
        }
        for name in imports:
            top_level = name.split(".")[0]
            assert top_level in stdlib_top, (
                f"unexpected non-stdlib import: {name}"
            )
