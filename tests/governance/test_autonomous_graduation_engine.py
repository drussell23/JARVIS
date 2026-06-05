"""Slice 96 — Autonomous Telemetry-Driven Graduation Engine (TIERED).

The tiered-boundary proof: a STANDARD-tier flag that is telemetry-
eligible + AST-clean AUTO-FLIPS (via a durable env-override ledger +
a boot applier that injects into os.environ); a SAFETY-tier flag that
is identically ready is held to an APPROVAL_ADVISORY and STRUCTURALLY
cannot reach the override ledger or the boot applier.

All unit assertions use synthetic ledger + registry stubs — hermetic,
no dependence on live state. A final LIVE readout (Phase 4) runs the
engine against the REAL GraduationLedger + populated registry and
reports honestly which dormant flags graduated / advised / held.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.autonomous_graduation_engine import (
    GraduationDisposition,
    GraduationEngineReport,
    GraduationTier,
    HoldReason,
    autonomous_graduation_engine_enabled,
    evaluate_graduations,
    execute_graduations,
    register_flags,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance import (
    graduation_override_ledger as gol,
)
from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
)


# ---------------------------------------------------------------------------
# Synthetic stubs — hermetic, no live state
# ---------------------------------------------------------------------------


class _StubLedger:
    """Mimics GraduationLedger.eligible_flags()/progress()."""

    def __init__(self, eligible, progress_map=None):
        self._eligible = list(eligible)
        self._progress = progress_map or {}

    def eligible_flags(self):
        return list(self._eligible)

    def progress(self, flag_name):
        return self._progress.get(
            flag_name,
            {
                "clean": 5, "infra": 0, "runner": 0, "migration": 0,
                "unique_sessions": 5, "required": 3,
            },
        )


_STD_FLAG = "JARVIS_STUB_STANDARD_FLAG"
_SAFETY_FLAG = "JARVIS_STUB_SAFETY_FLAG"
_STD_SRC = "backend/core/ouroboros/governance/stub_standard.py"
_SAFETY_SRC = "backend/core/ouroboros/governance/stub_safety.py"


def _stub_registry():
    reg = FlagRegistry()
    reg.register(FlagSpec(
        name=_STD_FLAG, type=FlagType.BOOL, default=False,
        description="stub standard", category=Category.TUNING,
        source_file=_STD_SRC,
    ))
    reg.register(FlagSpec(
        name=_SAFETY_FLAG, type=FlagType.BOOL, default=False,
        description="stub safety", category=Category.SAFETY,
        source_file=_SAFETY_SRC,
    ))
    return reg


def _no_ast_drift(*_a, **_k):
    return ()


def _drift_on(source_file):
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        InvariantViolation,
    )

    def _validate():
        return (
            InvariantViolation(
                invariant_name="stub_drift",
                target_file=source_file,
                detail="synthetic drift",
            ),
        )

    return _validate


@pytest.fixture(autouse=True)
def _engine_on(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH",
        str(tmp_path / "graduation_overrides.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_GRADUATION_ADVISORY_LEDGER_PATH",
        str(tmp_path / "graduation_advisories.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Test 1 — STANDARD ready → AUTO_FLIP → receipt → environ injection
# ---------------------------------------------------------------------------


def test_standard_ready_auto_flips_and_applies(tmp_path):
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    assert isinstance(report, GraduationEngineReport)
    dec = {d.flag_name: d for d in report.decisions}[_STD_FLAG]
    assert dec.tier is GraduationTier.STANDARD
    assert dec.disposition is GraduationDisposition.AUTO_FLIP
    assert _STD_FLAG in report.auto_flipped
    assert dec.evidence_sha256

    result = execute_graduations(report)
    assert _STD_FLAG in result.recorded_overrides

    # Receipt landed in the override ledger.
    overrides = gol.all_overrides()
    assert any(o.flag_name == _STD_FLAG for o in overrides)

    # Boot applier injects into a fresh environ.
    env: dict = {}
    applied = gol.apply_overrides_to_environ(env)
    assert _STD_FLAG in applied
    assert env[_STD_FLAG] == "true"


# ---------------------------------------------------------------------------
# Test 2 — SAFETY ready → APPROVAL_ADVISORY (the core safety test)
# ---------------------------------------------------------------------------


def test_safety_ready_routes_to_advisory_never_override(tmp_path):
    report = evaluate_graduations(
        ledger=_StubLedger([_SAFETY_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    dec = {d.flag_name: d for d in report.decisions}[_SAFETY_FLAG]
    assert dec.tier is GraduationTier.SAFETY
    assert dec.disposition is GraduationDisposition.APPROVAL_ADVISORY
    assert _SAFETY_FLAG in report.advisories
    assert _SAFETY_FLAG not in report.auto_flipped

    result = execute_graduations(report)
    # Safety flag NEVER recorded as an override.
    assert _SAFETY_FLAG not in result.recorded_overrides
    assert _SAFETY_FLAG in result.advisories_emitted

    # Structurally absent from the override ledger.
    overrides = gol.all_overrides()
    assert all(o.flag_name != _SAFETY_FLAG for o in overrides)

    # Boot applier cannot set it.
    env: dict = {}
    applied = gol.apply_overrides_to_environ(env)
    assert _SAFETY_FLAG not in applied
    assert _SAFETY_FLAG not in env


def test_safety_flag_structurally_cannot_be_recorded():
    """record_graduation MUST refuse a SAFETY-tier decision even if
    a caller hands it one directly — structural, not convention."""
    from backend.core.ouroboros.governance.autonomous_graduation_engine import (  # noqa: E501
        GraduationDecision,
    )
    forged = GraduationDecision(
        flag_name=_SAFETY_FLAG,
        tier=GraduationTier.SAFETY,
        disposition=GraduationDisposition.AUTO_FLIP,  # forged disposition
        evidence={"forged": True},
        delta="ready",
        evidence_sha256="deadbeef",
    )
    ok = gol.record_graduation(forged)
    assert ok is False
    assert all(o.flag_name != _SAFETY_FLAG for o in gol.all_overrides())


# ---------------------------------------------------------------------------
# Test 3 — Hard-lock denials (mathematically absolute)
# ---------------------------------------------------------------------------


def test_not_eligible_holds_with_reason():
    # _STD_FLAG NOT in eligible set → HOLD NOT_ELIGIBLE.
    report = evaluate_graduations(
        ledger=_StubLedger([]),  # nothing eligible
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    # No candidates → no decisions for the stub flag at all.
    assert _STD_FLAG not in {d.flag_name for d in report.decisions}
    assert report.auto_flipped == ()


def test_eligible_but_ast_drift_holds_ast_drift():
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_drift_on(_STD_SRC),
    )
    dec = {d.flag_name: d for d in report.decisions}[_STD_FLAG]
    assert dec.disposition is GraduationDisposition.HOLD
    assert dec.evidence.get("hold_reason") == HoldReason.AST_DRIFT.value
    # delta names the exact failing gate.
    assert "AST" in dec.delta or "ast" in dec.delta
    assert _STD_FLAG in report.held
    assert _STD_FLAG not in report.auto_flipped


def test_ast_drift_on_other_file_does_not_block():
    # Drift on a DIFFERENT source_file must not hold this flag.
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_drift_on("some/unrelated/file.py"),
    )
    dec = {d.flag_name: d for d in report.decisions}[_STD_FLAG]
    assert dec.disposition is GraduationDisposition.AUTO_FLIP


# ---------------------------------------------------------------------------
# Test 4 — Boot applier: env-precedence + idempotent + STANDARD-only
# ---------------------------------------------------------------------------


def test_applier_respects_env_precedence():
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    execute_graduations(report)

    # Operator explicitly set it false — applier must NOT overwrite.
    env = {_STD_FLAG: "false"}
    applied = gol.apply_overrides_to_environ(env)
    assert _STD_FLAG not in applied
    assert env[_STD_FLAG] == "false"


def test_applier_idempotent():
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    execute_graduations(report)
    env: dict = {}
    first = gol.apply_overrides_to_environ(env)
    second = gol.apply_overrides_to_environ(env)
    assert _STD_FLAG in first
    # Second pass: already present → not re-applied.
    assert _STD_FLAG not in second
    assert env[_STD_FLAG] == "true"


def test_applier_gated_off_is_inert(monkeypatch):
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    execute_graduations(report)
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED", "false")
    monkeypatch.setenv(
        "JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "false",
    )
    env: dict = {}
    applied = gol.apply_overrides_to_environ(env)
    assert applied == ()
    assert env == {}


# ---------------------------------------------------------------------------
# Test 5 — Receipts: immutable JSONL round-trip with sha + authorized_by
# ---------------------------------------------------------------------------


def test_receipt_round_trips_with_evidence_and_sha():
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    execute_graduations(report)
    overrides = gol.all_overrides()
    rec = [o for o in overrides if o.flag_name == _STD_FLAG][0]
    assert rec.authorized_true is True
    assert rec.authorized_by == "autonomous_graduation_engine"
    assert rec.evidence_sha256
    assert isinstance(rec.evidence, dict)
    assert rec.tier == GraduationTier.STANDARD.value

    # On-disk row is valid JSON with the receipt fields.
    path = Path(os.environ["JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH"])
    rows = [
        json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()
    ]
    row = [r for r in rows if r["flag_name"] == _STD_FLAG][0]
    assert row["authorized_true"] is True
    assert row["evidence_sha256"]
    assert row["authorized_by"] == "autonomous_graduation_engine"


# ---------------------------------------------------------------------------
# Test 6 — never-raises / master-off / pins
# ---------------------------------------------------------------------------


def test_master_off_returns_disabled_report(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "false",
    )
    assert autonomous_graduation_engine_enabled() is False
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    assert report.auto_flipped == ()
    assert report.advisories == ()
    for d in report.decisions:
        assert d.disposition is GraduationDisposition.DISABLED


def test_broken_ledger_never_raises():
    class _Boom:
        def eligible_flags(self):
            raise RuntimeError("boom")

        def progress(self, _):
            raise RuntimeError("boom")

    report = evaluate_graduations(
        ledger=_Boom(),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    assert isinstance(report, GraduationEngineReport)
    assert report.auto_flipped == ()


def test_broken_registry_never_raises():
    class _BoomReg:
        def get_spec(self, _):
            raise RuntimeError("boom")

    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_BoomReg(),
        validate_all_fn=_no_ast_drift,
    )
    assert isinstance(report, GraduationEngineReport)
    # Unknown spec → held, never auto-flipped.
    assert _STD_FLAG not in report.auto_flipped


def test_record_graduation_never_raises_on_garbage():
    assert gol.record_graduation(None) is False
    assert gol.record_graduation("not a decision") is False


def test_report_to_dict_is_serializable():
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_no_ast_drift,
    )
    d = report.to_dict()
    json.dumps(d)  # must not raise
    assert d["schema_version"]


# ---------------------------------------------------------------------------
# Slice 96 review fixes — FAIL-CLOSED regression pins
# ---------------------------------------------------------------------------


class _SpecReg:
    """Registry stub returning ONE spec with a chosen category (incl.
    None / a garbage non-Category value) — for the fail-closed tier test."""

    def __init__(self, flag, category, source_file):
        self._flag = flag
        self._spec = SimpleNamespace(
            name=flag, type=FlagType.BOOL, default=False,
            description="stub", category=category, source_file=source_file,
        )

    def get_spec(self, name):
        return self._spec if name == self._flag else None


@pytest.mark.parametrize("bad_category", [None, "garbage_not_a_category",
                                          SimpleNamespace(value="totally_unknown")])
def test_unknown_category_fails_closed_to_safety(bad_category):
    """An eligible, AST-clean flag whose category is None / a forged /
    unregistered value MUST NOT auto-flip. Fail-CLOSED: unknown category
    → SAFETY tier → APPROVAL_ADVISORY, never the override ledger."""
    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_SpecReg(_STD_FLAG, bad_category, _STD_SRC),
        validate_all_fn=_no_ast_drift,
    )
    dec = {d.flag_name: d for d in report.decisions}[_STD_FLAG]
    assert dec.tier is GraduationTier.SAFETY, dec.evidence
    assert dec.disposition is GraduationDisposition.APPROVAL_ADVISORY
    assert _STD_FLAG not in report.auto_flipped
    # And it can never reach the override ledger.
    execute_graduations(report)
    assert all(o.flag_name != _STD_FLAG for o in gol.all_overrides())


def test_ast_validator_unavailable_holds_all_failclosed():
    """If shipped_code_invariants.validate_all() itself RAISES, Gate B
    cannot prove AST stability → every candidate HOLDs (fail-CLOSED).
    The prior empty-set swallow waved every flag through (fail-OPEN)."""
    def _boom_validate():
        raise RuntimeError("validator exploded")

    report = evaluate_graduations(
        ledger=_StubLedger([_STD_FLAG]),
        registry=_stub_registry(),
        validate_all_fn=_boom_validate,
    )
    dec = {d.flag_name: d for d in report.decisions}[_STD_FLAG]
    assert dec.disposition is GraduationDisposition.HOLD
    assert dec.evidence.get("hold_reason") == HoldReason.AST_DRIFT.value
    assert dec.evidence.get("gate_b_validator_unavailable") is True
    assert _STD_FLAG not in report.auto_flipped


# ---------------------------------------------------------------------------
# AST pins — canonical pass + synthetic regression per pin
# ---------------------------------------------------------------------------


def _engine_source():
    import backend.core.ouroboros.governance.autonomous_graduation_engine as m
    return Path(m.__file__).read_text(encoding="utf-8")


def _run_pins():
    pins = register_shipped_invariants()
    src = _engine_source()
    tree = ast.parse(src)
    out = {}
    for p in pins:
        out[p.invariant_name] = p.validate(tree, src)
    return out


def test_ast_pins_canonical_pass():
    results = _run_pins()
    assert results, "engine must register at least one shipped invariant"
    for name, violations in results.items():
        assert violations == (), f"{name} should pass on canonical: {violations}"


def test_pin_authority_asymmetry_regresses():
    pins = {p.invariant_name: p for p in register_shipped_invariants()}
    pin = [p for n, p in pins.items() if "authority" in n][0]
    bad = (
        "from backend.core.ouroboros.governance.orchestrator import X\n"
    )
    assert pin.validate(ast.parse(bad), bad) != ()


def test_pin_master_default_false_regresses():
    pins = {p.invariant_name: p for p in register_shipped_invariants()}
    pin = [p for n, p in pins.items() if "master" in n][0]
    bad = (
        "def autonomous_graduation_engine_enabled():\n"
        "    return True\n"
    )
    assert pin.validate(ast.parse(bad), bad) != ()


def test_pin_tier_enum_closed_regresses():
    pins = {p.invariant_name: p for p in register_shipped_invariants()}
    pin = [p for n, p in pins.items() if "tier" in n or "enum" in n][0]
    bad = (
        "class GraduationTier:\n"
        "    STANDARD = 'standard'\n"
        "    SAFETY = 'safety'\n"
        "    BACKDOOR = 'backdoor'\n"
    )
    assert pin.validate(ast.parse(bad), bad) != ()


def test_pin_composes_canonical_regresses():
    pins = {p.invariant_name: p for p in register_shipped_invariants()}
    pin = [p for n, p in pins.items() if "composes" in n][0]
    bad = "x = 1\n"  # references none of the canonical substrates
    assert pin.validate(ast.parse(bad), bad) != ()


# ---------------------------------------------------------------------------
# FlagRegistry seed integration
# ---------------------------------------------------------------------------


def test_register_flags_installs_master_and_apply_flags():
    reg = FlagRegistry()
    n = register_flags(reg)
    assert n >= 2
    assert reg.get_spec("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED")
    assert reg.get_spec("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED")
    # Master defaults FALSE per §33.1.
    spec = reg.get_spec("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED")
    assert spec.default is False


# ---------------------------------------------------------------------------
# Phase 4 — LIVE readout (first automated readout; honest, no auto-fix)
# ---------------------------------------------------------------------------


def test_live_readout(capsys):
    """Run the engine against the REAL ledger + populated registry and
    PRINT the decisions. This is the first automated graduation readout
    — reported honestly. Always passes (it's a readout, not a gate)."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        GraduationLedger,
    )
    from backend.core.ouroboros.governance.flag_registry import ensure_seeded

    live_ledger = GraduationLedger()
    live_registry = ensure_seeded()
    report = evaluate_graduations(
        ledger=live_ledger,
        registry=live_registry,
    )
    lines = ["", "=== Slice 96 LIVE GRADUATION READOUT ==="]
    lines.append(f"eligible candidates: {len(report.decisions)}")
    lines.append(f"auto_flipped (STANDARD): {list(report.auto_flipped)}")
    lines.append(f"advisories (SAFETY):     {list(report.advisories)}")
    for d in report.decisions:
        lines.append(
            f"  {d.flag_name} tier={d.tier.value} "
            f"disp={d.disposition.value} delta={d.delta!r}"
        )
    print("\n".join(lines))
    captured = capsys.readouterr()
    assert "LIVE GRADUATION READOUT" in captured.out
    # Structural sanity: every decision is a valid disposition.
    for d in report.decisions:
        assert d.disposition in set(GraduationDisposition)
