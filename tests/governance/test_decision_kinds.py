"""Upgrade 2 Slice 1 — DecisionKind closed-taxonomy tests
(PRD §31.3).

Pins:
  § 1 — Closed enum has exactly 12 values
  § 2 — All values are str-subclass for backward-compat with
        existing freeform ``kind`` strings on shipped ledgers
  § 3 — Schema version constant
  § 4 — Public exports
  § 5 — Backward-compat — existing freeform strings used by
        already-shipped phase runners (route_runner /
        gate_runner / plan_runner / complete_runner) are NOT
        broken by the new enum (additive contract)
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Closed enum vocabulary
# ---------------------------------------------------------------------------


class TestClosedEnum:
    def test_exactly_twelve_values(self):
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        values = {m.value for m in DecisionKind}
        assert values == {
            "route_selection",
            "gate_pass",
            "gate_fail",
            "validator_pass",
            "validator_fail",
            "risk_escalation",
            "probe_trigger",
            "sbt_trigger",
            "auto_action_proposal",
            "approval_request",
            "phase_transition",
            "disabled",
        }

    def test_value_count_is_twelve(self):
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        assert len(list(DecisionKind)) == 12

    @pytest.mark.parametrize(
        "name,value",
        [
            ("ROUTE_SELECTION", "route_selection"),
            ("GATE_PASS", "gate_pass"),
            ("GATE_FAIL", "gate_fail"),
            ("VALIDATOR_PASS", "validator_pass"),
            ("VALIDATOR_FAIL", "validator_fail"),
            ("RISK_ESCALATION", "risk_escalation"),
            ("PROBE_TRIGGER", "probe_trigger"),
            ("SBT_TRIGGER", "sbt_trigger"),
            ("AUTO_ACTION_PROPOSAL", "auto_action_proposal"),
            ("APPROVAL_REQUEST", "approval_request"),
            ("PHASE_TRANSITION", "phase_transition"),
            ("DISABLED", "disabled"),
        ],
    )
    def test_enum_member_value_match(self, name, value):
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        member = getattr(DecisionKind, name)
        assert member.value == value


# ---------------------------------------------------------------------------
# § 2 — str subclass for backward-compat
# ---------------------------------------------------------------------------


class TestStringSubclass:
    def test_inherits_from_str(self):
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        assert issubclass(DecisionKind, str)

    def test_value_substitutes_for_string(self):
        """The .value attribute MUST equal a plain Python str
        for byte-identity with pre-Upgrade-2 ledger writes."""
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        v = DecisionKind.ROUTE_SELECTION.value
        assert isinstance(v, str)
        assert v == "route_selection"
        # Equality with a plain string holds (str-subclass)
        assert DecisionKind.ROUTE_SELECTION == "route_selection"


# ---------------------------------------------------------------------------
# § 3 — Schema version
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant_present(self):
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DECISION_KIND_SCHEMA_VERSION,
        )
        assert (
            DECISION_KIND_SCHEMA_VERSION == "decision_kind.1"
        )


# ---------------------------------------------------------------------------
# § 4 — Public exports
# ---------------------------------------------------------------------------


class TestPublicExports:
    def test_all_lists_match(self):
        from backend.core.ouroboros.governance.determinism import (
            decision_kinds as dk,
        )
        expected = sorted([
            "DECISION_KIND_SCHEMA_VERSION",
            "DecisionKind",
        ])
        assert sorted(dk.__all__) == expected


# ---------------------------------------------------------------------------
# § 5 — Backward-compat with shipped freeform strings
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """The 4 phase runners that already write to the ledger
    (route_runner / gate_runner / plan_runner / complete_runner)
    use freeform ``kind=`` strings. These reads MUST continue
    to work alongside enum-tagged reads. Verify this by
    grepping the live phase-runner sources for the existing
    kind strings + ensuring the enum's PHASE_TRANSITION value
    can substitute when new code writes."""

    def test_existing_route_runner_kind_string_unchanged(self):
        """``route_runner.py`` writes ``kind="route_assignment"``
        — that legacy string must stay distinct and parsable.
        The enum's ROUTE_SELECTION value
        (``"route_selection"``) is the NEW canonical form;
        old records remain readable."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "phase_runners" / "route_runner.py"
        )
        source = path.read_text(encoding="utf-8")
        # Pre-existing kind string is preserved
        assert 'kind="route_assignment"' in source

    def test_route_selection_enum_value_distinct_from_legacy(
        self,
    ):
        """The enum's ``ROUTE_SELECTION.value`` is a NEW
        canonical form; it differs from the legacy
        ``route_assignment`` string used by route_runner.
        New writes that adopt the enum will use the new form;
        legacy reads continue to work because
        DecisionRecord.kind is a freeform string field."""
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        assert (
            DecisionKind.ROUTE_SELECTION.value
            != "route_assignment"
        )
        # And the new value is human-readable + JSON-safe
        assert (
            DecisionKind.ROUTE_SELECTION.value
            == "route_selection"
        )

    def test_decision_record_accepts_enum_value_as_kind(self):
        """A DecisionRecord written with
        ``kind=DecisionKind.X.value`` MUST be byte-identical
        to one written with the raw string ``"route_selection"``
        (because the enum is a str subclass and serializes via
        .value through to_dict())."""
        from backend.core.ouroboros.governance.determinism.decision_kinds import (  # noqa: E501
            DecisionKind,
        )
        from backend.core.ouroboros.governance.determinism.decision_runtime import (  # noqa: E501
            DecisionRecord,
        )
        rec_enum = DecisionRecord(
            record_id="t1", session_id="s", op_id="op",
            phase="ROUTE",
            kind=DecisionKind.ROUTE_SELECTION.value,
            ordinal=0, inputs_hash="h", output_repr="o",
            monotonic_ts=1.0, wall_ts=2.0,
        )
        rec_str = DecisionRecord(
            record_id="t1", session_id="s", op_id="op",
            phase="ROUTE", kind="route_selection",
            ordinal=0, inputs_hash="h", output_repr="o",
            monotonic_ts=1.0, wall_ts=2.0,
        )
        assert rec_enum.to_dict() == rec_str.to_dict()


# ---------------------------------------------------------------------------
# § 6 — Authority floor (zero coupling to authority modules)
# ---------------------------------------------------------------------------


class TestAuthorityFloor:
    """The DecisionKind enum is a pure data primitive. It MUST
    NOT import orchestrator / iron_gate / providers / etc. —
    closed-taxonomy enums never depend on authority modules."""

    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.strategic_direction",
        "from backend.core.ouroboros.governance.sensor_governor",
    )

    def test_no_authority_imports(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "determinism" / "decision_kinds.py"
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"decision_kinds.py must not import {forbidden}"
            )
