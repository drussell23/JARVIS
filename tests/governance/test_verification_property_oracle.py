"""Phase 2 Slice 2.1 — PropertyOracle regression spine.

Pins:
  §1   oracle_enabled flag — default false; case-tolerant
  §2   VerdictKind — 4-valued enum (PASSED/FAILED/INSUFFICIENT/ERROR)
  §3   Property — frozen + hashable + factory normalization
  §4   Property.make — coerces metadata dict to frozen tuple
  §5   PropertyVerdict — frozen + .passed + .is_terminal helpers
  §6   register_evaluator — basic registration
  §7   register_evaluator — idempotent on identical re-register
  §8   register_evaluator — non-overwrite default + overwrite=True opt-in
  §9   register_evaluator — empty-key rejection (defensive)
  §10  is_kind_registered + known_kinds
  §11  Oracle — unregistered kind → INSUFFICIENT_EVIDENCE
  §12  Oracle — missing evidence_required → INSUFFICIENT_EVIDENCE
  §13  Oracle — evaluator raises → EVALUATOR_ERROR
  §14  Oracle — evaluator returns non-Verdict → EVALUATOR_ERROR
  §15  Oracle — None Property → EVALUATOR_ERROR
  §16  Oracle — populates evidence_hash + ts when evaluator forgets
  §17  Seed evaluator: test_passes
  §18  Seed evaluator: key_present
  §19  Seed evaluator: numeric_below_threshold
  §20  Seed evaluator: numeric_above_threshold
  §21  Seed evaluator: string_matches
  §22  Seed evaluator: set_subset
  §23  Authority invariants — no orchestrator/phase_runner imports
  §24  Authority invariants — pure stdlib + Antigravity adapter only
  §25  Six seed evaluators registered at module load
  §26  Evidence hash uses Antigravity canonical_hash when available
  §27  Public API exposed from package __init__
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.verification import (
    Property,
    PropertyOracle,
    PropertyVerdict,
    VerdictKind,
    get_default_oracle,
    oracle_enabled,
    register_evaluator,
)
from backend.core.ouroboros.governance.verification.property_oracle import (
    PROPERTY_VERDICT_SCHEMA_VERSION,
    is_kind_registered,
    known_kinds,
    reset_registry_for_tests,
)


@pytest.fixture
def fresh_registry():
    """Reset the registry between tests so seed evaluators are
    re-registered cleanly."""
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_oracle_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_VERIFICATION_ORACLE_ENABLED", raising=False)
    assert oracle_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_oracle_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_VERIFICATION_ORACLE_ENABLED", val)
    assert oracle_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage", ""])
def test_oracle_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_VERIFICATION_ORACLE_ENABLED", val)
    assert oracle_enabled() is False


# ---------------------------------------------------------------------------
# §2 — VerdictKind enum
# ---------------------------------------------------------------------------


def test_verdict_kind_has_four_values() -> None:
    members = {m.value for m in VerdictKind}
    assert members == {
        "passed", "failed", "insufficient_evidence", "evaluator_error",
    }


def test_verdict_kind_str_enum() -> None:
    """VerdictKind extends str so it serializes cleanly into JSON."""
    assert VerdictKind.PASSED == "passed"
    assert isinstance(VerdictKind.PASSED.value, str)


# ---------------------------------------------------------------------------
# §3-§4 — Property
# ---------------------------------------------------------------------------


def test_property_is_frozen() -> None:
    p = Property(kind="test_passes", name="my_test")
    with pytest.raises(Exception):
        p.kind = "different"  # type: ignore[misc]


def test_property_make_coerces_metadata() -> None:
    p = Property.make(
        kind="numeric_below_threshold",
        name="latency_check",
        evidence_required=("observed", "threshold"),
        metadata={"unit": "ms", "z_score": 2.0},
    )
    assert p.kind == "numeric_below_threshold"
    assert p.name == "latency_check"
    assert p.evidence_required == ("observed", "threshold")
    # Metadata sorted by key + frozen as tuple of pairs
    assert p.metadata == (("unit", "ms"), ("z_score", 2.0))


def test_property_make_handles_garbage_metadata() -> None:
    """Non-dict metadata → empty tuple; NEVER raises."""
    p = Property.make(
        kind="x", name="y", metadata="not a dict",  # type: ignore[arg-type]
    )
    assert p.metadata == ()


def test_property_make_handles_garbage_evidence_required() -> None:
    p = Property.make(
        kind="x", name="y",
        evidence_required="not a tuple",  # type: ignore[arg-type]
    )
    # Strings ARE iterable so we get a tuple of single chars; that's fine
    assert isinstance(p.evidence_required, tuple)


def test_property_make_falls_back_on_empty_kind() -> None:
    p = Property.make(kind="", name="")
    assert p.kind == "unknown"
    assert p.name == "unnamed"


def test_property_metadata_dict() -> None:
    p = Property.make(kind="x", name="y", metadata={"a": 1, "b": 2})
    assert p.metadata_dict() == {"a": 1, "b": 2}


def test_property_is_hashable() -> None:
    """Frozen dataclasses are hashable — required for use as dict
    keys + set members + decision-runtime ledger fingerprints."""
    p = Property(kind="x", name="y")
    s = {p}  # smoke test: doesn't raise
    assert p in s


# ---------------------------------------------------------------------------
# §5 — PropertyVerdict
# ---------------------------------------------------------------------------


def test_verdict_is_frozen() -> None:
    v = PropertyVerdict(
        property_name="x", kind="y", verdict=VerdictKind.PASSED,
    )
    with pytest.raises(Exception):
        v.confidence = 0.5  # type: ignore[misc]


def test_verdict_passed_helper() -> None:
    assert PropertyVerdict(
        property_name="x", kind="y", verdict=VerdictKind.PASSED,
    ).passed is True
    assert PropertyVerdict(
        property_name="x", kind="y", verdict=VerdictKind.FAILED,
    ).passed is False
    assert PropertyVerdict(
        property_name="x", kind="y",
        verdict=VerdictKind.INSUFFICIENT_EVIDENCE,
    ).passed is False


def test_verdict_is_terminal_helper() -> None:
    """PASSED/FAILED are terminal; INSUFFICIENT/ERROR are not."""
    for terminal_kind in (VerdictKind.PASSED, VerdictKind.FAILED):
        assert PropertyVerdict(
            property_name="x", kind="y", verdict=terminal_kind,
        ).is_terminal is True
    for non_terminal in (
        VerdictKind.INSUFFICIENT_EVIDENCE, VerdictKind.EVALUATOR_ERROR,
    ):
        assert PropertyVerdict(
            property_name="x", kind="y", verdict=non_terminal,
        ).is_terminal is False


def test_verdict_schema_version_pinned() -> None:
    """Schema version is pinned for ledger forward-compat."""
    assert PROPERTY_VERDICT_SCHEMA_VERSION == "property_verdict.1"


# ---------------------------------------------------------------------------
# §6-§9 — Registry
# ---------------------------------------------------------------------------


def test_register_evaluator_basic(fresh_registry) -> None:
    def my_check(prop, evidence):
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.PASSED, confidence=1.0,
        )
    register_evaluator(
        kind="custom_kind", evaluate=my_check,
        description="my custom check",
    )
    assert is_kind_registered("custom_kind")


def test_register_evaluator_idempotent_silent(
    fresh_registry, caplog,
) -> None:
    """Re-registering the SAME callable is silent — no log noise."""
    import logging

    def my_check(prop, evidence):
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.PASSED,
        )

    caplog.set_level(logging.INFO)
    register_evaluator(kind="x", evaluate=my_check)
    caplog.clear()
    register_evaluator(kind="x", evaluate=my_check)  # re-register
    info_logs = [r for r in caplog.records if "registered" in r.getMessage()]
    assert info_logs == [], "re-register of same callable should be silent"


def test_register_evaluator_non_overwrite_logs(
    fresh_registry, caplog,
) -> None:
    """Re-registering with a DIFFERENT callable without overwrite
    logs an INFO + does NOT replace."""
    import logging

    def check_first(prop, evidence):
        return PropertyVerdict(
            property_name="a", kind=prop.kind, verdict=VerdictKind.PASSED,
        )

    def check_second(prop, evidence):
        return PropertyVerdict(
            property_name="b", kind=prop.kind, verdict=VerdictKind.PASSED,
        )

    register_evaluator(kind="x", evaluate=check_first, description="A")
    caplog.set_level(logging.INFO)
    register_evaluator(kind="x", evaluate=check_second, description="B")
    # Original (check_first) should still be active
    oracle = get_default_oracle()
    p = Property.make(kind="x", name="check")
    v = oracle.evaluate(prop=p, evidence={})
    assert v.property_name == "a"  # check_first still in place
    info_logs = [
        r for r in caplog.records if "already" in r.getMessage()
    ]
    assert len(info_logs) >= 1


def test_register_evaluator_overwrite_replaces(fresh_registry) -> None:
    def check_first(prop, evidence):
        return PropertyVerdict(
            property_name="A", kind=prop.kind, verdict=VerdictKind.PASSED,
        )

    def check_second(prop, evidence):
        return PropertyVerdict(
            property_name="B", kind=prop.kind, verdict=VerdictKind.PASSED,
        )

    register_evaluator(kind="x", evaluate=check_first)
    register_evaluator(kind="x", evaluate=check_second, overwrite=True)
    oracle = get_default_oracle()
    p = Property.make(kind="x", name="check")
    v = oracle.evaluate(prop=p, evidence={})
    assert v.property_name == "B"  # replaced


def test_register_evaluator_empty_key_rejected(fresh_registry) -> None:
    """Empty/whitespace kind is silently rejected — defensive
    against bad operator input."""
    register_evaluator(kind="", evaluate=lambda p, e: None)
    register_evaluator(kind="   ", evaluate=lambda p, e: None)
    assert not is_kind_registered("")
    assert not is_kind_registered("   ")


def test_register_evaluator_none_callable_rejected(
    fresh_registry,
) -> None:
    register_evaluator(kind="x", evaluate=None)  # type: ignore[arg-type]
    assert not is_kind_registered("x")


def test_known_kinds_includes_seed_evaluators(fresh_registry) -> None:
    """The six seed evaluators are registered at module load AND
    re-registered by reset_registry_for_tests."""
    kinds = set(known_kinds())
    seed_kinds = {
        "test_passes", "key_present",
        "numeric_below_threshold", "numeric_above_threshold",
        "string_matches", "set_subset",
    }
    assert seed_kinds <= kinds


# ---------------------------------------------------------------------------
# §11-§16 — Oracle dispatch behavior
# ---------------------------------------------------------------------------


def test_oracle_unregistered_kind_returns_insufficient(
    fresh_registry,
) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="totally_unregistered", name="x")
    v = oracle.evaluate(prop=p, evidence={})
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE
    assert "no evaluator registered" in v.reason


def test_oracle_missing_evidence_returns_insufficient(
    fresh_registry,
) -> None:
    oracle = get_default_oracle()
    p = Property.make(
        kind="test_passes", name="my_test",
        evidence_required=("exit_code",),
    )
    # Empty evidence — exit_code missing
    v = oracle.evaluate(prop=p, evidence={})
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE
    assert "exit_code" in v.reason


def test_oracle_evaluator_raises_returns_error(fresh_registry) -> None:
    def boom(prop, evidence):
        raise RuntimeError("simulated check fault")

    register_evaluator(kind="boom", evaluate=boom, overwrite=True)
    oracle = get_default_oracle()
    p = Property.make(kind="boom", name="x")
    v = oracle.evaluate(prop=p, evidence={})
    assert v.verdict is VerdictKind.EVALUATOR_ERROR
    assert "RuntimeError" in v.reason
    assert "simulated check fault" in v.reason


def test_oracle_evaluator_returns_non_verdict(fresh_registry) -> None:
    def bad_return(prop, evidence):
        return "not a verdict"  # type: ignore[return-value]

    register_evaluator(kind="bad", evaluate=bad_return, overwrite=True)
    oracle = get_default_oracle()
    p = Property.make(kind="bad", name="x")
    v = oracle.evaluate(prop=p, evidence={})
    assert v.verdict is VerdictKind.EVALUATOR_ERROR
    assert "instead of PropertyVerdict" in v.reason


def test_oracle_none_property_returns_error(fresh_registry) -> None:
    """Defensive: None as prop doesn't crash."""
    oracle = get_default_oracle()
    v = oracle.evaluate(prop=None, evidence={})  # type: ignore[arg-type]
    assert v.verdict is VerdictKind.EVALUATOR_ERROR
    assert "is None" in v.reason


def test_oracle_populates_missing_evidence_hash(fresh_registry) -> None:
    """If a check forgets to set evidence_hash, the dispatcher
    fills it in defensively."""
    def lazy_check(prop, evidence):
        # Doesn't set evidence_hash
        return PropertyVerdict(
            property_name=prop.name, kind=prop.kind,
            verdict=VerdictKind.PASSED, confidence=1.0, reason="ok",
        )

    register_evaluator(kind="lazy", evaluate=lazy_check, overwrite=True)
    oracle = get_default_oracle()
    p = Property.make(kind="lazy", name="x")
    v = oracle.evaluate(prop=p, evidence={"some": "data"})
    assert v.evidence_hash != ""  # populated by dispatcher
    assert v.evaluation_ts_unix > 0


# ---------------------------------------------------------------------------
# §17-§22 — Seed evaluators
# ---------------------------------------------------------------------------


def test_seed_test_passes_pass(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )
    v = oracle.evaluate(prop=p, evidence={"exit_code": 0})
    assert v.verdict is VerdictKind.PASSED
    assert v.confidence == 1.0


def test_seed_test_passes_fail(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )
    v = oracle.evaluate(prop=p, evidence={"exit_code": 1})
    assert v.verdict is VerdictKind.FAILED


def test_seed_test_passes_non_int(fresh_registry) -> None:
    """Non-integer exit_code → INSUFFICIENT_EVIDENCE (not FAILED)."""
    oracle = get_default_oracle()
    p = Property.make(kind="test_passes", name="t")
    v = oracle.evaluate(
        prop=p, evidence={"exit_code": "garbage"},
    )
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE


def test_seed_key_present(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="key_present", name="k")
    assert oracle.evaluate(
        prop=p, evidence={"present": True},
    ).verdict is VerdictKind.PASSED
    assert oracle.evaluate(
        prop=p, evidence={"present": False},
    ).verdict is VerdictKind.FAILED
    # Missing key → defaults to False → FAILED
    assert oracle.evaluate(
        prop=p, evidence={},
    ).verdict is VerdictKind.FAILED


def test_seed_numeric_below_threshold(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="numeric_below_threshold", name="n")
    assert oracle.evaluate(
        prop=p, evidence={"observed": 5.0, "threshold": 10.0},
    ).verdict is VerdictKind.PASSED
    assert oracle.evaluate(
        prop=p, evidence={"observed": 15.0, "threshold": 10.0},
    ).verdict is VerdictKind.FAILED
    # equal is NOT below
    assert oracle.evaluate(
        prop=p, evidence={"observed": 10.0, "threshold": 10.0},
    ).verdict is VerdictKind.FAILED


def test_seed_numeric_above_threshold(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="numeric_above_threshold", name="n")
    assert oracle.evaluate(
        prop=p, evidence={"observed": 15.0, "threshold": 10.0},
    ).verdict is VerdictKind.PASSED
    assert oracle.evaluate(
        prop=p, evidence={"observed": 5.0, "threshold": 10.0},
    ).verdict is VerdictKind.FAILED


def test_seed_numeric_threshold_non_numeric(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="numeric_below_threshold", name="n")
    v = oracle.evaluate(
        prop=p, evidence={"observed": "not a number", "threshold": 10.0},
    )
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE


def test_seed_string_matches(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="string_matches", name="s")
    assert oracle.evaluate(
        prop=p, evidence={"actual": "hello", "expected": "hello"},
    ).verdict is VerdictKind.PASSED
    assert oracle.evaluate(
        prop=p, evidence={"actual": "hello", "expected": "world"},
    ).verdict is VerdictKind.FAILED


def test_seed_set_subset(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="set_subset", name="s")
    assert oracle.evaluate(
        prop=p,
        evidence={"actual": ["a", "b"], "allowed": ["a", "b", "c"]},
    ).verdict is VerdictKind.PASSED
    # Empty actual ⊆ any allowed
    assert oracle.evaluate(
        prop=p, evidence={"actual": [], "allowed": ["a"]},
    ).verdict is VerdictKind.PASSED
    # Extras → FAILED
    assert oracle.evaluate(
        prop=p,
        evidence={"actual": ["a", "z"], "allowed": ["a", "b"]},
    ).verdict is VerdictKind.FAILED


def test_seed_set_subset_non_iterable(fresh_registry) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="set_subset", name="s")
    v = oracle.evaluate(
        prop=p, evidence={"actual": 42, "allowed": ["a"]},
    )
    assert v.verdict is VerdictKind.INSUFFICIENT_EVIDENCE


# ---------------------------------------------------------------------------
# §23-§24 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import (
        property_oracle,
    )
    src = inspect.getsource(property_oracle)
    forbidden = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.phase_runner ",
        "from backend.core.ouroboros.governance.candidate_generator",
    )
    for f in forbidden:
        assert f not in src, f"property_oracle must NOT contain {f!r}"


def test_no_phase_runners_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import (
        property_oracle,
    )
    src = inspect.getsource(property_oracle)
    assert "phase_runners" not in src


def test_no_provider_imports() -> None:
    """Verification is a substrate — must NOT import providers."""
    import inspect
    from backend.core.ouroboros.governance.verification import (
        property_oracle,
    )
    src = inspect.getsource(property_oracle)
    assert "doubleword_provider" not in src
    assert "claude_provider" not in src.lower()
    assert "import providers" not in src


# ---------------------------------------------------------------------------
# §25 — Six seed evaluators registered at module load
# ---------------------------------------------------------------------------


def test_six_seed_evaluators_present_after_import() -> None:
    """Module-level import must register the six seed evaluators.

    Avoid importlib.reload: it breaks the cross-module reference
    graph (RepeatRunner + package __init__ still hold stale
    references to the OLD _EVALUATORS dict). Instead, verify the
    seed kinds are present after a fresh registry reset (which
    re-runs _register_seed_evaluators on the LIVE module)."""
    reset_registry_for_tests()
    expected = {
        "test_passes", "key_present",
        "numeric_below_threshold", "numeric_above_threshold",
        "string_matches", "set_subset",
    }
    assert expected <= set(known_kinds())


def test_oracle_singleton_returns_same_instance() -> None:
    o1 = get_default_oracle()
    o2 = get_default_oracle()
    assert o1 is o2


# ---------------------------------------------------------------------------
# §26 — Evidence hash via Antigravity canonical_hash
# ---------------------------------------------------------------------------


def test_evidence_hash_canonical_collapse(fresh_registry) -> None:
    """Evidence dicts with different key ordering → same hash.
    Proves canonical_hash is being used."""
    oracle = get_default_oracle()
    p = Property.make(
        kind="test_passes", name="t", evidence_required=("exit_code",),
    )
    v1 = oracle.evaluate(
        prop=p, evidence={"exit_code": 0, "extra": "a"},
    )
    v2 = oracle.evaluate(
        prop=p, evidence={"extra": "a", "exit_code": 0},
    )
    assert v1.evidence_hash == v2.evidence_hash


def test_evidence_hash_differs_on_different_evidence(
    fresh_registry,
) -> None:
    oracle = get_default_oracle()
    p = Property.make(kind="test_passes", name="t")
    v1 = oracle.evaluate(prop=p, evidence={"exit_code": 0})
    v2 = oracle.evaluate(prop=p, evidence={"exit_code": 1})
    assert v1.evidence_hash != v2.evidence_hash


# ---------------------------------------------------------------------------
# §27 — Public API exposure
# ---------------------------------------------------------------------------


def test_public_api_via_package_init() -> None:
    from backend.core.ouroboros.governance import verification
    assert "Property" in verification.__all__
    assert "PropertyOracle" in verification.__all__
    assert "PropertyVerdict" in verification.__all__
    assert "VerdictKind" in verification.__all__
    assert "oracle_enabled" in verification.__all__
    assert "register_evaluator" in verification.__all__


def test_property_oracle_is_stateless() -> None:
    """Two Oracle instances dispatching the same property+evidence
    yield identical verdicts (modulo evaluation_ts_unix)."""
    o1 = PropertyOracle()
    o2 = PropertyOracle()
    p = Property.make(kind="test_passes", name="t")
    v1 = o1.evaluate(prop=p, evidence={"exit_code": 0})
    v2 = o2.evaluate(prop=p, evidence={"exit_code": 0})
    assert v1.verdict == v2.verdict
    assert v1.evidence_hash == v2.evidence_hash
    assert v1.confidence == v2.confidence
