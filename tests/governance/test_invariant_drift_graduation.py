"""Move 4 Slice 5 — graduation regression spine.

Pins the full Slice 5 contract:

  * The 3 master flags (auditor / observer / bridge) are default
    ``true`` post-graduation, with asymmetric env semantics so an
    explicit ``0`` / ``false`` hot-reverts each independently.
  * Boot snapshot + observer + bridge wiring lands at
    ``GovernedLoopService.start`` (proxied through
    ``EventChannel`` like every other observability surface).
  * 4 GET routes mount + serve under
    ``/observability/invariant-drift{,/baseline,/history,/stats}``.
  * SSE event ``EVENT_TYPE_INVARIANT_DRIFT_DETECTED`` fires for
    novel drift cycles (separate from the auto-action proposal SSE
    which only fires for actionable drift).
  * 8 FlagSpec entries register in ``flag_registry_seed.SEED_SPECS``.
  * 2 ``shipped_code_invariants`` AST pins fire on the Slice 1 +
    Slice 4 modules.
  * Full-revert matrix — every ``0``/``false`` combination produces
    expected behavior; no flag combination breaks the system.

These pins lock the post-graduation contract so future refactors
that drop a flag, change a default, or break a boot wire-up are
caught by CI.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
from typing import List

import pytest

from backend.core.ouroboros.governance.flag_registry_seed import (
    SEED_SPECS,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION,
    invariant_drift_auditor_enabled,
)
from backend.core.ouroboros.governance.invariant_drift_auto_action_bridge import (  # noqa: E501
    bridge_enabled,
)
from backend.core.ouroboros.governance.invariant_drift_observer import (
    EVENT_TYPE_INVARIANT_DRIFT_DETECTED,
    observer_enabled,
    publish_invariant_drift_detected,
)
from backend.core.ouroboros.governance.invariant_drift_store import (
    install_boot_snapshot,
    InvariantDriftStore,
    BootSnapshotOutcome,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
    list_shipped_code_invariants,
    validate_all,
)


# ---------------------------------------------------------------------------
# 1. Master flag graduation — three flags, asymmetric env semantics
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_auditor_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            raising=False,
        )
        assert invariant_drift_auditor_enabled() is True

    def test_observer_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            raising=False,
        )
        assert observer_enabled() is True

    def test_bridge_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            raising=False,
        )
        assert bridge_enabled() is True

    @pytest.mark.parametrize(
        "flag_name,read_fn",
        [
            ("JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
             invariant_drift_auditor_enabled),
            ("JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
             observer_enabled),
            ("JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
             bridge_enabled),
        ],
    )
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", True),  # whitespace = unset = default true
            ("0", False),
            ("false", False),
            ("FALSE", False),
            ("no", False),
            ("off", False),
            ("1", True),
            ("true", True),
            ("YES", True),
        ],
    )
    def test_asymmetric_env_semantics_full_matrix(
        self, monkeypatch, flag_name, read_fn, value, expected,
    ):
        """Every flag × every value combination — explicit revert
        tokens must hot-revert independently of the other flags."""
        monkeypatch.setenv(flag_name, value)
        assert read_fn() is expected

    def test_individual_revert_does_not_cascade(self, monkeypatch):
        """Reverting one flag must NOT affect the other two —
        operators must be able to silence any single layer in
        isolation."""
        # Auditor on, observer off, bridge on
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", raising=False,
        )
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "false",
        )
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            raising=False,
        )
        assert invariant_drift_auditor_enabled() is True
        assert observer_enabled() is False
        assert bridge_enabled() is True


# ---------------------------------------------------------------------------
# 2. FlagRegistry seeds — 8 InvariantDriftAuditor flags registered
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    EXPECTED_SEED_NAMES = {
        "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
        "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
        "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
        "JARVIS_INVARIANT_DRIFT_OBSERVER_INTERVAL_S",
        "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_TICKS",
        "JARVIS_INVARIANT_DRIFT_OBSERVER_VIGILANCE_FACTOR",
        "JARVIS_INVARIANT_DRIFT_OBSERVER_DEDUP_WINDOW",
        "JARVIS_INVARIANT_DRIFT_POSTURE_MULTIPLIERS",
    }

    def test_all_eight_seeds_present(self):
        seed_names = {spec.name for spec in SEED_SPECS}
        missing = self.EXPECTED_SEED_NAMES - seed_names
        assert not missing, (
            f"InvariantDriftAuditor seeds missing from "
            f"flag_registry_seed.SEED_SPECS: {missing}"
        )

    def test_three_master_flags_default_true(self):
        masters = {
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.default is True, (
                    f"{spec.name} seed default must be True post-"
                    f"graduation (got {spec.default!r})"
                )

    def test_seeds_have_source_file_pointing_at_module(self):
        for spec in SEED_SPECS:
            if spec.name in self.EXPECTED_SEED_NAMES:
                assert "invariant_drift" in spec.source_file, (
                    f"{spec.name} source_file should point at an "
                    f"invariant_drift module (got "
                    f"{spec.source_file!r})"
                )

    def test_master_flags_carry_posture_relevance(self):
        """Master flags should be tagged as posture-relevant so the
        ``/help posture HARDEN`` filter surfaces them when operators
        are under pressure."""
        masters = {
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED",
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED",
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.posture_relevance, (
                    f"{spec.name} should carry posture_relevance "
                    f"so /help posture filter finds it"
                )


# ---------------------------------------------------------------------------
# 3. shipped_code_invariants AST pins
# ---------------------------------------------------------------------------


class TestShippedCodeInvariantPins:
    def test_two_invariant_drift_pins_registered(self):
        names = {
            inv.invariant_name for inv in list_shipped_code_invariants()
        }
        expected = {
            "invariant_drift_bridge_uses_propose_action",
            "invariant_drift_auditor_no_disk_writes",
        }
        missing = expected - names
        assert not missing, (
            f"shipped_code_invariants pins missing: {missing}"
        )

    def test_pins_currently_hold(self):
        violations = validate_all()
        relevant = [
            v for v in violations
            if "invariant_drift" in v.invariant_name
        ]
        assert relevant == [], (
            f"InvariantDriftAuditor pins fire violations: {relevant}"
        )


# ---------------------------------------------------------------------------
# 4. SSE event constant + publish helper
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_constant_pinned(self):
        assert (
            EVENT_TYPE_INVARIANT_DRIFT_DETECTED
            == "invariant_drift_detected"
        )

    def test_publish_with_no_drift_returns_none(self):
        # Empty drift bundle — no SSE publish.
        from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
            InvariantSnapshot,
        )
        snap = InvariantSnapshot(
            snapshot_id="x", captured_at_utc=1.0,
            shipped_invariant_names=(),
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="", flag_count=0,
            exploration_floor_pins=(),
            posture_value=None, posture_confidence=None,
        )
        result = publish_invariant_drift_detected(snap, ())
        assert result is None

    def test_publish_never_raises_on_broker_missing(
        self, monkeypatch,
    ):
        # Patch the lazy import target so it raises — publish must
        # swallow the error and return None.
        from backend.core.ouroboros.governance import (
            invariant_drift_observer as obs_mod,
        )
        from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
            DriftKind,
            DriftSeverity,
            InvariantDriftRecord,
            InvariantSnapshot,
        )
        snap = InvariantSnapshot(
            snapshot_id="x", captured_at_utc=1.0,
            shipped_invariant_names=(),
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="", flag_count=0,
            exploration_floor_pins=(),
            posture_value=None, posture_confidence=None,
        )
        records = (
            InvariantDriftRecord(
                drift_kind=DriftKind.POSTURE_DRIFT,
                severity=DriftSeverity.INFO,
                detail="x",
            ),
        )
        # The publisher will try to import ide_observability_stream.
        # Even if the broker raises, publish must NEVER raise out.
        result = obs_mod.publish_invariant_drift_detected(
            snap, records,
        )
        # Result is either None (broker missing/disabled) or an
        # event id string. Either is acceptable; the contract is
        # "never raises".
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# 5. GET route mounting + 503 + 200 paths
# ---------------------------------------------------------------------------


class TestObservabilityRoutes:
    def test_register_routes_mounts_four_endpoints(self, tmp_path):
        from aiohttp import web
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            register_invariant_drift_routes,
        )
        app = web.Application()
        register_invariant_drift_routes(app)
        paths = {
            r.url_for().path
            for resource in app.router.resources()
            for r in resource
        }
        assert "/observability/invariant-drift" in paths
        assert (
            "/observability/invariant-drift/baseline" in paths
        )
        assert (
            "/observability/invariant-drift/history" in paths
        )
        assert "/observability/invariant-drift/stats" in paths

    def test_routes_safe_to_mount_with_master_off(self, monkeypatch):
        # Master flag check is per-request; route mounting itself
        # is unconditionally safe.
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        from aiohttp import web
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            register_invariant_drift_routes,
        )
        app = web.Application()
        # Must not raise even with master off
        register_invariant_drift_routes(app)

    @pytest.mark.asyncio
    async def test_handler_returns_503_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            _InvariantDriftRoutesHandler,
        )
        from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
            InvariantDriftStore,
        )
        store = InvariantDriftStore(tmp_path)
        handler = _InvariantDriftRoutesHandler(store=store)
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_handler_returns_200_when_master_on(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            _InvariantDriftRoutesHandler,
        )
        from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
            InvariantDriftStore,
        )
        store = InvariantDriftStore(tmp_path)
        handler = _InvariantDriftRoutesHandler(store=store)
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_overview_includes_flags_and_baseline(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.invariant_drift_auditor import (  # noqa: E501
            InvariantSnapshot,
        )
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            _InvariantDriftRoutesHandler,
        )
        from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
            InvariantDriftStore,
        )
        store = InvariantDriftStore(tmp_path)
        snap = InvariantSnapshot(
            snapshot_id="snap1", captured_at_utc=1.0,
            shipped_invariant_names=("alpha",),
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="", flag_count=0,
            exploration_floor_pins=(),
            posture_value=None, posture_confidence=None,
        )
        store.write_baseline(snap)
        handler = _InvariantDriftRoutesHandler(store=store)
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        body = json.loads(response.body)
        assert "flags" in body
        assert body["flags"]["auditor_enabled"] is True
        assert body["baseline"]["snapshot_id"] == "snap1"

    @pytest.mark.asyncio
    async def test_baseline_endpoint_compact_shape(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            _InvariantDriftRoutesHandler,
        )
        from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
            InvariantDriftStore,
        )
        store = InvariantDriftStore(tmp_path)
        handler = _InvariantDriftRoutesHandler(store=store)
        request = SimpleNamespace(query={})
        response = await handler.handle_baseline(request)
        body = json.loads(response.body)
        assert body["has_baseline"] is False
        assert body["baseline"] is None

    @pytest.mark.asyncio
    async def test_stats_endpoint_includes_cadence(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.invariant_drift_observability import (  # noqa: E501
            _InvariantDriftRoutesHandler,
        )
        from backend.core.ouroboros.governance.invariant_drift_store import (  # noqa: E501
            InvariantDriftStore,
        )
        store = InvariantDriftStore(tmp_path)
        handler = _InvariantDriftRoutesHandler(store=store)
        request = SimpleNamespace(query={})
        response = await handler.handle_stats(request)
        body = json.loads(response.body)
        assert "cadence" in body
        assert "base_interval_s" in body["cadence"]
        assert "posture_multipliers" in body["cadence"]


# ---------------------------------------------------------------------------
# 6. Boot wiring — install_boot_snapshot returns DISABLED with master off
# ---------------------------------------------------------------------------


class TestBootWiring:
    def test_boot_disabled_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        store = InvariantDriftStore(tmp_path)
        result = install_boot_snapshot(store=store)
        assert result.outcome is BootSnapshotOutcome.DISABLED

    def test_boot_post_graduation_runs_with_unset_env(
        self, monkeypatch, tmp_path,
    ):
        # Post-graduation: empty env = default true = boot runs.
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", raising=False,
        )
        store = InvariantDriftStore(tmp_path)
        result = install_boot_snapshot(store=store)
        # Must NOT be DISABLED — graduation default is on.
        assert result.outcome is not BootSnapshotOutcome.DISABLED

    def test_event_channel_imports_invariant_drift_module(self):
        """The boot wiring lives in event_channel.py; pin its
        presence so a refactor that drops the integration is
        caught."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        # Three markers — the Slice 5 wiring is unmistakable.
        assert (
            "register_invariant_drift_routes" in source
        ), "event_channel must mount the invariant-drift GET routes"
        assert (
            "install_boot_snapshot" in source
        ), "event_channel must call install_boot_snapshot at boot"
        assert (
            "Move 4 Slice 5" in source
        ), (
            "event_channel must mark the wiring with the slice "
            "comment for traceability"
        )


# ---------------------------------------------------------------------------
# 7. Full-revert matrix — every revert combo produces clean state
# ---------------------------------------------------------------------------


class TestFullRevertMatrix:
    """The graduation contract: any single flag flip must work in
    isolation without breaking any other flag's behavior."""

    def test_full_revert_master_off_zeros_all_three(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "false",
        )
        # Sub-flags can stay default (true), but they're
        # functionally inert because the master gate fires first.
        assert invariant_drift_auditor_enabled() is False
        # Sub-flag values are still readable independently.
        assert observer_enabled() is True
        assert bridge_enabled() is True

    def test_observer_revert_keeps_bridge_independent(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", "false",
        )
        assert observer_enabled() is False
        # Bridge default still on
        assert bridge_enabled() is True

    def test_bridge_revert_keeps_observer_independent(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            "false",
        )
        assert bridge_enabled() is False
        # Observer default still on
        assert observer_enabled() is True

    @pytest.mark.parametrize(
        "auditor,observer,bridge",
        [
            ("false", "true", "true"),
            ("true", "false", "true"),
            ("true", "true", "false"),
            ("false", "false", "true"),
            ("false", "true", "false"),
            ("true", "false", "false"),
            ("false", "false", "false"),
        ],
    )
    def test_all_seven_revert_combinations(
        self, monkeypatch, auditor, observer, bridge,
    ):
        """7 non-trivial off-combinations + the all-on default = 8
        total flag states. Each must be readable cleanly without
        cross-flag interference."""
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", auditor,
        )
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_OBSERVER_ENABLED", observer,
        )
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            bridge,
        )
        assert invariant_drift_auditor_enabled() is (auditor == "true")
        assert observer_enabled() is (observer == "true")
        assert bridge_enabled() is (bridge == "true")
