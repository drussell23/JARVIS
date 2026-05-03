"""ClusterIntelligence-CrossSession Slice 5 -- graduation regression spine.

Pins:
  * 4 master flag defaults flipped false -> true (Slice 1 + 2 + 3 + 4)
  * 4 modules own register_flags + 3 own register_shipped_invariants
  * FlagRegistry discovery seeds all 12 arc flags (3 + 2 + 4 + 3)
  * shipped_code_invariants discovery seeds 3 AST pins; each
    passes against live source
  * EVENT_TYPE_DOMAIN_MAP_UPDATED constant + publish helper exist
  * cascade observer fires SSE on every successful persist
  * Operator escape hatches preserved
  * E2E at graduated defaults: cluster_coverage envelope ->
    successful op -> DomainMap entry -> next-emission envelope
    description carries prior context
"""
from __future__ import annotations

import importlib
import json
import pathlib
from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.cluster_exploration_cascade_observer import (  # noqa: E501
    cascade_observer_enabled,
    observe_cluster_coverage_completion,
    render_prior_context_block,
)
from backend.core.ouroboros.governance.domain_map_memory import (
    DomainMapStore,
    domain_map_enabled,
    reset_default_store,
)
from backend.core.ouroboros.governance.intake.sensors.proactive_exploration_sensor import (  # noqa: E501
    _use_representative_paths_enabled,
)
from backend.core.ouroboros.governance.semantic_index import (
    _representative_paths_enabled,
)


# ---------------------------------------------------------------------------
# Graduated defaults
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
        "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
        "JARVIS_DOMAIN_MAP_ENABLED",
        "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED",
        "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    reset_default_store()


class TestGraduatedDefaults:
    def test_slice1_master_default_true(self):
        assert _representative_paths_enabled() is True

    def test_slice2_subflag_default_true(self):
        assert _use_representative_paths_enabled() is True

    def test_slice3_master_default_true(self):
        assert domain_map_enabled() is True

    def test_slice4_master_default_true(self):
        assert cascade_observer_enabled() is True


# ---------------------------------------------------------------------------
# Module-owned registration callables
# ---------------------------------------------------------------------------


_MODULES_WITH_FLAGS = (
    "backend.core.ouroboros.governance.semantic_index",
    "backend.core.ouroboros.governance.intake.sensors."
    "proactive_exploration_sensor",
    "backend.core.ouroboros.governance.domain_map_memory",
    "backend.core.ouroboros.governance."
    "cluster_exploration_cascade_observer",
)

_MODULES_WITH_INVARIANTS = (
    "backend.core.ouroboros.governance.semantic_index",
    "backend.core.ouroboros.governance.domain_map_memory",
    "backend.core.ouroboros.governance."
    "cluster_exploration_cascade_observer",
)


class TestModuleOwnedRegistration:
    @pytest.mark.parametrize("modname", _MODULES_WITH_FLAGS)
    def test_register_flags_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_flags", None)
        assert callable(fn), (
            f"{modname} missing module-owned register_flags"
        )

    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_register_shipped_invariants_callable(self, modname):
        mod = importlib.import_module(modname)
        fn = getattr(mod, "register_shipped_invariants", None)
        assert callable(fn), (
            f"{modname} missing register_shipped_invariants"
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeding
# ---------------------------------------------------------------------------


class TestFlagRegistrySeeding:
    def _empty_registry(self):
        from backend.core.ouroboros.governance.flag_registry import (
            FlagRegistry,
        )
        return FlagRegistry()

    def _all_flag_names(self) -> set:
        registry = self._empty_registry()
        for modname in _MODULES_WITH_FLAGS:
            mod = importlib.import_module(modname)
            mod.register_flags(registry)
        return {spec.name for spec in registry.list_all()}

    def test_all_12_flags_seeded(self):
        names = self._all_flag_names()
        expected = {
            # semantic_index Slice 1 (3)
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "JARVIS_CLUSTER_REPRESENTATIVE_PATH_K",
            "JARVIS_SEMANTIC_GIT_PATH_SCAN_TIMEOUT_S",
            # proactive_exploration_sensor Slice 2 (2)
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "JARVIS_EXPLORATION_REPRESENTATIVE_PATH_CAP",
            # domain_map_memory Slice 3 (4)
            "JARVIS_DOMAIN_MAP_ENABLED",
            "JARVIS_DOMAIN_MAP_FILES_CAP",
            "JARVIS_DOMAIN_MAP_ROLE_MAX_CHARS",
            "JARVIS_DOMAIN_MAP_LOCK_TIMEOUT_S",
            # cluster_exploration_cascade_observer Slice 4 (3)
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED",
            "JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED",
            "JARVIS_CLUSTER_CASCADE_PRIOR_CONTEXT_MAX_FILES",
        }
        missing = expected - names
        assert not missing, f"missing seeds: {sorted(missing)}"

    @pytest.mark.parametrize("master_flag, expected_default", [
        ("JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED", True),
        ("JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS", True),
        ("JARVIS_DOMAIN_MAP_ENABLED", True),
        ("JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED", True),
        # Auto-role stays default-false (cost commitment escape hatch)
        ("JARVIS_DOMAIN_MAP_AUTO_ROLE_ENABLED", False),
    ])
    def test_master_flag_default(
        self, master_flag, expected_default,
    ):
        registry = self._empty_registry()
        for modname in _MODULES_WITH_FLAGS:
            mod = importlib.import_module(modname)
            mod.register_flags(registry)
        spec = next(
            (s for s in registry.list_all() if s.name == master_flag),
            None,
        )
        assert spec is not None, f"{master_flag} not registered"
        assert spec.default is expected_default


# ---------------------------------------------------------------------------
# Shipped invariants
# ---------------------------------------------------------------------------


class TestShippedInvariants:
    @pytest.mark.parametrize("modname", _MODULES_WITH_INVARIANTS)
    def test_invariants_returned_as_list(self, modname):
        mod = importlib.import_module(modname)
        invariants = mod.register_shipped_invariants()
        assert isinstance(invariants, list)
        assert len(invariants) >= 1

    def test_total_pin_count_meets_target(self):
        total = 0
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            total += len(mod.register_shipped_invariants())
        # semantic_index: 1 (slice1 helpers presence)
        # domain_map_memory: 1 (authority + frozen contract)
        # cluster_exploration_cascade_observer: 1 (authority)
        # = 3 pins
        assert total >= 3

    def test_each_pin_passes_against_live_source(self):
        """Every pin's validate() returns no violations against
        its target source. Load-bearing -- ensures the AST
        contracts the pins check actually match what's shipped."""
        import ast as _ast

        repo_root = pathlib.Path(__file__).resolve().parents[2]
        for modname in _MODULES_WITH_INVARIANTS:
            mod = importlib.import_module(modname)
            for inv in mod.register_shipped_invariants():
                target_path = repo_root / inv.target_file
                source = target_path.read_text()
                tree = _ast.parse(source)
                violations = inv.validate(tree, source)
                assert violations == (), (
                    f"{inv.invariant_name!r} flagged violations: "
                    f"{violations}"
                )


# ---------------------------------------------------------------------------
# SSE event surface
# ---------------------------------------------------------------------------


class TestSSEEvent:
    def test_event_constant_defined(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_DOMAIN_MAP_UPDATED,
        )
        assert EVENT_TYPE_DOMAIN_MAP_UPDATED == "domain_map_updated"

    def test_publish_helper_exists(self):
        from backend.core.ouroboros.governance import (
            ide_observability_stream as mod,
        )
        assert hasattr(mod, "publish_domain_map_update")
        assert callable(mod.publish_domain_map_update)

    def test_publish_helper_returns_none_when_stream_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_domain_map_update,
        )
        out = publish_domain_map_update(
            centroid_hash8="abc12345",
            cluster_id=3,
            theme_label="x",
        )
        assert out is None

    def test_publish_helper_never_raises_on_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            publish_domain_map_update,
        )
        # Empty hash, all-defaults
        publish_domain_map_update(centroid_hash8="")
        # Negative cluster_id, garbage role
        publish_domain_map_update(
            centroid_hash8="x", cluster_id=-1,
            theme_label="" * 1000,
        )


# ---------------------------------------------------------------------------
# Cascade observer fires SSE on every successful persist
# ---------------------------------------------------------------------------


class TestCascadeSSEPublish:
    @pytest.mark.asyncio
    async def test_cascade_fires_publish_on_record(
        self, tmp_path, monkeypatch,
    ):
        calls: List[Dict[str, Any]] = []

        def _spy(**kwargs):
            calls.append(kwargs)
            return "evt-1"

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_domain_map_update",
            _spy,
        )

        store = DomainMapStore(project_root=tmp_path)
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": "abc12345",
            "cluster_id": 3,
            "theme_label": "voice biometric",
        })
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=evidence,
            touched_files=("voice/auth.py",),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        assert len(calls) == 1
        assert calls[0]["centroid_hash8"] == "abc12345"
        assert calls[0]["cluster_id"] == 3
        assert calls[0]["discovered_files_count"] == 1

    @pytest.mark.asyncio
    async def test_publish_failure_does_not_break_cascade(
        self, tmp_path, monkeypatch,
    ):
        def _explode(**_):
            raise RuntimeError("publish boom")

        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_domain_map_update",
            _explode,
        )

        store = DomainMapStore(project_root=tmp_path)
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": "abc12345",
        })
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=evidence,
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        # Persistence still happened despite SSE blowing up.
        assert out is not None


# ---------------------------------------------------------------------------
# E2E at graduated defaults
# ---------------------------------------------------------------------------


class TestGraduatedEndToEnd:
    @pytest.mark.asyncio
    async def test_full_loop_at_default_env(
        self, tmp_path, monkeypatch,
    ):
        """At graduated defaults (no env vars set), a successful
        cluster_coverage op persists into DomainMap, and a
        subsequent render_prior_context_block on the same hash
        returns the prior-context block."""
        # Stub publish to avoid SSE side-effects
        monkeypatch.setattr(
            "backend.core.ouroboros.governance."
            "ide_observability_stream.publish_domain_map_update",
            lambda **kw: None,
        )

        store = DomainMapStore(project_root=tmp_path)
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": "ee990033",
            "cluster_id": 7,
            "theme_label": "ghost hands UI automation",
        })
        # First exploration completes successfully
        out = await observe_cluster_coverage_completion(
            op_id="op-first",
            intake_evidence_json=evidence,
            touched_files=("ghost_hands/foo.py", "ghost_hands/bar.py"),
            verify_passed=True,
            store=store,
        )
        assert out is not None
        assert out.exploration_count == 1

        # Next-session render brings back prior context
        block = render_prior_context_block(
            "ee990033", store=store,
        )
        assert "Previously explored" in block
        assert "count=1" in block
        assert "ghost_hands/foo.py" in block
        assert "ghost_hands/bar.py" in block

        # Second exploration on the same cluster (different files)
        out2 = await observe_cluster_coverage_completion(
            op_id="op-second",
            intake_evidence_json=evidence,
            touched_files=("ghost_hands/baz.py",),
            verify_passed=True,
            store=store,
        )
        assert out2.exploration_count == 2
        # Files merged across explorations
        assert "ghost_hands/foo.py" in out2.discovered_files
        assert "ghost_hands/bar.py" in out2.discovered_files
        assert "ghost_hands/baz.py" in out2.discovered_files

        # Render now reflects the merged state
        block2 = render_prior_context_block(
            "ee990033", store=store,
        )
        assert "count=2" in block2
        assert "ghost_hands/baz.py" in block2

    @pytest.mark.asyncio
    async def test_master_off_overrides_graduated_default(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_DOMAIN_MAP_ENABLED", "false")
        store = DomainMapStore(project_root=tmp_path)
        evidence = json.dumps({
            "category": "cluster_coverage",
            "centroid_hash8": "abc12345",
        })
        out = await observe_cluster_coverage_completion(
            op_id="op-1",
            intake_evidence_json=evidence,
            touched_files=("a.py",),
            verify_passed=True,
            store=store,
        )
        # DomainMap off -> cascade short-circuits
        assert out is None


# ---------------------------------------------------------------------------
# Operator escape hatches
# ---------------------------------------------------------------------------


class TestOperatorEscapeHatches:
    @pytest.mark.parametrize("flag, fn_module, fn_name", [
        (
            "JARVIS_SEMANTIC_INDEX_REPRESENTATIVE_PATHS_ENABLED",
            "backend.core.ouroboros.governance.semantic_index",
            "_representative_paths_enabled",
        ),
        (
            "JARVIS_PROACTIVE_EXPLORATION_USE_REPRESENTATIVE_PATHS",
            "backend.core.ouroboros.governance.intake.sensors."
            "proactive_exploration_sensor",
            "_use_representative_paths_enabled",
        ),
        (
            "JARVIS_DOMAIN_MAP_ENABLED",
            "backend.core.ouroboros.governance.domain_map_memory",
            "domain_map_enabled",
        ),
        (
            "JARVIS_CLUSTER_CASCADE_OBSERVER_ENABLED",
            "backend.core.ouroboros.governance."
            "cluster_exploration_cascade_observer",
            "cascade_observer_enabled",
        ),
    ])
    def test_master_off_overrides_default(
        self, monkeypatch, flag, fn_module, fn_name,
    ):
        monkeypatch.setenv(flag, "false")
        mod = importlib.import_module(fn_module)
        fn = getattr(mod, fn_name)
        assert fn() is False
