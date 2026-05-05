"""Upgrade 2 Slice 5 — Graduation regression tests
(CLOSES Upgrade 2).

Pins:
  * Master flag default-TRUE post graduation
  * /decisions REPL + replay CLI auto-discovery
  * 4 FlagRegistry seeds installed
  * 4 AST shipped-code-invariants pins HOLD against shipped code
  * SSE event vocabulary present
  * scripts/replay_determinism.py launcher exists

Mirrors M9 / Upgrade 1 / M11 graduation discipline.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(
        self, monkeypatch,
    ):
        monkeypatch.delenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_determinism_enabled,
        )
        assert replay_determinism_enabled() is True

    def test_explicit_false_instant_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_DETERMINISM_REPLAY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.determinism.replay_determinism import (  # noqa: E501
            replay_determinism_enabled,
        )
        assert replay_determinism_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — /decisions REPL auto-discovery
# ---------------------------------------------------------------------------


class TestDecisionsREPLAutoDiscovery:
    def test_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.decisions_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1


# ---------------------------------------------------------------------------
# § 3 — FlagRegistry seeds (4 entries)
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_master_seed_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        master = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_DETERMINISM_REPLAY_ENABLED"
        )
        assert master.default is True

    def test_default_limit_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_DECISIONS_READER_DEFAULT_LIMIT"
        )
        assert spec.default == 100

    def test_max_records_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_DECISIONS_READER_MAX_RECORDS"
        )
        assert spec.default == 10_000

    def test_max_sessions_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_DECISIONS_READER_MAX_SESSIONS"
        )
        assert spec.default == 1_000


# ---------------------------------------------------------------------------
# § 4 — AST shipped-code-invariants pins (4 entries)
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_four_upgrade_2_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _REGISTRY,
            _register_seed_invariants,
        )
        _register_seed_invariants()
        u2_pins = {
            k for k in _REGISTRY
            if k in {
                "replay_determinism_master_default_true",
                "decision_kind_closed_enum_intact",
                "decisions_observability_read_only",
                "replay_lazy_imports_sse_publisher",
            }
        }
        assert len(u2_pins) == 4, (
            f"Expected 4 Upgrade 2 pins, got {len(u2_pins)}: "
            f"{sorted(u2_pins)}"
        )

    def test_all_upgrade_2_pins_pass_against_shipped_code(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _register_seed_invariants,
            validate_all,
        )
        _register_seed_invariants()
        violations = validate_all()
        u2_pin_names = {
            "replay_determinism_master_default_true",
            "decision_kind_closed_enum_intact",
            "decisions_observability_read_only",
            "replay_lazy_imports_sse_publisher",
        }
        u2_violations = [
            v for v in violations
            if v.invariant_name in u2_pin_names
        ]
        assert u2_violations == [], (
            f"Upgrade 2 AST pins regressed: "
            f"{[v.invariant_name for v in u2_violations]}"
        )


# ---------------------------------------------------------------------------
# § 5 — SSE event vocabulary present
# ---------------------------------------------------------------------------


class TestSSEVocabularyPresent:
    def test_decision_drift_detected_constant(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_DECISION_DRIFT_DETECTED,
        )
        assert (
            EVENT_TYPE_DECISION_DRIFT_DETECTED
            == "decision_drift_detected"
        )

    def test_publisher_helper_callable(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_decision_drift_event,
        )
        assert callable(publish_decision_drift_event)


# ---------------------------------------------------------------------------
# § 6 — scripts/replay_determinism.py launcher exists
# ---------------------------------------------------------------------------


class TestLauncherExists:
    def test_launcher_file_present(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts" / "replay_determinism.py"
        )
        assert path.exists(), (
            "scripts/replay_determinism.py launcher missing"
        )

    def test_launcher_has_main(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "scripts" / "replay_determinism.py"
        )
        source = path.read_text(encoding="utf-8")
        # Sanity — the launcher delegates to the primitive
        assert "replay_cli_main" in source
        assert "def main(" in source


# ---------------------------------------------------------------------------
# § 7 — Closure regression — full Upgrade 2 spine still green
# ---------------------------------------------------------------------------


class TestSpineHealth:
    """The graduation regression file is itself part of the
    spine — running it confirms the contract holds. Other
    Upgrade 2 test files run via the standard pytest invocation;
    this section catalogs them for operator visibility."""

    def test_decision_kinds_module_present(self):
        from backend.core.ouroboros.governance.determinism import (  # noqa: E501
            decision_kinds as dk,
        )
        assert hasattr(dk, "DecisionKind")

    def test_replay_determinism_module_present(self):
        from backend.core.ouroboros.governance.determinism import (  # noqa: E501
            replay_determinism as rd,
        )
        assert hasattr(rd, "replay_session_consistency")
        assert hasattr(rd, "replay_cli_main")

    def test_decisions_reader_module_present(self):
        from backend.core.ouroboros.governance.determinism import (  # noqa: E501
            decisions_reader as r,
        )
        assert hasattr(r, "list_available_sessions")
        assert hasattr(r, "read_records_for_session")

    def test_decisions_observability_module_present(self):
        from backend.core.ouroboros.governance import (
            decisions_observability as obs,
        )
        assert hasattr(obs, "register_routes")

    def test_decisions_repl_module_present(self):
        from backend.core.ouroboros.governance import (
            decisions_repl as r,
        )
        assert hasattr(r, "dispatch_decisions_command")
        assert hasattr(r, "register_verbs")
