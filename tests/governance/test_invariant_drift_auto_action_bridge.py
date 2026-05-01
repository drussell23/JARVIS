"""Move 4 Slice 4 — InvariantDriftAutoActionBridge regression spine.

Coverage tracks the bridge's three contracts:

  * Translation contract — drift records → AdvisoryAction via the
    severity mapping table; mapping env-overridable; aggregation
    is highest-severity.
  * Bridge contract — emit() is best-effort, defensive, never
    raises; master-flag-gated; NO_ACTION never appends ledger;
    ledger + SSE both fire on actionable drift.
  * Authority + cost-contract — bridge consumes ``_propose_action``
    so the §26.6 structural guard is inherited; bridge route
    sentinel is NOT in COST_GATED_ROUTES; AST-pinned imports.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance import (
    invariant_drift_auto_action_bridge as bridge_mod,
)
from backend.core.ouroboros.governance.auto_action_router import (
    AdvisoryAction,
    AdvisoryActionType,
    AutoActionProposalLedger,
    reset_default_ledger_for_tests,
)
from backend.core.ouroboros.governance.invariant_drift_auditor import (
    DriftKind,
    DriftSeverity,
    ExplorationFloorPin,
    InvariantDriftRecord,
    InvariantSnapshot,
)
from backend.core.ouroboros.governance.invariant_drift_auto_action_bridge import (  # noqa: E501
    InvariantDriftAutoActionBridge,
    aggregate_severity,
    bridge_enabled,
    drift_to_action_type,
    install_auto_action_bridge,
    reset_installed_bridge_for_tests,
    severity_to_action_mapping,
)
from backend.core.ouroboros.governance.invariant_drift_observer import (
    get_signal_emitter,
    reset_signal_emitter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _master_flags(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INVARIANT_DRIFT_AUDITOR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
        "true",
    )
    yield


@pytest.fixture(autouse=True)
def _ledger_in_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_AUTO_ACTION_LEDGER_PATH",
        str(tmp_path / "proposals.jsonl"),
    )
    reset_default_ledger_for_tests()
    yield
    reset_default_ledger_for_tests()


@pytest.fixture(autouse=True)
def _isolate_bridge():
    reset_installed_bridge_for_tests()
    reset_signal_emitter()
    yield
    reset_installed_bridge_for_tests()
    reset_signal_emitter()


def _make_record(
    severity: DriftSeverity = DriftSeverity.CRITICAL,
    kind: DriftKind = DriftKind.SHIPPED_INVARIANT_REMOVED,
    affected_keys: Tuple[str, ...] = (),
    detail: str = "stub",
) -> InvariantDriftRecord:
    return InvariantDriftRecord(
        drift_kind=kind, severity=severity,
        detail=detail, affected_keys=affected_keys,
    )


def _make_snapshot(
    snapshot_id: str = "stub",
    posture_value: Optional[str] = "EXPLORE",
) -> InvariantSnapshot:
    return InvariantSnapshot(
        snapshot_id=snapshot_id,
        captured_at_utc=1000.0,
        shipped_invariant_names=("alpha",),
        shipped_violation_signature="",
        shipped_violation_count=0,
        flag_registry_hash="",
        flag_count=0,
        exploration_floor_pins=(),
        posture_value=posture_value,
        posture_confidence=None,
    )


# ---------------------------------------------------------------------------
# 1. Master flag — asymmetric env semantics (default false)
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_true_post_graduation_when_unset(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            raising=False,
        )
        # Slice 5 graduation flipped this default.
        assert bridge_enabled() is True

    @pytest.mark.parametrize(
        "value,expected",
        [("1", True), ("true", True), ("YES", True), ("on", True),
         ("0", False), ("false", False), ("no", False),
         # Empty = unset = post-graduation default true
         ("", True),
         # Garbage falls to revert
         ("garbage", False)],
    )
    def test_env_matrix(self, monkeypatch, value, expected):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            value,
        )
        assert bridge_enabled() is expected


# ---------------------------------------------------------------------------
# 2. Severity → action mapping — defaults + env override
# ---------------------------------------------------------------------------


class TestSeverityMapping:
    def test_defaults_have_sensible_severity_distribution(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING", raising=False,
        )
        m = severity_to_action_mapping()
        # CRITICAL must escalate; INFO must be benign.
        assert m[DriftSeverity.CRITICAL] is not \
            AdvisoryActionType.NO_ACTION
        assert m[DriftSeverity.WARNING] is not \
            AdvisoryActionType.NO_ACTION
        assert m[DriftSeverity.INFO] is AdvisoryActionType.NO_ACTION

    def test_default_critical_maps_to_route_to_notify_apply(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING", raising=False,
        )
        m = severity_to_action_mapping()
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY

    def test_default_warning_maps_to_raise_exploration_floor(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING", raising=False,
        )
        m = severity_to_action_mapping()
        assert m[DriftSeverity.WARNING] is \
            AdvisoryActionType.RAISE_EXPLORATION_FLOOR

    def test_env_override_replaces_individual_keys(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            json.dumps({"critical": "demote_risk_tier"}),
        )
        m = severity_to_action_mapping()
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.DEMOTE_RISK_TIER
        # Defaults preserved for other keys
        assert m[DriftSeverity.WARNING] is \
            AdvisoryActionType.RAISE_EXPLORATION_FLOOR

    def test_env_override_unknown_severity_ignored(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            json.dumps({"unknown_sev": "no_action"}),
        )
        m = severity_to_action_mapping()
        # Defaults preserved
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY

    def test_env_override_unknown_action_ignored(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            json.dumps({"critical": "bogus_action"}),
        )
        m = severity_to_action_mapping()
        # Default preserved
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY

    def test_invalid_json_falls_to_defaults(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            "not_valid_json {{",
        )
        m = severity_to_action_mapping()
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY

    def test_non_dict_payload_falls_to_defaults(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            json.dumps([1, 2, 3]),
        )
        m = severity_to_action_mapping()
        assert m[DriftSeverity.CRITICAL] is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY


# ---------------------------------------------------------------------------
# 3. aggregate_severity + drift_to_action_type
# ---------------------------------------------------------------------------


class TestAggregateAndMap:
    def test_empty_records_returns_none(self):
        assert aggregate_severity(()) is None

    def test_single_record_returns_its_severity(self):
        r = _make_record(DriftSeverity.WARNING)
        assert aggregate_severity((r,)) is DriftSeverity.WARNING

    def test_critical_dominates_warning_and_info(self):
        records = (
            _make_record(DriftSeverity.INFO),
            _make_record(DriftSeverity.WARNING),
            _make_record(DriftSeverity.CRITICAL),
        )
        assert aggregate_severity(records) is DriftSeverity.CRITICAL

    def test_warning_dominates_info(self):
        records = (
            _make_record(DriftSeverity.INFO),
            _make_record(DriftSeverity.WARNING),
        )
        assert aggregate_severity(records) is DriftSeverity.WARNING

    def test_drift_to_action_empty_is_no_action(self):
        assert drift_to_action_type(()) is \
            AdvisoryActionType.NO_ACTION

    def test_drift_to_action_critical_is_route_to_notify(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING", raising=False,
        )
        records = (_make_record(DriftSeverity.CRITICAL),)
        assert drift_to_action_type(records) is \
            AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY

    def test_drift_to_action_info_only_is_no_action(self):
        records = (_make_record(DriftSeverity.INFO),)
        assert drift_to_action_type(records) is \
            AdvisoryActionType.NO_ACTION

    def test_drift_to_action_respects_env_override(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_BRIDGE_MAPPING",
            json.dumps({"critical": "defer_op_family"}),
        )
        records = (_make_record(DriftSeverity.CRITICAL),)
        assert drift_to_action_type(records) is \
            AdvisoryActionType.DEFER_OP_FAMILY


# ---------------------------------------------------------------------------
# 4. Bridge.emit — full decision tree
# ---------------------------------------------------------------------------


class TestBridgeEmit:
    def test_critical_drift_appends_ledger(
        self, monkeypatch, tmp_path,
    ):
        bridge = InvariantDriftAutoActionBridge()
        records = (_make_record(DriftSeverity.CRITICAL),)
        bridge.emit(_make_snapshot(), records)
        # Read ledger file directly
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert path.exists()
        rows = [
            json.loads(ln) for ln in
            path.read_text().splitlines() if ln.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["action_type"] == "route_to_notify_apply"
        assert rows[0]["reason_code"] == "invariant_drift_detected"

    def test_no_action_drift_does_not_append(self):
        bridge = InvariantDriftAutoActionBridge()
        records = (_make_record(DriftSeverity.INFO),)
        bridge.emit(_make_snapshot(), records)
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert not path.exists()

    def test_empty_drift_does_not_append(self):
        bridge = InvariantDriftAutoActionBridge()
        bridge.emit(_make_snapshot(), ())
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert not path.exists()

    def test_master_flag_off_no_op(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_INVARIANT_DRIFT_AUTO_ACTION_BRIDGE_ENABLED",
            "false",
        )
        bridge = InvariantDriftAutoActionBridge()
        bridge.emit(
            _make_snapshot(),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert not path.exists()
        stats = bridge.stats()
        assert stats["emit_count_skipped_disabled"] == 1
        assert stats["emit_count_appended"] == 0

    def test_emit_stats_tracking(self):
        bridge = InvariantDriftAutoActionBridge()
        # 2 critical, 1 info, 1 empty
        bridge.emit(_make_snapshot(),
                    (_make_record(DriftSeverity.CRITICAL),))
        bridge.emit(_make_snapshot(),
                    (_make_record(DriftSeverity.CRITICAL),))
        bridge.emit(_make_snapshot(),
                    (_make_record(DriftSeverity.INFO),))
        bridge.emit(_make_snapshot(), ())
        stats = bridge.stats()
        assert stats["emit_count_total"] == 4
        assert stats["emit_count_appended"] == 2
        assert stats["emit_count_skipped_no_action"] == 2

    def test_proposed_record_carries_posture(self):
        bridge = InvariantDriftAutoActionBridge()
        bridge.emit(
            _make_snapshot(posture_value="HARDEN"),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        row = json.loads(path.read_text().strip())
        assert row["posture"] == "HARDEN"

    def test_proposed_record_carries_op_id_with_snapshot_id(self):
        bridge = InvariantDriftAutoActionBridge()
        bridge.emit(
            _make_snapshot(snapshot_id="snap-abc"),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        row = json.loads(path.read_text().strip())
        assert row["op_id"] == "drift-snap-abc"

    def test_proposed_record_evidence_is_compact(self):
        bridge = InvariantDriftAutoActionBridge()
        records = (
            _make_record(DriftSeverity.CRITICAL),
            _make_record(DriftSeverity.WARNING),
            _make_record(DriftSeverity.INFO),
        )
        bridge.emit(_make_snapshot(), records)
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        row = json.loads(path.read_text().strip())
        ev = row["evidence"]
        assert "critical:" in ev
        assert "warning:" in ev
        assert "info:" in ev
        # Evidence is bounded so SSE/REPL renders cleanly
        assert len(ev) <= 200

    def test_proposed_record_carries_history_size(self):
        bridge = InvariantDriftAutoActionBridge()
        records = tuple(
            _make_record(DriftSeverity.CRITICAL) for _ in range(5)
        )
        bridge.emit(_make_snapshot(), records)
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        row = json.loads(path.read_text().strip())
        assert row["history_size"] == 5

    def test_emit_never_raises_on_ledger_failure(self):
        # Inject a ledger that raises on append
        class _BoomLedger(AutoActionProposalLedger):
            def append(self, action):
                raise OSError("disk full")

        bridge = InvariantDriftAutoActionBridge(ledger=_BoomLedger())
        # Must NOT propagate
        bridge.emit(
            _make_snapshot(),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        # SSE publish was still attempted (best-effort independent
        # of ledger result)
        stats = bridge.stats()
        assert stats["emit_count_total"] == 1
        assert stats["emit_count_appended"] == 0

    def test_emit_never_raises_on_sse_failure(self, monkeypatch):
        # Patch publish to raise
        monkeypatch.setattr(
            bridge_mod, "publish_auto_action_proposal_emitted",
            lambda action: (_ for _ in ()).throw(
                RuntimeError("SSE broker dead"),
            ),
        )
        bridge = InvariantDriftAutoActionBridge()
        # Must NOT propagate
        bridge.emit(
            _make_snapshot(),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        # Ledger append still landed
        stats = bridge.stats()
        assert stats["emit_count_appended"] == 1

    def test_propose_action_failure_skipped(self, monkeypatch):
        # Patch _propose_action to raise (simulating a future cost-
        # contract guard fire)
        monkeypatch.setattr(
            bridge_mod, "_propose_action",
            lambda **kw: (_ for _ in ()).throw(
                RuntimeError("cost contract violation"),
            ),
        )
        bridge = InvariantDriftAutoActionBridge()
        # Must NOT propagate
        bridge.emit(
            _make_snapshot(),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        stats = bridge.stats()
        assert stats["emit_count_failed_construction"] == 1
        # No ledger append, no SSE
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert not path.exists()


# ---------------------------------------------------------------------------
# 5. install_auto_action_bridge — convenience installer
# ---------------------------------------------------------------------------


class TestInstaller:
    def test_install_registers_bridge_as_emitter(self):
        bridge = install_auto_action_bridge()
        assert get_signal_emitter() is bridge

    def test_install_idempotent(self):
        a = install_auto_action_bridge()
        b = install_auto_action_bridge()
        assert a is b

    def test_install_returns_bridge_instance(self):
        bridge = install_auto_action_bridge()
        assert isinstance(bridge, InvariantDriftAutoActionBridge)

    def test_reset_clears_install(self):
        a = install_auto_action_bridge()
        reset_installed_bridge_for_tests()
        b = install_auto_action_bridge()
        assert a is not b


# ---------------------------------------------------------------------------
# 6. End-to-end — observer feeds bridge feeds ledger
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_observer_drift_lands_in_auto_action_ledger(
        self, monkeypatch, tmp_path,
    ):
        """The full path: drift detected by the observer → emitted
        through the registered bridge → AdvisoryAction landed in
        the auto_action_router ledger."""
        from backend.core.ouroboros.governance.invariant_drift_store import (
            InvariantDriftStore,
            install_boot_snapshot,
        )
        from backend.core.ouroboros.governance.invariant_drift_observer import (
            InvariantDriftObserver,
        )

        # Establish baseline + observer wired with the bridge
        store = InvariantDriftStore(tmp_path)
        baseline = _make_snapshot(snapshot_id="baseline")
        # Override shipped_invariant_names so we get a real diff
        baseline = InvariantSnapshot(
            **{**baseline.to_dict(),
               "shipped_invariant_names": ("alpha", "beta"),
               "exploration_floor_pins": ()},
        ) if False else InvariantSnapshot(
            snapshot_id="baseline", captured_at_utc=1000.0,
            shipped_invariant_names=("alpha", "beta"),
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="", flag_count=0,
            exploration_floor_pins=(),
            posture_value="EXPLORE", posture_confidence=0.5,
        )
        install_boot_snapshot(store=store, snapshot=baseline)

        bridge = install_auto_action_bridge()
        drifted = InvariantSnapshot(
            snapshot_id="drifted", captured_at_utc=2000.0,
            shipped_invariant_names=("alpha",),  # beta dropped
            shipped_violation_signature="",
            shipped_violation_count=0,
            flag_registry_hash="", flag_count=0,
            exploration_floor_pins=(),
            posture_value="EXPLORE", posture_confidence=0.5,
        )
        observer = InvariantDriftObserver(
            store, capture=lambda: drifted,
            posture_reader=lambda: None,
        )
        result = await observer.run_one_cycle()
        assert len(result.drift_records) >= 1
        # Bridge should have emitted to ledger
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        assert path.exists()
        rows = [
            json.loads(ln) for ln in
            path.read_text().splitlines() if ln.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["op_id"] == "drift-drifted"
        assert rows[0]["action_type"] == "route_to_notify_apply"
        assert "shipped_invariant_removed" in rows[0]["evidence"]
        # Bridge stats
        stats = bridge.stats()
        assert stats["emit_count_appended"] == 1


# ---------------------------------------------------------------------------
# 7. Cost contract — bridge route is NOT in COST_GATED_ROUTES
# ---------------------------------------------------------------------------


class TestCostContract:
    def test_bridge_route_sentinel_is_not_cost_gated(self):
        # Read the bridge route + COST_GATED_ROUTES from the
        # cost_contract_assertion module and verify they're disjoint.
        from backend.core.ouroboros.governance.cost_contract_assertion import (  # noqa: E501
            COST_GATED_ROUTES,
        )
        # Read the sentinel from the bridge module
        bridge_route = bridge_mod._BRIDGE_ROUTE
        assert bridge_route not in COST_GATED_ROUTES, (
            f"bridge route {bridge_route!r} must NOT be in "
            f"COST_GATED_ROUTES {COST_GATED_ROUTES} — drift "
            f"signals are out-of-band of any per-op route"
        )

    def test_advisory_action_carries_drift_bridge_route_evidence(
        self,
    ):
        """The action constructed by the bridge should carry an op_id
        prefix unmistakably identifying its origin (drift-...) so
        operator auditing can filter bridge-emitted vs
        postmortem/confidence/adaptation-emitted proposals."""
        bridge = InvariantDriftAutoActionBridge()
        bridge.emit(
            _make_snapshot(snapshot_id="abc"),
            (_make_record(DriftSeverity.CRITICAL),),
        )
        path = Path(os.environ["JARVIS_AUTO_ACTION_LEDGER_PATH"])
        row = json.loads(path.read_text().strip())
        assert row["op_id"].startswith("drift-")


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
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
    "subagent_scheduler",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance"
                / "invariant_drift_auto_action_bridge.py"
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
            f"invariant_drift_auto_action_bridge.py imports "
            f"forbidden authority modules: {offenders}"
        )

    def test_governance_imports_in_allowlist(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        allowed = (
            "auto_action_router",
            "invariant_drift_auditor",
            "invariant_drift_observer",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if not mod.startswith(
                    "backend.core.ouroboros.governance",
                ):
                    continue
                ok = any(sub in mod for sub in allowed)
                assert ok, (
                    f"bridge imports unexpected governance module: "
                    f"{mod}"
                )

    def test_public_api_exported(self):
        expected_exports = {
            "InvariantDriftAutoActionBridge",
            "aggregate_severity",
            "bridge_enabled",
            "drift_to_action_type",
            "install_auto_action_bridge",
            "reset_installed_bridge_for_tests",
            "severity_to_action_mapping",
        }
        assert set(bridge_mod.__all__) == expected_exports

    def test_module_uses_propose_action_for_cost_guard(self):
        # The bridge MUST consume _propose_action (single source of
        # truth for the §26.6 structural cost-contract guard).
        # Pinned by source-token grep so a refactor that bypasses
        # it is caught.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        assert "_propose_action" in source, (
            "bridge MUST consume _propose_action so §26.6 cost-"
            "contract structural guard is inherited; do not "
            "construct AdvisoryAction directly"
        )
