"""Upgrade 1 Slice 5 — Graduation regression tests (CLOSES Upgrade 1).

Pins:
  * Master flag default-TRUE post graduation
  * /budget REPL surface (auto-discovered via register_verbs)
  * /budget subcommand contracts (status, op, config, help)
  * 5 FlagRegistry seeds installed
  * 4 AST shipped-code-invariants pins HOLD against shipped code
  * Provider wire-up: lazy-import string presence in Claude +
    DW providers (catches refactors that strip the integration)

Mirrors M11 Slice 5 + Upgrade 3 Slice 5 graduation discipline.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", raising=False,
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_budget_enabled,
        )
        assert epistemic_budget_enabled() is True

    def test_explicit_false_instant_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.epistemic_budget import (  # noqa: E501
            epistemic_budget_enabled,
        )
        assert epistemic_budget_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — /budget REPL auto-discovery
# ---------------------------------------------------------------------------


class TestBudgetREPLGraduation:
    def test_register_verbs_auto_discovers(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.budget_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1

    def test_help_works_master_off(self, monkeypatch):
        """help bypasses master-flag gate (discoverability)."""
        monkeypatch.setenv(
            "JARVIS_EPISTEMIC_BUDGET_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.budget_repl import (
            dispatch_budget_command,
        )
        result = dispatch_budget_command("/budget help")
        assert result.ok is True
        assert "/budget" in result.text


# ---------------------------------------------------------------------------
# § 3 — FlagRegistry seeds (5 entries)
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_master_flag_seed_present_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        master = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_EPISTEMIC_BUDGET_ENABLED"
        )
        assert master.default is True

    def test_max_rounds_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_EPISTEMIC_MAX_ROUNDS"
        )
        assert spec.default == 12

    def test_drop_threshold_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if (
                s.name
                == "JARVIS_EPISTEMIC_CONFIDENCE_DROP_THRESHOLD"
            )
        )
        assert spec.default == 0.25

    def test_sbt_branch_cap_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_EPISTEMIC_SBT_BRANCH_CAP"
        )
        assert spec.default == 3

    def test_tracker_ttl_seed_present(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        spec = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_EPISTEMIC_TRACKER_TTL_S"
        )
        assert spec.default == 3600


# ---------------------------------------------------------------------------
# § 4 — AST shipped-code-invariants pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_four_upgrade_1_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _REGISTRY,
            _register_seed_invariants,
        )
        _register_seed_invariants()
        u1_pins = {
            k for k in _REGISTRY
            if (
                "epistemic_budget" in k
                or k == "tool_executor_per_round_observer_wired"
            )
        }
        # Exactly 4 per scope
        assert len(u1_pins) == 4, (
            f"Expected 4 Upgrade 1 pins, got {len(u1_pins)}: "
            f"{sorted(u1_pins)}"
        )

    def test_all_upgrade_1_pins_pass_against_shipped_code(self):
        """The 4 Upgrade 1 pins MUST hold against the live
        source. If any tripped, the graduation contract
        regressed."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _register_seed_invariants,
            validate_all,
        )
        _register_seed_invariants()
        violations = validate_all()
        u1_violations = [
            v for v in violations
            if (
                "epistemic_budget" in v.invariant_name
                or v.invariant_name == (
                    "tool_executor_per_round_observer_wired"
                )
            )
        ]
        assert u1_violations == [], (
            f"Upgrade 1 AST pins regressed: "
            f"{[v.invariant_name for v in u1_violations]}"
        )


# ---------------------------------------------------------------------------
# § 5 — Provider wire-up presence
# ---------------------------------------------------------------------------


class TestProviderWireUp:
    def _provider_source(self, name: str) -> str:
        return (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / f"{name}.py"
        ).read_text(encoding="utf-8")

    def test_claude_provider_imports_bridge(self):
        source = self._provider_source("providers")
        assert (
            "epistemic_budget_provider_bridge" in source
        ), (
            "providers.py must lazy-import "
            "epistemic_budget_provider_bridge — Slice 5 wire-up "
            "regressed"
        )
        assert "attach_to_provider_run" in source
        assert "per_round_observer" in source

    def test_dw_provider_imports_bridge(self):
        source = self._provider_source("doubleword_provider")
        assert (
            "epistemic_budget_provider_bridge" in source
        ), (
            "doubleword_provider.py must lazy-import "
            "epistemic_budget_provider_bridge — Slice 5 wire-up "
            "regressed"
        )
        assert "attach_to_provider_run" in source
        assert "per_round_observer" in source

    def test_both_providers_close_op_in_finally(self):
        """Every wire-up MUST pair attach_to_provider_run with
        a close_op call (idempotent op cleanup)."""
        for prov in ("providers", "doubleword_provider"):
            source = self._provider_source(prov)
            assert "_eb_close" in source or "close_op" in source, (
                f"{prov}.py is missing close_op pairing — "
                f"tracker entries will leak across ops"
            )


# ---------------------------------------------------------------------------
# § 6 — tool_executor.run() per_round_observer parameter
# ---------------------------------------------------------------------------


class TestToolExecutorObserverParam:
    def test_parameter_signature_present(self):
        """Pyright-independent: read source + verify param name
        is in the signature."""
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "tool_executor.py"
        ).read_text(encoding="utf-8")
        # Parameter declared in the run() signature
        assert (
            "per_round_observer: Optional[Callable[[int]"
            in source
        )
        # Awaited at the round boundary
        assert "await per_round_observer(round_index)" in source

    def test_default_none_preserves_pre_graduation_behavior(
        self,
    ):
        """The default ``None`` for per_round_observer must
        remain — without it, byte-identical pre-graduation
        provider behavior is lost when the master flag is
        off."""
        source = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "tool_executor.py"
        ).read_text(encoding="utf-8")
        assert (
            "per_round_observer: Optional[Callable[[int], "
            "Awaitable[Any]]] = None" in source
        )
