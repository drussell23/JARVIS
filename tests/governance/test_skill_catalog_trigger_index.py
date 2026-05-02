"""Slice 2 (SkillRegistry-AutonomousReach) -- catalog trigger index.

Pins the new ``SkillCatalog.triggers_for_signal`` lookup +
``trigger_index_counts`` observability helper that landed
in skill_catalog.py.

Coverage:
  * Index built on register / cleared on unregister + reset
  * triggers_for_signal returns matching candidates by kind
  * Payload-narrowed matches (posture / drift_kind / sensor_name)
  * Pre-arc manifests with empty trigger_specs add zero entries
  * DISABLED kind specs filter out at lookup time (via
    spec_matches_invocation, no parallel decision path)
  * Multiple skills sharing the same kind all returned
  * Unregister cleans the index AND empty kind buckets are
    dropped (counts stay honest)
  * Race resilience: if a manifest is unregistered between
    snapshot and lookup, the candidate is skipped (not returned)
  * trigger_index_counts surfaces kind -> count
  * Catalog narrows; compute_should_fire still authoritative
    (the candidate may still be denied by reach gate / risk gate
    / master flag at the decision layer)
  * Backward-compat: existing register/unregister/get/list_all
    behavior unchanged for manifests without trigger_specs
  * NEVER raises -- garbage invocation returns []
  * Lock domain: index mutation is atomic with primary index
"""
from __future__ import annotations

import threading

import pytest

from backend.core.ouroboros.governance.skill_catalog import (
    SkillCatalog,
    SkillSource,
)
from backend.core.ouroboros.governance.skill_manifest import (
    SkillManifest,
    SkillReach,
)
from backend.core.ouroboros.governance.skill_trigger import (
    SkillInvocation,
    SkillOutcome,
    SkillTriggerKind,
    compute_should_fire,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_manifest(
    *,
    name: str,
    reach: str = "any",
    trigger_specs=None,
):
    return SkillManifest.from_mapping({
        "name": name,
        "description": "d", "trigger": "t",
        "entrypoint": "mod.x:f",
        "reach": reach,
        "trigger_specs": list(trigger_specs or ()),
    })


@pytest.fixture
def catalog() -> SkillCatalog:
    return SkillCatalog()


@pytest.fixture
def autonomous_invocation_factory():
    """Build an autonomous invocation given a kind + payload."""
    def _make(
        *,
        kind: SkillTriggerKind = SkillTriggerKind.SENSOR_FIRED,
        payload=None,
        skill_name: str = "any",
    ):
        return SkillInvocation(
            skill_name=skill_name,
            triggered_by_kind=kind,
            triggered_by_signal=f"signal.{kind.value}",
            payload=payload or {},
        )
    return _make


# ---------------------------------------------------------------------------
# Index build/teardown lifecycle
# ---------------------------------------------------------------------------


class TestIndexLifecycle:
    def test_pre_arc_manifest_no_index_entries(self, catalog):
        m = SkillManifest.from_mapping({
            "name": "legacy",
            "description": "d", "trigger": "t",
            "entrypoint": "mod.x:f",
        })
        catalog.register(m, source=SkillSource.OPERATOR)
        assert catalog.trigger_index_counts() == {}

    def test_register_indexes_specs(self, catalog):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
            {"kind": "drift_detected"},
            {"kind": "sensor_fired"},  # second sensor_fired
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        counts = catalog.trigger_index_counts()
        assert counts == {"sensor_fired": 2, "drift_detected": 1}

    def test_unregister_removes_from_index(self, catalog):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
            {"kind": "drift_detected"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        assert catalog.unregister("a") is True
        # Empty kind buckets dropped -- counts stay honest.
        assert catalog.trigger_index_counts() == {}

    def test_unregister_only_drops_target_skill(self, catalog):
        m1 = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        m2 = _build_manifest(name="b", trigger_specs=[
            {"kind": "sensor_fired"},
            {"kind": "drift_detected"},
        ])
        catalog.register(m1, source=SkillSource.OPERATOR)
        catalog.register(m2, source=SkillSource.OPERATOR)
        catalog.unregister("a")
        # Only b's specs remain.
        assert catalog.trigger_index_counts() == {
            "sensor_fired": 1, "drift_detected": 1,
        }

    def test_reset_clears_index(self, catalog):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        catalog.reset()
        assert catalog.trigger_index_counts() == {}

    def test_index_atomic_with_primary_register(self, catalog):
        """Index mutation must happen INSIDE the same lock as
        primary write -- proven by checking that get() and
        triggers_for_signal() see consistent state from a single
        register call."""
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # Primary lookup sees the manifest.
        assert catalog.get("a") is not None
        # Trigger lookup sees the indexed spec.
        assert catalog.trigger_index_counts() == {"sensor_fired": 1}


# ---------------------------------------------------------------------------
# triggers_for_signal narrowing
# ---------------------------------------------------------------------------


class TestTriggersForSignal:
    def test_kind_narrowing(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
            {"kind": "drift_detected"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # SENSOR_FIRED invocation -> only the sensor_fired spec
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        out = catalog.triggers_for_signal(inv)
        assert len(out) == 1
        assert out[0][0].qualified_name == "a"
        assert out[0][1] == 0  # spec_index

    def test_no_match_returns_empty(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # POSTURE_TRANSITION invocation -> no candidates
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.POSTURE_TRANSITION,
        )
        assert catalog.triggers_for_signal(inv) == []

    def test_payload_narrowing_posture(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {
                "kind": "posture_transition",
                "required_posture": "HARDEN",
            },
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # Wrong posture -> no candidates (filtered by
        # spec_matches_invocation).
        inv_wrong = autonomous_invocation_factory(
            kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "EXPLORE"},
        )
        assert catalog.triggers_for_signal(inv_wrong) == []
        # Right posture -> match.
        inv_right = autonomous_invocation_factory(
            kind=SkillTriggerKind.POSTURE_TRANSITION,
            payload={"posture": "HARDEN"},
        )
        out = catalog.triggers_for_signal(inv_right)
        assert len(out) == 1
        assert out[0][0].qualified_name == "a"

    def test_payload_narrowing_drift_kind(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {
                "kind": "drift_detected",
                "required_drift_kind": "RECURRENCE_DRIFT",
            },
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # Wrong drift kind -> no.
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.DRIFT_DETECTED,
            payload={"drift_kind": "POSTURE_LOCKED"},
        )
        assert catalog.triggers_for_signal(inv) == []

    def test_payload_narrowing_sensor_name(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {
                "kind": "sensor_fired",
                "required_sensor_name": "test_failure",
            },
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        inv_wrong = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
            payload={"sensor_name": "voice_command"},
        )
        assert catalog.triggers_for_signal(inv_wrong) == []
        inv_right = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
            payload={"sensor_name": "test_failure"},
        )
        assert len(catalog.triggers_for_signal(inv_right)) == 1

    def test_multiple_skills_same_kind_all_returned(
        self, catalog, autonomous_invocation_factory,
    ):
        for n in ("a", "b", "c"):
            catalog.register(
                _build_manifest(name=n, trigger_specs=[
                    {"kind": "sensor_fired"},
                ]),
                source=SkillSource.OPERATOR,
            )
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        out = catalog.triggers_for_signal(inv)
        assert len(out) == 3
        assert {m.qualified_name for m, _ in out} == {"a", "b", "c"}

    def test_multiple_specs_in_one_skill_all_returned(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
            {"kind": "drift_detected"},
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        out = catalog.triggers_for_signal(inv)
        # Both sensor_fired specs returned with their respective
        # indices.
        assert len(out) == 2
        indices = sorted(idx for _, idx in out)
        assert indices == [0, 2]

    def test_disabled_kind_spec_filters_out(
        self, catalog, autonomous_invocation_factory,
    ):
        # A spec with kind=DISABLED is never indexed under
        # SENSOR_FIRED, so a SENSOR_FIRED invocation can't match
        # it. Even if the index *did* contain it, the spec match
        # check filters DISABLED to never match.
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "disabled"},
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # DISABLED is in the index under "disabled" kind.
        assert catalog.trigger_index_counts() == {
            "disabled": 1, "sensor_fired": 1,
        }
        # An invocation with DISABLED kind matches no spec.
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.DISABLED,
        )
        assert catalog.triggers_for_signal(inv) == []
        # SENSOR_FIRED invocation only matches the sensor_fired
        # spec, not the disabled one.
        inv_sf = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        out = catalog.triggers_for_signal(inv_sf)
        assert len(out) == 1
        assert out[0][1] == 1  # the sensor_fired spec at index 1

    def test_garbage_invocation_returns_empty(self, catalog):
        assert catalog.triggers_for_signal(None) == []  # type: ignore[arg-type]
        assert catalog.triggers_for_signal("oops") == []  # type: ignore[arg-type]
        assert catalog.triggers_for_signal(42) == []  # type: ignore[arg-type]

    def test_invocation_with_garbage_kind_returns_empty(
        self, catalog,
    ):
        # Manually construct an invocation with a non-enum kind.
        inv = SkillInvocation.__new__(SkillInvocation)
        object.__setattr__(inv, "skill_name", "x")
        object.__setattr__(inv, "triggered_by_kind", "fake_kind")
        object.__setattr__(inv, "triggered_by_signal", "")
        object.__setattr__(inv, "triggered_at_monotonic", 0.0)
        object.__setattr__(inv, "arguments", {})
        object.__setattr__(inv, "payload", {})
        object.__setattr__(inv, "caller_op_id", "")
        object.__setattr__(inv, "schema_version", "")
        assert catalog.triggers_for_signal(inv) == []

    def test_empty_catalog_returns_empty(
        self, catalog, autonomous_invocation_factory,
    ):
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        assert catalog.triggers_for_signal(inv) == []

    def test_returns_actual_manifest_instances(
        self, catalog, autonomous_invocation_factory,
    ):
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED,
        )
        out = catalog.triggers_for_signal(inv)
        # Same identity (manifest is frozen, references safe to
        # share)
        assert out[0][0] is m


# ---------------------------------------------------------------------------
# Catalog narrows; compute_should_fire decides
# ---------------------------------------------------------------------------


class TestCatalogVsDecision:
    def test_catalog_narrows_decision_authoritative(
        self, catalog, autonomous_invocation_factory,
    ):
        """The catalog returns candidates whose SPEC matches; the
        decision function is still the authority. Verify the
        composition: lookup -> compute_should_fire returns INVOKED
        when both layers agree."""
        m = _build_manifest(
            name="a",
            reach="autonomous",
            trigger_specs=[{"kind": "sensor_fired"}],
        )
        catalog.register(m, source=SkillSource.OPERATOR)
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED, skill_name="a",
        )
        candidates = catalog.triggers_for_signal(inv)
        assert len(candidates) == 1
        manifest, _ = candidates[0]
        result = compute_should_fire(manifest, inv, enabled=True)
        assert result.outcome is SkillOutcome.INVOKED

    def test_catalog_returns_candidate_decision_can_still_deny(
        self, catalog, autonomous_invocation_factory,
    ):
        """A skill with reach=OPERATOR_PLUS_MODEL is still a
        valid trigger-index entry, but compute_should_fire denies
        autonomous invocations of it -- proving the catalog is a
        narrowing index, not a fire authority."""
        m = _build_manifest(
            name="a",
            reach="operator_plus_model",  # excludes autonomous
            trigger_specs=[{"kind": "sensor_fired"}],
        )
        catalog.register(m, source=SkillSource.OPERATOR)
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED, skill_name="a",
        )
        candidates = catalog.triggers_for_signal(inv)
        # Catalog returns it (spec kind matches).
        assert len(candidates) == 1
        # But the decision function denies because of reach gate.
        manifest, _ = candidates[0]
        result = compute_should_fire(manifest, inv, enabled=True)
        assert result.outcome is SkillOutcome.SKIPPED_DISABLED

    def test_blocked_risk_class_catalog_returns_decision_denies(
        self, catalog, autonomous_invocation_factory,
    ):
        """Same composition test for the risk gate."""
        # Build via SkillManifest direct construction (risk_class
        # isn't part of SkillManifest -- it's only on the trigger
        # primitive's compute_should_fire). The decision function
        # reads risk_class via getattr and treats absent as
        # safe_auto. So we can't easily test "blocked" via a
        # manifest yet -- that's a Slice 4 concern (risk_class
        # field on SkillManifest). Verify the placeholder: catalog
        # returns; default (safe_auto) decision is INVOKED.
        m = _build_manifest(
            name="a", reach="autonomous",
            trigger_specs=[{"kind": "sensor_fired"}],
        )
        catalog.register(m, source=SkillSource.OPERATOR)
        inv = autonomous_invocation_factory(
            kind=SkillTriggerKind.SENSOR_FIRED, skill_name="a",
        )
        candidates = catalog.triggers_for_signal(inv)
        manifest, _ = candidates[0]
        # Risk floor blocked at decision time -> DENIED_POLICY.
        result = compute_should_fire(
            manifest, inv, risk_floor="blocked", enabled=True,
        )
        assert result.outcome is SkillOutcome.DENIED_POLICY


# ---------------------------------------------------------------------------
# Race resilience -- unregister between snapshot and lookup
# ---------------------------------------------------------------------------


class TestRaceResilience:
    def test_unregister_during_lookup_skipped(self, catalog):
        """Construct a state where a manifest is in the trigger
        index but missing from _by_qualified_name (synthetic race
        injection). Lookup must skip the orphan candidate, not
        raise."""
        m = _build_manifest(name="a", trigger_specs=[
            {"kind": "sensor_fired"},
        ])
        catalog.register(m, source=SkillSource.OPERATOR)
        # Surgically remove ONLY from primary index, leaving
        # trigger index pointing at a phantom name.
        with catalog._lock:
            del catalog._by_qualified_name["a"]
        # The trigger index still has the entry...
        assert catalog.trigger_index_counts() == {"sensor_fired": 1}
        # ...but lookup defensively skips the orphan.
        inv = SkillInvocation(
            skill_name="a",
            triggered_by_kind=SkillTriggerKind.SENSOR_FIRED,
        )
        assert catalog.triggers_for_signal(inv) == []


# ---------------------------------------------------------------------------
# trigger_index_counts observability
# ---------------------------------------------------------------------------


class TestTriggerIndexCounts:
    def test_empty(self, catalog):
        assert catalog.trigger_index_counts() == {}

    def test_per_kind_count(self, catalog):
        catalog.register(
            _build_manifest(name="a", trigger_specs=[
                {"kind": "sensor_fired"},
                {"kind": "sensor_fired"},
                {"kind": "drift_detected"},
            ]),
            source=SkillSource.OPERATOR,
        )
        catalog.register(
            _build_manifest(name="b", trigger_specs=[
                {"kind": "drift_detected"},
                {"kind": "posture_transition"},
            ]),
            source=SkillSource.OPERATOR,
        )
        assert catalog.trigger_index_counts() == {
            "sensor_fired": 2,
            "drift_detected": 2,
            "posture_transition": 1,
        }


# ---------------------------------------------------------------------------
# Lock domain: index mutation atomic with primary index
# ---------------------------------------------------------------------------


class TestLockDomain:
    def test_concurrent_register_unregister_safe(self, catalog):
        """Hammer register + unregister from multiple threads.
        Final state must be consistent (every registered manifest
        present in BOTH indexes; every unregistered manifest
        absent from BOTH)."""
        names = [f"sk{i}" for i in range(20)]

        def _reg(n):
            catalog.register(
                _build_manifest(name=n, trigger_specs=[
                    {"kind": "sensor_fired"},
                ]),
                source=SkillSource.OPERATOR,
            )

        threads = [
            threading.Thread(target=_reg, args=(n,)) for n in names
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # All present in primary.
        all_present = catalog.list_all()
        assert {m.qualified_name for m in all_present} == set(names)
        # All present in trigger index.
        assert (
            catalog.trigger_index_counts().get("sensor_fired", 0)
            == 20
        )
        # Now unregister half from threads.
        to_remove = names[:10]

        def _unreg(n):
            catalog.unregister(n)

        threads = [
            threading.Thread(target=_unreg, args=(n,)) for n in to_remove
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Both indexes consistent.
        remaining = {m.qualified_name for m in catalog.list_all()}
        assert remaining == set(names[10:])
        assert (
            catalog.trigger_index_counts().get("sensor_fired", 0)
            == 10
        )
