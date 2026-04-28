"""Phase 2 Slice 2.3 — property_capture regression spine.

Pins:
  §1   property_capture_enabled flag — default false; case-tolerant
  §2   PropertyClaim — frozen + .is_load_bearing helper
  §3   PropertyClaim — to_dict / from_dict round-trip
  §4   PropertyClaim — from_dict rejects bad input
  §5   Severity constants — three canonical values
  §6   Synthesizer — empty plan → empty claims
  §7   Synthesizer — None / non-Mapping plan → empty
  §8   Synthesizer — Rule 1: tests_to_pass → test_passes/must_hold
  §9   Synthesizer — Rule 1: skips malformed test entries
  §10  Synthesizer — Rule 2: regression risk → key_present/should_hold
  §11  Synthesizer — Rule 2: skips non-regression risks
  §12  Synthesizer — Rule 2: skips risks without mitigation
  §13  Synthesizer — Rule 3: tests_to_skip → key_present/ideal
  §14  Synthesizer — Rule 4: signature_invariants → string_matches/must_hold
  §15  Synthesizer — claim_id deterministic across calls (same op_id)
  §16  Synthesizer — claim_id differs across op_ids
  §17  Capture flag-off → no-op (returns 0, no disk traffic)
  §18  Capture happy path → records each claim, returns count
  §19  Capture defensive — disk fault → partial count, no raise
  §20  Capture rejects non-PropertyClaim entries
  §21  Reader — empty ledger → empty tuple
  §22  Reader — round-trip via capture_claims + get_recorded_claims
  §23  Reader — filter by op_id (multiple ops in same ledger)
  §24  Reader — skips corrupt JSONL rows
  §25  filter_load_bearing — keeps only must_hold
  §26  PLAN runner adapter registered at module load
  §27  PLAN runner adapter round-trip (PropertyClaim ↔ dict)
  §28  Authority invariants — no orchestrator/phase_runner/provider imports
  §29  Public API exposed from package __init__
  §30  Schema version pinned
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from backend.core.ouroboros.governance.verification import (
    CANONICAL_SEVERITIES,
    Property,
    PropertyClaim,
    SEVERITY_IDEAL,
    SEVERITY_MUST_HOLD,
    SEVERITY_SHOULD_HOLD,
    capture_claims,
    filter_load_bearing,
    get_recorded_claims,
    property_capture_enabled,
    synthesize_claims_from_plan,
)
from backend.core.ouroboros.governance.verification.property_capture import (
    PROPERTY_CLAIM_SCHEMA_VERSION,
    _derive_claim_id,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "test-session")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        reset_all_for_tests,
    )
    reset_all_for_tests()
    yield tmp_path / "det"
    reset_all_for_tests()


# ---------------------------------------------------------------------------
# §1 — Master flag
# ---------------------------------------------------------------------------


def test_capture_default_true(monkeypatch) -> None:
    """Phase 2 Slice 2.5 graduated default — env unset → True."""
    monkeypatch.delenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", raising=False,
    )
    assert property_capture_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  "])
def test_capture_empty_reads_as_default_true(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", val,
    )
    assert property_capture_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_capture_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", val,
    )
    assert property_capture_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_capture_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", val,
    )
    assert property_capture_enabled() is False


# ---------------------------------------------------------------------------
# §2-§4 — PropertyClaim schema
# ---------------------------------------------------------------------------


def test_claim_is_frozen() -> None:
    c = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
    )
    with pytest.raises(Exception):
        c.op_id = "different"  # type: ignore[misc]


def test_claim_is_load_bearing_helper() -> None:
    c_must = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
        severity=SEVERITY_MUST_HOLD,
    )
    c_should = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
        severity=SEVERITY_SHOULD_HOLD,
    )
    c_ideal = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
        severity=SEVERITY_IDEAL,
    )
    assert c_must.is_load_bearing is True
    assert c_should.is_load_bearing is False
    assert c_ideal.is_load_bearing is False


def test_claim_to_dict_from_dict_round_trip() -> None:
    original = PropertyClaim(
        op_id="op-42",
        claimed_at_phase="PLAN",
        property=Property.make(
            kind="test_passes",
            name="test_users_login",
            evidence_required=("exit_code",),
            metadata={"test_name": "test_users_login"},
        ),
        rationale="plan declared",
        severity=SEVERITY_MUST_HOLD,
        claim_id="claim-abc-123",
        ts_unix=1700000000.0,
    )
    d = original.to_dict()
    parsed = PropertyClaim.from_dict(d)
    assert parsed is not None
    assert parsed.op_id == "op-42"
    assert parsed.claimed_at_phase == "PLAN"
    assert parsed.property.kind == "test_passes"
    assert parsed.property.name == "test_users_login"
    assert parsed.property.evidence_required == ("exit_code",)
    assert parsed.severity == SEVERITY_MUST_HOLD
    assert parsed.claim_id == "claim-abc-123"


def test_claim_from_dict_rejects_garbage() -> None:
    assert PropertyClaim.from_dict("not a mapping") is None  # type: ignore[arg-type]
    assert PropertyClaim.from_dict({}) is None  # missing schema_version
    assert PropertyClaim.from_dict({
        "schema_version": "wrong.0",
    }) is None
    assert PropertyClaim.from_dict({
        "schema_version": PROPERTY_CLAIM_SCHEMA_VERSION,
        "property": "not a mapping",
    }) is None


def test_claim_from_dict_handles_missing_optional_fields() -> None:
    """from_dict provides safe defaults for missing optional fields."""
    minimal = {
        "schema_version": PROPERTY_CLAIM_SCHEMA_VERSION,
        "op_id": "op-1",
        "claimed_at_phase": "PLAN",
        "property": {
            "kind": "x", "name": "y",
            "evidence_required": [],
            "metadata": {},
        },
    }
    c = PropertyClaim.from_dict(minimal)
    assert c is not None
    assert c.severity == SEVERITY_SHOULD_HOLD  # default
    assert c.rationale == ""
    assert c.claim_id == ""
    assert c.ts_unix == 0.0


# ---------------------------------------------------------------------------
# §5 — Severity constants
# ---------------------------------------------------------------------------


def test_severity_constants_canonical() -> None:
    assert SEVERITY_MUST_HOLD == "must_hold"
    assert SEVERITY_SHOULD_HOLD == "should_hold"
    assert SEVERITY_IDEAL == "ideal"
    assert set(CANONICAL_SEVERITIES) == {
        "must_hold", "should_hold", "ideal",
    }


# ---------------------------------------------------------------------------
# §6-§7 — Synthesizer edge cases
# ---------------------------------------------------------------------------


def test_synthesize_empty_plan() -> None:
    assert synthesize_claims_from_plan({}, op_id="op-1") == ()


def test_synthesize_none_plan() -> None:
    assert synthesize_claims_from_plan(
        None, op_id="op-1",  # type: ignore[arg-type]
    ) == ()


def test_synthesize_non_mapping_plan() -> None:
    assert synthesize_claims_from_plan(
        "not a dict", op_id="op-1",  # type: ignore[arg-type]
    ) == ()
    assert synthesize_claims_from_plan(
        [], op_id="op-1",  # type: ignore[arg-type]
    ) == ()


def test_synthesize_empty_op_id() -> None:
    plan = {"test_strategy": {"tests_to_pass": ["test_x"]}}
    assert synthesize_claims_from_plan(plan, op_id="") == ()


# ---------------------------------------------------------------------------
# §8-§9 — Rule 1: tests_to_pass
# ---------------------------------------------------------------------------


def test_synthesize_tests_to_pass_basic() -> None:
    plan = {
        "test_strategy": {
            "tests_to_pass": [
                "test_users_login",
                "test_users_logout",
                "test_session_persistence",
            ],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-42")
    assert len(claims) == 3
    for c in claims:
        assert c.property.kind == "test_passes"
        assert c.severity == SEVERITY_MUST_HOLD
        assert c.claimed_at_phase == "PLAN"
        assert c.op_id == "op-42"
        assert c.property.evidence_required == ("exit_code",)


def test_synthesize_tests_to_pass_skips_malformed() -> None:
    plan = {
        "test_strategy": {
            "tests_to_pass": [
                "valid_test",
                "",  # empty string
                None,  # not a string
                123,  # not a string
                "   ",  # whitespace only
                "another_valid",
            ],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    # Only 2 valid claims
    assert len(claims) == 2
    names = [c.property.metadata_dict()["test_name"] for c in claims]
    assert names == ["valid_test", "another_valid"]


def test_synthesize_tests_to_pass_non_list() -> None:
    """tests_to_pass not a list → silently skipped."""
    plan = {"test_strategy": {"tests_to_pass": "not a list"}}
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert claims == ()


# ---------------------------------------------------------------------------
# §10-§12 — Rule 2: regression risk
# ---------------------------------------------------------------------------


def test_synthesize_regression_risk() -> None:
    plan = {
        "risk_factors": [
            {
                "type": "regression",
                "description": "Auth bypass on /api/v2",
                "mitigation": "Added auth middleware test",
            },
        ],
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 1
    c = claims[0]
    assert c.property.kind == "key_present"
    assert c.severity == SEVERITY_SHOULD_HOLD
    assert "Auth bypass" in c.property.name


def test_synthesize_skips_non_regression_risks() -> None:
    plan = {
        "risk_factors": [
            {"type": "performance", "mitigation": "x"},
            {"type": "security", "mitigation": "y"},
            {"type": "regression", "mitigation": "z"},  # only this one
        ],
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 1


def test_synthesize_skips_regression_without_mitigation() -> None:
    plan = {
        "risk_factors": [
            {"type": "regression", "description": "x"},  # no mitigation
            {"type": "regression", "description": "y", "mitigation": ""},
        ],
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert claims == ()


# ---------------------------------------------------------------------------
# §13 — Rule 3: tests_to_skip
# ---------------------------------------------------------------------------


def test_synthesize_tests_to_skip() -> None:
    plan = {
        "test_strategy": {
            "tests_to_skip": ["test_legacy_path"],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 1
    c = claims[0]
    assert c.property.kind == "key_present"
    assert c.severity == SEVERITY_IDEAL
    assert "test_legacy_path" in c.property.name


# ---------------------------------------------------------------------------
# §14 — Rule 4: signature_invariants
# ---------------------------------------------------------------------------


def test_synthesize_signature_invariants() -> None:
    plan = {
        "approach": {
            "signature_invariants": [
                {"function": "auth.verify_token", "signature": "(token: str) -> bool"},
                {"function": "auth.refresh_token", "signature": "(token: str) -> str"},
            ],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 2
    for c in claims:
        assert c.property.kind == "string_matches"
        assert c.severity == SEVERITY_MUST_HOLD
        assert c.property.evidence_required == ("actual", "expected")


def test_synthesize_signature_invariants_skip_incomplete() -> None:
    plan = {
        "approach": {
            "signature_invariants": [
                {"function": "x", "signature": ""},  # empty sig
                {"function": "", "signature": "y"},  # empty fn
                "not a dict",
                {"function": "valid", "signature": "(x) -> y"},
            ],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 1


# ---------------------------------------------------------------------------
# §15-§16 — Deterministic claim_id
# ---------------------------------------------------------------------------


def test_claim_id_deterministic_same_op() -> None:
    """Same (op_id, claim_index) → same claim_id within a session."""
    cid1 = _derive_claim_id("op-1", 0)
    cid2 = _derive_claim_id("op-1", 0)
    assert cid1 == cid2


def test_claim_id_differs_across_index() -> None:
    cid_0 = _derive_claim_id("op-1", 0)
    cid_1 = _derive_claim_id("op-1", 1)
    assert cid_0 != cid_1


def test_claim_id_differs_across_op_ids() -> None:
    cid_a = _derive_claim_id("op-1", 0)
    cid_b = _derive_claim_id("op-2", 0)
    assert cid_a != cid_b


# ---------------------------------------------------------------------------
# §17-§20 — Capture API
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_flag_off_returns_zero(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_PROPERTY_CAPTURE_ENABLED", "false",
    )
    claim = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
    )
    count = await capture_claims(op_id="op-1", claims=[claim])
    assert count == 0


@pytest.mark.asyncio
async def test_capture_empty_claims_returns_zero(isolated) -> None:
    count = await capture_claims(op_id="op-1", claims=[])
    assert count == 0


@pytest.mark.asyncio
async def test_capture_records_each_claim(isolated) -> None:
    """Happy path: each claim becomes one ledger record."""
    claims = [
        PropertyClaim(
            op_id="op-1", claimed_at_phase="PLAN",
            property=Property.make(kind="test_passes", name=f"t{i}"),
            severity=SEVERITY_MUST_HOLD,
            claim_id=f"claim-{i}",
        )
        for i in range(3)
    ]
    count = await capture_claims(op_id="op-1", claims=claims)
    assert count == 3
    # Read back via the reader
    recovered = get_recorded_claims(op_id="op-1")
    assert len(recovered) == 3


@pytest.mark.asyncio
async def test_capture_rejects_non_claim_entries(isolated) -> None:
    """Garbage entries are silently skipped."""
    claim = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="y"),
    )
    mixed = [
        claim,
        "not a claim",  # type: ignore[list-item]
        None,  # type: ignore[list-item]
        42,  # type: ignore[list-item]
        claim,
    ]
    count = await capture_claims(op_id="op-1", claims=mixed)
    assert count == 2  # only the two real claims


# ---------------------------------------------------------------------------
# §21-§24 — Reader API
# ---------------------------------------------------------------------------


def test_reader_empty_ledger(isolated) -> None:
    """No ledger file → empty tuple."""
    claims = get_recorded_claims(op_id="op-1")
    assert claims == ()


def test_reader_empty_op_id(isolated) -> None:
    claims = get_recorded_claims(op_id="")
    assert claims == ()


@pytest.mark.asyncio
async def test_reader_round_trip(isolated) -> None:
    original = PropertyClaim(
        op_id="op-42",
        claimed_at_phase="PLAN",
        property=Property.make(
            kind="test_passes", name="t1",
            evidence_required=("exit_code",),
            metadata={"test_name": "t1"},
        ),
        rationale="plan declared",
        severity=SEVERITY_MUST_HOLD,
        claim_id="claim-x",
    )
    await capture_claims(op_id="op-42", claims=[original])
    recovered = get_recorded_claims(op_id="op-42")
    assert len(recovered) == 1
    rc = recovered[0]
    assert rc.op_id == "op-42"
    assert rc.property.kind == "test_passes"
    assert rc.property.name == "t1"
    assert rc.severity == SEVERITY_MUST_HOLD


@pytest.mark.asyncio
async def test_reader_filters_by_op_id(isolated) -> None:
    """Multiple ops in same ledger — reader filters cleanly."""
    c1 = PropertyClaim(
        op_id="op-A", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="a"),
    )
    c2 = PropertyClaim(
        op_id="op-B", claimed_at_phase="PLAN",
        property=Property.make(kind="x", name="b"),
    )
    await capture_claims(op_id="op-A", claims=[c1])
    await capture_claims(op_id="op-B", claims=[c2])

    a_claims = get_recorded_claims(op_id="op-A")
    b_claims = get_recorded_claims(op_id="op-B")
    assert len(a_claims) == 1
    assert len(b_claims) == 1
    assert a_claims[0].property.name == "a"
    assert b_claims[0].property.name == "b"


def test_reader_skips_corrupt_jsonl(isolated, monkeypatch) -> None:
    """Corrupt rows in the JSONL → silently skipped."""
    # Manually write a ledger with mixed valid/corrupt rows
    ledger_path = isolated / "test-session" / "decisions.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    valid_record = json.dumps({
        "schema_version": "decision_record.1",
        "record_id": "rec-0",
        "session_id": "test-session",
        "op_id": "op-1",
        "phase": "PLAN",
        "kind": "property_claim",
        "ordinal": 0,
        "inputs_hash": "h",
        "output_repr": json.dumps({
            "schema_version": PROPERTY_CLAIM_SCHEMA_VERSION,
            "op_id": "op-1",
            "claimed_at_phase": "PLAN",
            "property": {
                "kind": "x", "name": "y",
                "evidence_required": [], "metadata": {},
            },
            "rationale": "", "severity": "should_hold",
            "claim_id": "c-0", "ts_unix": 0,
        }),
        "monotonic_ts": 1.0, "wall_ts": 2.0,
    })
    rows = [
        valid_record,
        "{not valid json",
        json.dumps({"schema_version": "wrong.0"}),  # filtered out
        valid_record,
        "",  # blank line
    ]
    ledger_path.write_text("\n".join(rows) + "\n")

    claims = get_recorded_claims(op_id="op-1")
    # Only the two valid records should parse
    assert len(claims) == 2


# ---------------------------------------------------------------------------
# §25 — filter_load_bearing
# ---------------------------------------------------------------------------


def test_filter_load_bearing_keeps_must_hold() -> None:
    claims = [
        PropertyClaim(
            op_id="op-1", claimed_at_phase="PLAN",
            property=Property.make(kind="x", name=f"c{i}"),
            severity=sev,
        )
        for i, sev in enumerate([
            SEVERITY_MUST_HOLD, SEVERITY_SHOULD_HOLD,
            SEVERITY_IDEAL, SEVERITY_MUST_HOLD,
        ])
    ]
    filtered = filter_load_bearing(claims)
    assert len(filtered) == 2
    assert all(c.severity == SEVERITY_MUST_HOLD for c in filtered)


def test_filter_load_bearing_empty() -> None:
    assert filter_load_bearing([]) == ()


# ---------------------------------------------------------------------------
# §26-§27 — Adapter integration
# ---------------------------------------------------------------------------


def test_property_claim_adapter_registered() -> None:
    """The (PLAN, property_claim) adapter is registered at module
    load. Tests that import property_capture get the adapter for
    free."""
    from backend.core.ouroboros.governance.determinism.phase_capture import (
        get_adapter,
        _IDENTITY_ADAPTER,
    )
    # Force the adapter registration by importing
    import backend.core.ouroboros.governance.verification.property_capture  # noqa
    adapter = get_adapter(phase="PLAN", kind="property_claim")
    assert adapter is not _IDENTITY_ADAPTER
    assert adapter.name == "property_claim_adapter"


def test_property_claim_adapter_round_trip() -> None:
    from backend.core.ouroboros.governance.determinism.phase_capture import (
        get_adapter,
    )
    import backend.core.ouroboros.governance.verification.property_capture  # noqa
    adapter = get_adapter(phase="PLAN", kind="property_claim")

    original = PropertyClaim(
        op_id="op-1", claimed_at_phase="PLAN",
        property=Property.make(
            kind="test_passes", name="t1",
            evidence_required=("exit_code",),
        ),
        severity=SEVERITY_MUST_HOLD,
    )
    serialized = adapter.serialize(original.to_dict())
    assert isinstance(serialized, dict)
    deserialized = adapter.deserialize(serialized)
    assert isinstance(deserialized, PropertyClaim)
    assert deserialized.property.kind == "test_passes"


# ---------------------------------------------------------------------------
# §28 — Authority invariants
# ---------------------------------------------------------------------------


def test_no_orchestrator_imports() -> None:
    import ast
    import inspect
    from backend.core.ouroboros.governance.verification import (
        property_capture,
    )
    tree = ast.parse(inspect.getsource(property_capture))
    forbidden_modules = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.candidate_generator",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for f in forbidden_modules:
                assert f != node.module, (
                    f"property_capture must NOT import from {f}"
                )


def test_no_provider_imports() -> None:
    import inspect
    from backend.core.ouroboros.governance.verification import (
        property_capture,
    )
    src = inspect.getsource(property_capture)
    assert "doubleword_provider" not in src
    assert "claude_provider" not in src.lower()


# ---------------------------------------------------------------------------
# §29-§30 — Public API + schema version
# ---------------------------------------------------------------------------


def test_public_api_via_package_init() -> None:
    from backend.core.ouroboros.governance import verification
    assert "PropertyClaim" in verification.__all__
    assert "synthesize_claims_from_plan" in verification.__all__
    assert "capture_claims" in verification.__all__
    assert "get_recorded_claims" in verification.__all__
    assert "filter_load_bearing" in verification.__all__
    assert "property_capture_enabled" in verification.__all__
    assert "SEVERITY_MUST_HOLD" in verification.__all__


def test_schema_version_pinned() -> None:
    assert PROPERTY_CLAIM_SCHEMA_VERSION == "property_claim.1"


# ---------------------------------------------------------------------------
# §31 — Plan runner wiring source-level pin
# ---------------------------------------------------------------------------


def test_plan_runner_wiring_present() -> None:
    """The PLAN runner's success path includes the property_capture
    integration. Source-level pin so a refactor that strips the
    wiring fails this test."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
        encoding="utf-8",
    ).read()
    assert "Slice 2.3" in src, (
        "plan_runner must reference Phase 2 Slice 2.3"
    )
    assert "synthesize_claims_from_plan" in src
    assert "capture_claims" in src
    # Wiring is BEFORE the success-path return
    capture_idx = src.index("synthesize_claims_from_plan")
    return_idx = src.index('reason="planned"')
    assert capture_idx < return_idx


def test_plan_runner_imports_capture_lazily() -> None:
    """The plan_runner imports property_capture LAZILY (inside
    function body), not at module top level."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
        encoding="utf-8",
    ).read()
    lines = src.split("\n")
    top_level = [
        ln for ln in lines
        if ln.startswith(
            "from backend.core.ouroboros.governance.verification.property_capture"
        )
    ]
    assert top_level == []


def test_plan_runner_capture_has_try_except() -> None:
    """Defensive try/except wraps the capture call in plan_runner —
    capture failure must not break PLAN."""
    src = open(
        "backend/core/ouroboros/governance/phase_runners/plan_runner.py",
        encoding="utf-8",
    ).read()
    capture_idx = src.index("synthesize_claims_from_plan")
    preceding = src[max(0, capture_idx - 1000):capture_idx]
    try_idx = preceding.rfind("try:")
    assert try_idx != -1, "capture call must be inside try/except"
    following = src[capture_idx:capture_idx + 2000]
    assert "except Exception" in following


# ---------------------------------------------------------------------------
# §32 — Synthesizer composition: full-spectrum plan
# ---------------------------------------------------------------------------


def test_synthesize_full_spectrum_plan() -> None:
    """Plan with all 4 rule sources → claims of all 4 kinds + severities."""
    plan = {
        "approach": {
            "signature_invariants": [
                {"function": "f", "signature": "(x: int) -> str"},
            ],
        },
        "test_strategy": {
            "tests_to_pass": ["test_must_pass_1", "test_must_pass_2"],
            "tests_to_skip": ["test_skip_1"],
        },
        "risk_factors": [
            {"type": "regression", "description": "API drift",
             "mitigation": "added compat layer test"},
            {"type": "performance", "mitigation": "perf bench"},  # ignored
        ],
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-multi")
    # 1 sig_invariant + 2 tests_to_pass + 1 tests_to_skip + 1 regression
    # = 5 claims total
    assert len(claims) == 5

    by_severity = {}
    for c in claims:
        by_severity.setdefault(c.severity, []).append(c)

    assert len(by_severity[SEVERITY_MUST_HOLD]) == 3  # sig + 2 tests
    assert len(by_severity[SEVERITY_SHOULD_HOLD]) == 1  # regression
    assert len(by_severity[SEVERITY_IDEAL]) == 1  # tests_to_skip


def test_synthesize_no_duplicate_claim_ids() -> None:
    """Within one synthesis, all claim_ids are unique."""
    plan = {
        "test_strategy": {
            "tests_to_pass": [f"test_{i}" for i in range(20)],
        },
    }
    claims = synthesize_claims_from_plan(plan, op_id="op-1")
    assert len(claims) == 20
    ids = {c.claim_id for c in claims}
    assert len(ids) == 20  # all unique
