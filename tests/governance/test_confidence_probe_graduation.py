"""Move 5 Slice 5 — Graduation regression spine.

Pins the full Slice 5 contract:

  * 2 master flags (bridge + prober) default-true post-graduation
    + asymmetric env semantics (3 flags × 9 values matrix)
  * 4 cap knobs (max_questions, convergence_quorum,
    max_tool_rounds, wall_clock_s) with floor + ceiling structural
    safety
  * 6 FlagSpec entries seeded in flag_registry_seed.SEED_SPECS
  * 3 shipped_code_invariants AST pins registered + currently-hold
  * SSE event EVENT_TYPE_PROBE_OUTCOME pinned + publisher
    publishes on non-DISABLED outcomes (capturing fake broker)
  * 4 GET routes mountable + 503 with master off + 200 with on
  * Full-revert matrix — every flag-combo produces clean state

These pins lock the post-graduation contract so future refactors
that drop a flag, change a default, or break the wire-up are
caught by CI.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.flag_registry_seed import (
    SEED_SPECS,
)
from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
    list_shipped_code_invariants,
    validate_all,
)
from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    bridge_enabled,
    convergence_quorum,
    max_questions,
    max_tool_rounds_per_question,
)
from backend.core.ouroboros.governance.verification.confidence_probe_runner import (  # noqa: E501
    EVENT_TYPE_PROBE_OUTCOME,
    probe_wall_clock_s,
    publish_probe_outcome,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    READONLY_TOOL_ALLOWLIST,
    prober_enabled,
)


# ---------------------------------------------------------------------------
# 1. Master flag graduation — 2 flags × asymmetric env semantics matrix
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_bridge_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", raising=False,
        )
        assert bridge_enabled() is True

    def test_prober_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
            raising=False,
        )
        assert prober_enabled() is True

    @pytest.mark.parametrize(
        "flag_name,read_fn",
        [
            ("JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED",
             bridge_enabled),
            ("JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
             prober_enabled),
        ],
    )
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", True),  # whitespace = unset = post-graduation
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
    def test_asymmetric_env_full_matrix(
        self, monkeypatch, flag_name, read_fn, value, expected,
    ):
        monkeypatch.setenv(flag_name, value)
        assert read_fn() is expected

    def test_individual_revert_does_not_cascade(self, monkeypatch):
        """Reverting bridge does NOT revert prober (independent
        knobs)."""
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        monkeypatch.delenv(
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
            raising=False,
        )
        assert bridge_enabled() is False
        assert prober_enabled() is True


# ---------------------------------------------------------------------------
# 2. Cap knobs — floor + ceiling structural safety
# ---------------------------------------------------------------------------


class TestCapKnobs:
    @pytest.mark.parametrize(
        "knob,read_fn,floor,default,ceiling",
        [
            ("JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS",
             max_questions, 2, 3, 5),
            ("JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS",
             max_tool_rounds_per_question, 1, 5, 10),
            ("JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S",
             probe_wall_clock_s, 5.0, 30.0, 120.0),
        ],
    )
    def test_default_when_unset(
        self, monkeypatch, knob, read_fn, floor, default,
        ceiling,
    ):
        monkeypatch.delenv(knob, raising=False)
        assert read_fn() == default

    @pytest.mark.parametrize(
        "knob,read_fn,floor,default,ceiling,below_floor_value",
        [
            # INT knobs: use int strings below floor
            ("JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS",
             max_questions, 2, 3, 5, "1"),
            ("JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS",
             max_tool_rounds_per_question, 1, 5, 10, "0"),
            # FLOAT knob: use sub-floor float
            ("JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S",
             probe_wall_clock_s, 5.0, 30.0, 120.0, "0.001"),
        ],
    )
    def test_floor_clamp(
        self, monkeypatch, knob, read_fn, floor, default,
        ceiling, below_floor_value,
    ):
        monkeypatch.setenv(knob, below_floor_value)
        assert read_fn() == floor

    @pytest.mark.parametrize(
        "knob,read_fn,floor,default,ceiling",
        [
            ("JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS",
             max_questions, 2, 3, 5),
            ("JARVIS_CONFIDENCE_PROBE_MAX_TOOL_ROUNDS",
             max_tool_rounds_per_question, 1, 5, 10),
            ("JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S",
             probe_wall_clock_s, 5.0, 30.0, 120.0),
        ],
    )
    def test_ceiling_clamp(
        self, monkeypatch, knob, read_fn, floor, default,
        ceiling,
    ):
        monkeypatch.setenv(knob, "999999")
        assert read_fn() == ceiling

    def test_convergence_quorum_floor(self, monkeypatch):
        # Quorum has floor only (no ceiling — a high quorum is
        # operator's choice; convergence math just won't fire)
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM", "1",
        )
        assert convergence_quorum() == 2  # floor

    def test_garbage_falls_to_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS", "garbage",
        )
        assert max_questions() == 3


# ---------------------------------------------------------------------------
# 3. FlagRegistry seeds — 6 entries + posture relevance
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    EXPECTED_SEED_NAMES = {
        "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED",
        "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
        "JARVIS_CONFIDENCE_PROBE_MAX_QUESTIONS",
        "JARVIS_CONFIDENCE_PROBE_CONVERGENCE_QUORUM",
        "JARVIS_CONFIDENCE_PROBE_WALL_CLOCK_S",
        "JARVIS_CONFIDENCE_PROBE_GENERATOR_MODE",
    }

    def test_all_six_seeds_present(self):
        seed_names = {spec.name for spec in SEED_SPECS}
        missing = self.EXPECTED_SEED_NAMES - seed_names
        assert not missing, (
            f"Move 5 seeds missing from "
            f"flag_registry_seed.SEED_SPECS: {missing}"
        )

    def test_two_master_flags_default_true(self):
        masters = {
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED",
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.default is True, (
                    f"{spec.name} seed default must be True post-"
                    f"graduation (got {spec.default!r})"
                )

    def test_seeds_source_file_in_verification_dir(self):
        for spec in SEED_SPECS:
            if spec.name in self.EXPECTED_SEED_NAMES:
                assert "verification/" in spec.source_file, (
                    f"{spec.name} source_file should point at "
                    f"verification/ subdir (got "
                    f"{spec.source_file!r})"
                )

    def test_master_flags_carry_posture_relevance(self):
        masters = {
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED",
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED",
        }
        for spec in SEED_SPECS:
            if spec.name in masters:
                assert spec.posture_relevance, (
                    f"{spec.name} should carry posture_relevance "
                    f"so /help posture filter finds it"
                )


# ---------------------------------------------------------------------------
# 4. shipped_code_invariants — 3 AST pins
# ---------------------------------------------------------------------------


class TestShippedCodeInvariantPins:
    EXPECTED_PIN_NAMES = {
        "confidence_probe_bridge_no_mutation_tools",
        "readonly_evidence_prober_allowlist_pinned",
        "confidence_probe_cap_structure_pinned",
    }

    def test_three_move_5_pins_registered(self):
        names = {
            inv.invariant_name
            for inv in list_shipped_code_invariants()
        }
        missing = self.EXPECTED_PIN_NAMES - names
        assert not missing, (
            f"Move 5 shipped_code_invariants pins missing: "
            f"{missing}"
        )

    def test_pins_currently_hold(self):
        violations = validate_all()
        relevant = [
            v for v in violations
            if v.invariant_name in self.EXPECTED_PIN_NAMES
        ]
        assert relevant == [], (
            f"Move 5 pins fire violations: {relevant}"
        )

    def test_total_invariant_count_at_least_23(self):
        # Move 5 added 3 pins (post-Move-4 baseline 20 → 23).
        # Move 6 added 5 more (28). Future moves may add more —
        # this pin asserts the floor (Move-5 contribution stays
        # registered) without being brittle to growth.
        count = len(list_shipped_code_invariants())
        assert count >= 23, (
            f"expected ≥23 shipped_code_invariants post-Move-5; "
            f"got {count}"
        )


# ---------------------------------------------------------------------------
# 5. SSE event vocabulary + publisher
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_type_constant_pinned(self):
        assert EVENT_TYPE_PROBE_OUTCOME == "confidence_probe_outcome"

    def test_publish_with_bridge_off_returns_none(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        result = publish_probe_outcome(
            outcome="converged",
            op_id="op-1",
            detail="x",
            agreement_count=2,
            distinct_count=1,
            total_answers=2,
            canonical_answer="x",
        )
        assert result is None

    def test_publish_never_raises(self, monkeypatch):
        # Even on broker-missing / any failure, must never raise.
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        # Garbage args — defensively coerced
        result = publish_probe_outcome(
            outcome=None,  # type: ignore[arg-type]
            op_id=None,  # type: ignore[arg-type]
            detail=None,  # type: ignore[arg-type]
            agreement_count="not_an_int",  # type: ignore[arg-type]
            distinct_count="x",  # type: ignore[arg-type]
            total_answers="y",  # type: ignore[arg-type]
            canonical_answer=None,
        )
        # Either valid frame_id or None — never raises
        assert result is None or isinstance(result, str)

    def test_publish_with_capturing_broker(self, monkeypatch):
        # Patch the lazy broker import
        published: List[Dict[str, Any]] = []

        class _FakeBroker:
            def publish(self, **kw):
                published.append(kw)
                return f"frame-{len(published)}"

        def _fake_get_broker():
            return _FakeBroker()

        import sys
        import types
        fake_mod = types.ModuleType(
            "backend.core.ouroboros.governance.ide_observability_stream",
        )
        fake_mod.get_default_broker = _fake_get_broker
        monkeypatch.setitem(
            sys.modules,
            "backend.core.ouroboros.governance.ide_observability_stream",  # noqa: E501
            fake_mod,
        )
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        result = publish_probe_outcome(
            outcome="converged",
            op_id="op-x",
            detail="probes agreed",
            agreement_count=2,
            distinct_count=1,
            total_answers=2,
            canonical_answer="foo is a function",
        )
        assert result == "frame-1"
        assert len(published) == 1
        assert published[0]["event_type"] == EVENT_TYPE_PROBE_OUTCOME
        payload = published[0]["payload"]
        assert payload["outcome"] == "converged"
        assert payload["op_id"] == "op-x"
        assert "function" in payload["canonical_answer"]


# ---------------------------------------------------------------------------
# 6. GET routes — 4 endpoints + 503/200 paths
# ---------------------------------------------------------------------------


class TestObservabilityRoutes:
    def test_register_routes_mounts_four_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            register_confidence_probe_routes,
        )
        app = web.Application()
        register_confidence_probe_routes(app)
        paths = {
            r.url_for().path
            for resource in app.router.resources()
            for r in resource
        }
        assert "/observability/probe" in paths
        assert "/observability/probe/config" in paths
        assert "/observability/probe/allowlist" in paths
        assert "/observability/probe/stats" in paths

    def test_routes_safe_to_mount_with_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        from aiohttp import web
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            register_confidence_probe_routes,
        )
        app = web.Application()
        register_confidence_probe_routes(app)

    @pytest.mark.asyncio
    async def test_handler_returns_503_when_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            _ConfidenceProbeRoutesHandler,
        )
        handler = _ConfidenceProbeRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_handler_returns_200_when_master_on(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            _ConfidenceProbeRoutesHandler,
        )
        handler = _ConfidenceProbeRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        assert response.status == 200

    @pytest.mark.asyncio
    async def test_overview_includes_flags_and_cadence(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            _ConfidenceProbeRoutesHandler,
        )
        handler = _ConfidenceProbeRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_overview(request)
        body = json.loads(response.body)
        assert "flags" in body
        assert body["flags"]["bridge_enabled"] is True
        assert "cadence" in body
        assert body["sse_event_type"] == EVENT_TYPE_PROBE_OUTCOME

    @pytest.mark.asyncio
    async def test_allowlist_endpoint_returns_9_tools(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.verification.confidence_probe_observability import (  # noqa: E501
            _ConfidenceProbeRoutesHandler,
        )
        handler = _ConfidenceProbeRoutesHandler()
        request = SimpleNamespace(query={})
        response = await handler.handle_allowlist(request)
        body = json.loads(response.body)
        assert body["count"] == 9
        assert "read_file" in body["allowlist"]
        # Sanity check — no mutation tools
        assert "edit_file" not in body["allowlist"]
        assert "bash" not in body["allowlist"]

    def test_event_channel_imports_confidence_probe_module(self):
        """Slice 5b A — pin the event_channel mount so a future
        refactor cannot silently drop the wiring. Mirrors the
        Move 4 pattern in test_invariant_drift_graduation.py."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        assert (
            "register_confidence_probe_routes" in source
        ), (
            "event_channel must mount the confidence-probe GET "
            "routes (Slice 5b A)"
        )
        assert (
            "Move 5 Slice 5b" in source
        ), (
            "event_channel must mark the wiring with the slice "
            "comment for traceability"
        )


# ---------------------------------------------------------------------------
# 7. Full-revert matrix
# ---------------------------------------------------------------------------


class TestFullRevertMatrix:
    @pytest.mark.parametrize(
        "bridge,prober",
        [
            ("true", "true"),
            ("false", "true"),
            ("true", "false"),
            ("false", "false"),
        ],
    )
    def test_all_four_revert_combinations(
        self, monkeypatch, bridge, prober,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", bridge,
        )
        monkeypatch.setenv(
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", prober,
        )
        assert bridge_enabled() is (bridge == "true")
        assert prober_enabled() is (prober == "true")

    def test_master_off_zeros_bridge_only(self, monkeypatch):
        # Reverting bridge does not affect prober flag readability
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_READONLY_EVIDENCE_PROBER_ENABLED", "true",
        )
        assert bridge_enabled() is False
        assert prober_enabled() is True


# ---------------------------------------------------------------------------
# 8. Allowlist content sanity
# ---------------------------------------------------------------------------


class TestAllowlistSanity:
    def test_allowlist_has_no_mutation_tools(self):
        forbidden = {
            "edit_file", "write_file", "delete_file",
            "run_tests", "bash",
        }
        for tool in READONLY_TOOL_ALLOWLIST:
            assert tool not in forbidden, (
                f"allowlist contains mutation tool: {tool}"
            )

    def test_allowlist_size_is_9(self):
        assert len(READONLY_TOOL_ALLOWLIST) == 9
