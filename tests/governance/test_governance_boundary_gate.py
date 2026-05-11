"""Regression spine for §40 Wave 2 #5 — RRD §1 Boundary
recursion-depth gate.

Covers:

* §33.1 safety-gate canonical shape — master flag default-**TRUE**
* Closed 4-value :class:`BoundaryVerdict` taxonomy
* Pure-function :func:`evaluate_target_files` across every reachable
  verdict + defensive on malformed inputs
* Canonical governance prefix derived structurally from
  ``__file__`` — no hardcoded path literal
* Composition into :func:`risk_tier_floor.recommended_floor` /
  :func:`apply_floor_to_name` / :func:`floor_reason` via
  the additive ``target_files`` kwarg (backward-compat preserved)
* Orchestrator GATE call site forwards ``ctx.target_files``
* 4 AST pin canonical-source pass + 4 synthetic regressions
* FlagRegistry seed auto-discovered
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    governance_boundary_gate as gbg,
)
from backend.core.ouroboros.governance.governance_boundary_gate import (
    BoundaryReport,
    BoundaryVerdict,
    GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
    _ENV_MASTER,
    _is_within_governance,
    _normalize_path,
    canonical_governance_prefix,
    evaluate_target_files,
    is_boundary_crossed,
    master_enabled,
    reset_for_tests,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv(_ENV_MASTER, raising=False)
    reset_for_tests()
    yield
    reset_for_tests()


# ---------------------------------------------------------------------------
# §33.1 safety-gate canonical shape — default-TRUE
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_true(self):
        """Safety-gate canonical shape — mirrors
        JARVIS_ASCII_GATE / JARVIS_SEMANTIC_GUARD_ENABLED."""
        assert master_enabled() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "FALSE"],
    )
    def test_explicit_false_turns_off(self, monkeypatch, falsy):
        monkeypatch.setenv(_ENV_MASTER, falsy)
        assert master_enabled() is False

    @pytest.mark.parametrize(
        "truthy", ["1", "true", "yes", "on", "TRUE"],
    )
    def test_explicit_true_keeps_on(self, monkeypatch, truthy):
        monkeypatch.setenv(_ENV_MASTER, truthy)
        assert master_enabled() is True

    def test_empty_keeps_default_true(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "   ")
        # Whitespace-only → unset → default-TRUE
        assert master_enabled() is True


# ---------------------------------------------------------------------------
# Canonical governance prefix (no hardcoding — derived from __file__)
# ---------------------------------------------------------------------------


class TestCanonicalPrefix:
    def test_prefix_matches_expected(self):
        prefix = canonical_governance_prefix()
        assert prefix == "backend/core/ouroboros/governance/"

    def test_prefix_ends_with_slash(self):
        assert canonical_governance_prefix().endswith("/")

    def test_prefix_derived_structurally(self):
        """The prefix MUST point to the directory where THIS
        module lives — operator binding 'no hardcoding' enforced
        via the AST pin. Here we cross-check that the string we
        return actually matches the on-disk structural truth.
        """
        canonical = Path(gbg.__file__).resolve().parent
        # Find repo root via .git
        repo_root = None
        for ancestor in (canonical, *canonical.parents):
            if (ancestor / ".git").exists():
                repo_root = ancestor
                break
        assert repo_root is not None
        rel = str(canonical.relative_to(repo_root)).replace("\\", "/")
        if not rel.endswith("/"):
            rel = rel + "/"
        assert canonical_governance_prefix() == rel


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class TestVerdictTaxonomy:
    def test_exactly_4_values(self):
        values = {v.value for v in BoundaryVerdict}
        assert values == {
            "boundary_crossed",
            "within_limits",
            "empty_target",
            "disabled",
        }


# ---------------------------------------------------------------------------
# §33.5 frozen report
# ---------------------------------------------------------------------------


class TestReportArtifact:
    def test_to_dict_shape(self):
        r = BoundaryReport(
            schema_version=GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION,
            verdict=BoundaryVerdict.BOUNDARY_CROSSED,
            crossing_paths=("a", "b"),
            total_targets=3,
            canonical_prefix="backend/core/ouroboros/governance/",
            detail="d",
        )
        d = r.to_dict()
        assert set(d.keys()) == {
            "schema_version", "verdict",
            "crossing_paths", "total_targets",
            "canonical_prefix", "detail",
        }
        assert d["verdict"] == "boundary_crossed"

    def test_report_is_frozen(self):
        r = BoundaryReport(
            schema_version="x",
            verdict=BoundaryVerdict.EMPTY_TARGET,
            crossing_paths=(),
            total_targets=0,
            canonical_prefix="p",
            detail="d",
        )
        with pytest.raises(Exception):
            r.total_targets = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Path normalization
# ---------------------------------------------------------------------------


class TestPathNormalization:
    def test_string_passthrough(self):
        assert _normalize_path("a/b/c.py") == "a/b/c.py"

    def test_backslashes_converted(self):
        assert _normalize_path("a\\b\\c.py") == "a/b/c.py"

    def test_bytes_decoded(self):
        assert _normalize_path(b"hello.py") == "hello.py"

    def test_path_object_coerced(self):
        assert _normalize_path(Path("a/b.py")) == "a/b.py"

    def test_none_returns_empty(self):
        assert _normalize_path(None) == ""

    def test_strips_whitespace(self):
        assert _normalize_path("  a/b.py  ") == "a/b.py"

    def test_absolute_path_relativized(self):
        # Build an absolute path INTO the canonical governance dir
        gov_dir = Path(gbg.__file__).resolve().parent
        abs_path = str(gov_dir / "iron_gate.py")
        result = _normalize_path(abs_path)
        # Should be repo-relative now
        assert "backend/core/ouroboros/governance/" in result
        assert "iron_gate.py" in result


# ---------------------------------------------------------------------------
# Boundary membership check
# ---------------------------------------------------------------------------


class TestIsWithinGovernance:
    def test_cage_path_detected(self):
        assert _is_within_governance(
            "backend/core/ouroboros/governance/orchestrator.py",
        )

    def test_cage_subdirectory(self):
        assert _is_within_governance(
            "backend/core/ouroboros/governance/m10/primitives.py",
        )

    def test_lookalike_not_detected(self):
        """Path-string lookalikes that aren't actually the cage
        must NOT cross the boundary."""
        assert not _is_within_governance(
            "backend/core/ouroboros/governance_external/foo.py",
        )
        assert not _is_within_governance(
            "backend/core/governance_other/bar.py",
        )

    def test_downstream_files_not_detected(self):
        for p in [
            "frontend/app.tsx",
            "tests/test_foo.py",
            "backend/core/ouroboros/battle_test/harness.py",
            "backend/core/ouroboros/consciousness/types.py",
            "scripts/run.py",
        ]:
            assert not _is_within_governance(p), p

    def test_empty_not_detected(self):
        assert not _is_within_governance("")

    def test_dot_slash_prefix_stripped(self):
        assert _is_within_governance(
            "./backend/core/ouroboros/governance/policy.py",
        )


# ---------------------------------------------------------------------------
# evaluate_target_files — every verdict reachable
# ---------------------------------------------------------------------------


class TestEvaluateTargetFiles:
    def test_empty_list_returns_empty_target(self):
        r = evaluate_target_files([])
        assert r.verdict is BoundaryVerdict.EMPTY_TARGET
        assert r.total_targets == 0

    def test_none_returns_empty_target(self):
        r = evaluate_target_files(None)
        assert r.verdict is BoundaryVerdict.EMPTY_TARGET

    def test_downstream_only_within_limits(self):
        r = evaluate_target_files([
            "frontend/app.tsx",
            "tests/test_foo.py",
            "scripts/run.py",
        ])
        assert r.verdict is BoundaryVerdict.WITHIN_LIMITS
        assert r.total_targets == 3
        assert len(r.crossing_paths) == 0

    def test_single_cage_file_boundary_crossed(self):
        r = evaluate_target_files([
            "backend/core/ouroboros/governance/orchestrator.py",
        ])
        assert r.verdict is BoundaryVerdict.BOUNDARY_CROSSED
        assert len(r.crossing_paths) == 1
        assert "approval_required" in r.detail.lower() or (
            "cage" in r.detail.lower()
            or "approval" in r.detail.lower()
        )

    def test_mixed_paths_boundary_crossed(self):
        r = evaluate_target_files([
            "frontend/app.tsx",
            "backend/core/ouroboros/governance/iron_gate.py",
            "tests/test_x.py",
        ])
        assert r.verdict is BoundaryVerdict.BOUNDARY_CROSSED
        assert len(r.crossing_paths) == 1
        assert r.total_targets == 3

    def test_master_off_returns_disabled(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "false")
        r = evaluate_target_files([
            "backend/core/ouroboros/governance/orchestrator.py",
        ])
        assert r.verdict is BoundaryVerdict.DISABLED
        # Operator-override discipline: when off, no crossing
        # paths recorded (gate is structurally disengaged)
        assert len(r.crossing_paths) == 0

    def test_absolute_paths_normalized(self):
        gov_dir = Path(gbg.__file__).resolve().parent
        abs_path = str(gov_dir / "policy.py")
        r = evaluate_target_files([abs_path])
        assert r.verdict is BoundaryVerdict.BOUNDARY_CROSSED

    def test_crossing_paths_bounded(self):
        """Pathological huge list shouldn't bloat audit records."""
        many = [
            f"backend/core/ouroboros/governance/file_{i}.py"
            for i in range(100)
        ]
        r = evaluate_target_files(many)
        assert r.verdict is BoundaryVerdict.BOUNDARY_CROSSED
        # Bounded at 32
        assert len(r.crossing_paths) == 32
        assert r.total_targets == 100

    def test_lookalike_not_crossed(self):
        r = evaluate_target_files([
            "backend/core/ouroboros/governance_external/foo.py",
            "backend/core/governance_other/bar.py",
        ])
        assert r.verdict is BoundaryVerdict.WITHIN_LIMITS

    def test_malformed_inputs_skipped(self):
        r = evaluate_target_files([None, "", "   ", 42])
        # Each input normalized; non-None values pass through;
        # malformed entries don't break the predicate.
        assert r.verdict in (
            BoundaryVerdict.EMPTY_TARGET,
            BoundaryVerdict.WITHIN_LIMITS,
        )


class TestConveniencePredicate:
    def test_returns_true_when_crossed(self):
        assert is_boundary_crossed([
            "backend/core/ouroboros/governance/iron_gate.py",
        ])

    def test_returns_false_when_within_limits(self):
        assert not is_boundary_crossed(["frontend/app.tsx"])

    def test_returns_false_when_empty(self):
        assert not is_boundary_crossed([])
        assert not is_boundary_crossed(None)

    def test_returns_false_when_master_off(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "false")
        # Master off → DISABLED verdict → predicate returns False
        # so the gate doesn't accidentally block ops in rollback.
        assert not is_boundary_crossed([
            "backend/core/ouroboros/governance/iron_gate.py",
        ])


# ---------------------------------------------------------------------------
# Composition into risk_tier_floor
# ---------------------------------------------------------------------------


class TestRiskTierFloorComposition:
    """The boundary gate composes into the canonical
    risk_tier_floor strictest-wins ladder via additive
    ``target_files`` kwarg. Backward-compat preserved."""

    def test_no_target_files_no_change(self):
        """Legacy call (no target_files) MUST be byte-equivalent
        to pre-Wave-2-#5 behavior."""
        from backend.core.ouroboros.governance.risk_tier_floor import (
            recommended_floor,
        )
        # No env / paranoia / quiet hours / vision / op_id → None
        assert recommended_floor() is None

    def test_target_files_cage_forces_approval(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            recommended_floor,
        )
        floor = recommended_floor(
            target_files=[
                "backend/core/ouroboros/governance/policy.py",
            ],
        )
        assert floor == "approval_required"

    def test_target_files_downstream_no_change(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            recommended_floor,
        )
        floor = recommended_floor(
            target_files=["frontend/app.tsx", "tests/foo.py"],
        )
        assert floor is None

    def test_apply_floor_to_name_safe_auto_upgraded(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            apply_floor_to_name,
        )
        effective, applied = apply_floor_to_name(
            "safe_auto",
            target_files=[
                "backend/core/ouroboros/governance/orchestrator.py",
            ],
        )
        assert effective == "approval_required"
        assert applied == "approval_required"

    def test_apply_floor_to_name_notify_apply_upgraded(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            apply_floor_to_name,
        )
        effective, applied = apply_floor_to_name(
            "notify_apply",
            target_files=[
                "backend/core/ouroboros/governance/iron_gate.py",
            ],
        )
        assert effective == "approval_required"
        assert applied == "approval_required"

    def test_apply_floor_to_name_already_at_floor_no_op(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            apply_floor_to_name,
        )
        effective, applied = apply_floor_to_name(
            "approval_required",
            target_files=[
                "backend/core/ouroboros/governance/iron_gate.py",
            ],
        )
        assert effective == "approval_required"
        assert applied is None  # no upgrade — was already at floor

    def test_apply_floor_to_name_blocked_passes_through(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            apply_floor_to_name,
        )
        effective, applied = apply_floor_to_name(
            "blocked",
            target_files=[
                "backend/core/ouroboros/governance/iron_gate.py",
            ],
        )
        # Stricter than approval_required already — no change
        assert effective == "blocked"
        assert applied is None

    def test_master_off_target_files_no_effect(self, monkeypatch):
        monkeypatch.setenv(_ENV_MASTER, "false")
        from backend.core.ouroboros.governance.risk_tier_floor import (
            recommended_floor,
        )
        floor = recommended_floor(
            target_files=[
                "backend/core/ouroboros/governance/iron_gate.py",
            ],
        )
        # Gate disabled → no boundary contribution → other
        # signals decide; here there are none, so None.
        assert floor is None

    def test_floor_reason_diagnostic_includes_boundary(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            floor_reason,
        )
        reason = floor_reason(target_files=[
            "backend/core/ouroboros/governance/policy.py",
        ])
        assert "governance_boundary_crossed" in reason
        assert "approval_required" in reason
        assert "RRD §1" in reason

    def test_floor_reason_no_target_files_clean(self):
        from backend.core.ouroboros.governance.risk_tier_floor import (
            floor_reason,
        )
        reason = floor_reason()
        assert reason == "(no floor active)"


# ---------------------------------------------------------------------------
# AST pins — canonical-source pass + synthetic regressions
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_source():
    path = Path(gbg.__file__)
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    return src, tree


@pytest.fixture
def pins():
    return gbg.register_shipped_invariants()


class TestAstPinsCanonicalPass:
    def test_4_pins_registered(self, pins):
        assert len(pins) == 4
        names = {p.invariant_name for p in pins}
        assert names == {
            "governance_boundary_verdict_taxonomy_closed",
            "governance_boundary_authority_asymmetry",
            "governance_boundary_master_default_true",
            "governance_boundary_no_hardcoded_prefix",
        }

    def test_verdict_taxonomy_passes(self, canonical_source, pins):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_verdict_taxonomy_closed"
        )
        assert not pin.validate(tree, src)

    def test_authority_asymmetry_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_authority_asymmetry"
        )
        assert not pin.validate(tree, src)

    def test_master_default_true_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_master_default_true"
        )
        assert not pin.validate(tree, src)

    def test_no_hardcoded_prefix_passes(
        self, canonical_source, pins,
    ):
        src, tree = canonical_source
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_no_hardcoded_prefix"
        )
        assert not pin.validate(tree, src)


class TestAstPinsSyntheticRegression:
    def test_verdict_pin_fires_on_missing(self, pins):
        synthetic = """
import enum
class BoundaryVerdict(str, enum.Enum):
    BOUNDARY_CROSSED = "boundary_crossed"
    WITHIN_LIMITS = "within_limits"
    EMPTY_TARGET = "empty_target"
    # MISSING: DISABLED
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_verdict_taxonomy_closed"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "missing" in violations[0]

    def test_authority_pin_fires_on_forbidden_import(self, pins):
        synthetic = (
            "from backend.core.ouroboros.governance.risk_tier_floor "
            "import recommended_floor\n"
        )
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_authority_asymmetry"
        )
        violations = pin.validate(tree, synthetic)
        assert violations
        assert "risk_tier_floor" in violations[0]

    def test_master_pin_fires_on_default_false(self, pins):
        # Safety-gate canonical shape is default-TRUE. Synthetic
        # with default-False MUST fire the pin.
        synthetic = """
def master_enabled():
    return _flag("FOO", default=False)
"""
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_master_default_true"
        )
        violations = pin.validate(tree, synthetic)
        assert violations

    def test_no_hardcoded_prefix_pin_fires(self, pins):
        # Synthetic with NO reference to Path(__file__).
        synthetic = "_GOVERNANCE = 'backend/foo/'\n"
        tree = ast.parse(synthetic)
        pin = next(
            p for p in pins
            if p.invariant_name
            == "governance_boundary_no_hardcoded_prefix"
        )
        violations = pin.validate(tree, synthetic)
        assert violations


# ---------------------------------------------------------------------------
# FlagRegistry seed auto-discovered
# ---------------------------------------------------------------------------


class TestFlagRegistrySeed:
    def test_seed_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        assert _ENV_MASTER in names

    def test_seed_safety_category_default_true(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all() if f.name == _ENV_MASTER
        )
        assert spec.default is True
        assert spec.category.value == "safety"


# ---------------------------------------------------------------------------
# Orchestrator integration call site
# ---------------------------------------------------------------------------


class TestOrchestratorIntegration:
    """The orchestrator's GATE phase MUST pass ctx.target_files
    to apply_floor_to_name + floor_reason. We pin this via
    a structural source check — if the integration regresses,
    the boundary gate becomes a no-op in production."""

    def test_orchestrator_passes_target_files(self):
        from backend.core.ouroboros.governance import orchestrator
        src = Path(orchestrator.__file__).read_text(encoding="utf-8")
        # apply_floor_to_name receives target_files kwarg
        assert "target_files=_target_files" in src
        # floor_reason receives target_files kwarg
        assert "target_files=_target_files," in src

    def test_orchestrator_resolves_target_files(self):
        from backend.core.ouroboros.governance import orchestrator
        src = Path(orchestrator.__file__).read_text(encoding="utf-8")
        # The integration MUST read from ctx.target_files
        assert (
            '_target_files = getattr(ctx, "target_files", ()) or ()'
            in src
        )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_all_exports_present(self):
        for name in gbg.__all__:
            assert getattr(gbg, name) is not None

    def test_schema_version(self):
        assert GOVERNANCE_BOUNDARY_GATE_SCHEMA_VERSION.startswith(
            "governance_boundary_gate.",
        )
