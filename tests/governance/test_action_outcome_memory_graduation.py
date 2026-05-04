"""M11 Slice 5 — Graduation regression tests (CLOSES M11).

Pins:
  * Master flag default-TRUE post graduation
  * /outcomes REPL surface (auto-discovered via register_verbs)
  * /outcomes subcommand contracts (top, for-cluster, for-region,
    clear, config, help)
  * HTTP routes mount + read-only contract
  * 5 FlagRegistry seeds installed
  * 4 AST shipped-code-invariants pins HOLD against shipped code
  * SuccessPatternStore façade migration (Decision B3) — both
    legacy local store AND M11 store updated atomically

Mirrors Upgrade 3 Slice 5 graduation test discipline (the
full-arc closure pin set the same canonical surface).
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# § 1 — Master flag graduation
# ---------------------------------------------------------------------------


class TestMasterFlagGraduation:
    def test_default_is_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED",
            raising=False,
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is True

    def test_explicit_false_instant_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            action_outcome_memory_enabled,
        )
        assert action_outcome_memory_enabled() is False


# ---------------------------------------------------------------------------
# § 2 — /outcomes REPL surface
# ---------------------------------------------------------------------------


class TestOutcomesREPL:
    def test_register_verbs_auto_discovers(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.outcomes_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1

    def test_dispatch_help_works_master_off(self, monkeypatch):
        """help bypasses master-flag gate (discoverability)."""
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command("/outcomes help")
        assert result.ok is True
        assert "/outcomes" in result.text

    def test_dispatch_disabled_when_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command("/outcomes top")
        assert result.matched is True
        assert result.ok is False
        assert (
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED"
            in result.text
        )

    def test_dispatch_top(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command("/outcomes top 5")
        assert result.ok is True
        assert "no records yet" in result.text

    def test_dispatch_config(self, monkeypatch):
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command("/outcomes config")
        assert result.ok is True
        assert "polarity_mode" in result.text
        assert "max_records_per_cluster" in result.text

    def test_dispatch_for_region_missing_args(self):
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command(
            "/outcomes for-region",
        )
        assert result.matched is True
        assert result.ok is False
        assert "missing target_files" in result.text

    def test_dispatch_for_cluster_missing_args(self):
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command(
            "/outcomes for-cluster",
        )
        assert result.matched is True
        assert result.ok is False
        assert "missing cluster_id" in result.text

    def test_dispatch_unknown_subcommand(self):
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command(
            "/outcomes nosuch",
        )
        assert result.matched is True
        assert result.ok is False
        assert "unknown subcommand" in result.text.lower()

    def test_dispatch_no_match(self):
        from backend.core.ouroboros.governance.outcomes_repl import (
            dispatch_outcomes_command,
        )
        result = dispatch_outcomes_command("not me")
        assert result.matched is False


# ---------------------------------------------------------------------------
# § 3 — HTTP routes mount + contract
# ---------------------------------------------------------------------------


class TestHTTPRoutes:
    def test_register_routes_mounts_two_endpoints(self):
        from aiohttp import web
        from backend.core.ouroboros.governance.action_outcome_memory_observability import (  # noqa: E501
            register_action_outcome_routes,
        )
        app = web.Application()
        register_action_outcome_routes(app)
        canonical = sorted(
            r.canonical for r in app.router.resources()
        )
        assert "/observability/action-outcomes" in canonical
        assert (
            "/observability/action-outcomes/cluster/{id}"
            in canonical
        )

    @pytest.mark.asyncio
    async def test_overview_503_when_master_off(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.action_outcome_memory_observability import (  # noqa: E501
            _ActionOutcomeRoutesHandler,
        )
        h = _ActionOutcomeRoutesHandler()
        request = SimpleNamespace(query={}, match_info={})
        response = await h.handle_overview(request)
        assert response.status == 503

    @pytest.mark.asyncio
    async def test_overview_200_when_master_on(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR", str(tmp_path),
        )
        from types import SimpleNamespace
        from backend.core.ouroboros.governance.action_outcome_memory_observability import (  # noqa: E501
            _ActionOutcomeRoutesHandler,
        )
        h = _ActionOutcomeRoutesHandler()
        request = SimpleNamespace(query={}, match_info={})
        response = await h.handle_overview(request)
        assert response.status == 200


# ---------------------------------------------------------------------------
# § 4 — FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeds:
    def test_five_seeds_installed(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        m11_seeds = {
            s.name for s in SEED_SPECS
            if "ACTION_OUTCOME" in s.name
        }
        # Exactly 5 per scope
        assert len(m11_seeds) == 5

    def test_master_flag_seed_default_true(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        master = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED"
        )
        assert master.default is True

    def test_polarity_mode_seed_default_balanced(self):
        from backend.core.ouroboros.governance.flag_registry_seed import (  # noqa: E501
            SEED_SPECS,
        )
        polarity = next(
            s for s in SEED_SPECS
            if s.name == "JARVIS_ACTION_OUTCOME_POLARITY_MODE"
        )
        assert polarity.default == "balanced"


# ---------------------------------------------------------------------------
# § 5 — AST shipped-code-invariants pins
# ---------------------------------------------------------------------------


class TestASTPins:
    def test_four_m11_pins_registered(self):
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _REGISTRY,
            _register_seed_invariants,
        )
        _register_seed_invariants()
        m11_pins = {
            k for k in _REGISTRY if "action_outcome" in k
        }
        # Exactly 4 per scope
        assert len(m11_pins) == 4

    def test_all_m11_pins_pass_against_shipped_code(self):
        """The 4 M11 pins MUST hold against the live source.
        If any tripped, the graduation contract regressed."""
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            _register_seed_invariants,
            validate_all,
        )
        _register_seed_invariants()
        violations = validate_all()
        m11_violations = [
            v for v in violations
            if "action_outcome" in v.invariant_name
        ]
        assert m11_violations == [], (
            f"M11 AST pins regressed: "
            f"{[(v.invariant_name, v.violation_text[:80]) for v in m11_violations]}"
        )


# ---------------------------------------------------------------------------
# § 6 — Decision B3 — SuccessPatternStore façade migration
# ---------------------------------------------------------------------------


class TestSuccessPatternStoreFacade:
    def test_legacy_local_store_unchanged(
        self, monkeypatch, tmp_path,
    ):
        """Legacy callers (orchestrator.py) see byte-identical
        local-store behavior post-façade. record_success +
        get_similar_successes still work as before."""
        # Isolate adaptive_learning's persistence dir
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR",
            str(tmp_path / "aom"),
        )
        from backend.core.ouroboros.governance.adaptive_learning import (  # noqa: E501
            SuccessPatternStore,
        )
        store = SuccessPatternStore(
            persistence_dir=tmp_path / "legacy",
        )
        store.record_success(
            domain_key="backend",
            description="Added dataclass for config",
            target_files=("a.py", "b.py"),
            provider="claude",
            approach_summary="add dataclass",
        )
        # Legacy retrieval still works
        results = store.get_similar_successes(
            domain_key="backend",
            target_files=("a.py", "b.py"),
        )
        assert len(results) == 1
        assert results[0].provider == "claude"

    def test_facade_forwards_to_m11(
        self, monkeypatch, tmp_path,
    ):
        """**Load-bearing**: every record_success call ALSO
        produces an M11 ActionOutcomeRecord with
        outcome_kind=APPLIED_VERIFIED. Decision B3: the legacy
        SuccessPatternStore becomes a thin façade — operators
        querying via /outcomes top see the same data legacy
        consumers see via get_similar_successes."""
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_HISTORY_DIR",
            str(tmp_path / "aom"),
        )
        from backend.core.ouroboros.governance.action_outcome_memory import (  # noqa: E501
            OutcomeKind,
            read_all_action_outcomes,
        )
        from backend.core.ouroboros.governance.adaptive_learning import (  # noqa: E501
            SuccessPatternStore,
        )
        store = SuccessPatternStore(
            persistence_dir=tmp_path / "legacy",
        )
        store.record_success(
            domain_key="backend",
            description="Added dataclass for config",
            target_files=("a.py", "b.py"),
            provider="claude",
            approach_summary="add dataclass",
        )
        # M11 store now has the record
        m11_records = read_all_action_outcomes()
        assert len(m11_records) == 1
        assert (
            m11_records[0].outcome_kind
            is OutcomeKind.APPLIED_VERIFIED
        )
        # attempt is normalized lowercase + underscored
        assert (
            m11_records[0].attempted_action_kind
            == "add_dataclass"
        )

    def test_facade_failure_does_not_break_legacy(
        self, monkeypatch, tmp_path,
    ):
        """If the M11 forward path raises (broken module, missing
        env, etc.), the legacy local store STILL works. Façade
        is additive, never breaks existing callers."""
        # Force M11 to disable so its record_action_outcome
        # short-circuits to DISABLED — façade must still complete
        # the legacy local-store write.
        monkeypatch.setenv(
            "JARVIS_ACTION_OUTCOME_MEMORY_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.adaptive_learning import (  # noqa: E501
            SuccessPatternStore,
        )
        store = SuccessPatternStore(
            persistence_dir=tmp_path / "legacy",
        )
        store.record_success(
            domain_key="backend",
            description="x",
            target_files=("a.py",),
            provider="claude",
        )
        # Legacy store has the record
        results = store.get_similar_successes(
            domain_key="backend", target_files=("a.py",),
        )
        assert len(results) == 1


# ---------------------------------------------------------------------------
# § 7 — Bytes pin: event_channel + serpent_flow wirings
# ---------------------------------------------------------------------------


class TestEventChannelMount:
    def test_event_channel_mounts_action_outcome_routes(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "event_channel.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "register_action_outcome_routes" in source
        assert "M11 Slice 5" in source

    def test_serpent_flow_dispatches_outcomes(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "battle_test"
            / "serpent_flow.py"
        )
        source = path.read_text(encoding="utf-8")
        assert "outcomes_repl" in source
        assert '"outcomes", "/outcomes"' in source


# ---------------------------------------------------------------------------
# § 8 — Authority direction (façade-aware: forward only)
# ---------------------------------------------------------------------------


class TestDependencyDirection:
    def test_strategic_direction_imports_aom_lazily(self):
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "strategic_direction.py"
        )
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "action_outcome_memory" in line and (
                line.startswith("from ")
                or line.startswith("import ")
            ):
                pytest.fail(
                    f"strategic_direction must lazy-import "
                    f"action_outcome_memory; found top-level: "
                    f"{line!r}"
                )

    def test_adaptive_learning_imports_aom_lazily(self):
        """The Slice 5.E façade in SuccessPatternStore.record_-
        success uses lazy imports inside the method body.
        adaptive_learning must NOT import action_outcome_memory
        at module scope — that would create a startup
        dependency that breaks if M11 module is absent."""
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / "adaptive_learning.py"
        )
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.lstrip()
            # Module-scope import = no leading whitespace
            if (
                stripped == line  # not indented
                and "action_outcome_memory" in line
                and (
                    line.startswith("from ")
                    or line.startswith("import ")
                )
            ):
                pytest.fail(
                    f"adaptive_learning must lazy-import "
                    f"action_outcome_memory inside "
                    f"record_success(); found module-scope: "
                    f"{line!r}"
                )
