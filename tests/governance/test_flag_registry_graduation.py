"""Slice 4 graduation pins for FlagRegistry + /help dispatcher arc.

Groups mirror the DirectionInferrer graduation pattern:
  A. Authority invariants (grep-enforced zero-import)
  B. Behavioral invariants (master off/on, typo thresholds, duplicate
     registration, thread safety, filter accuracy, JSON export stability)
  C. Graduation-specific invariants (default literal, seed coverage,
     env defaults)
  C'. Docstring bit-rot guards
  D. Schema version discipline
  E. Integration invariants (registry → /help → GET → SSE all wired)
  F. Full-revert matrix (single flag kill on all 4 surfaces)
  G. CLAUDE.md doc guard

Compromising any of these pins is a regression. Do not edit to green.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FLAG_REGISTRY_SCHEMA_VERSION,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
    ensure_seeded,
    get_default_registry,
    is_enabled,
    levenshtein_distance,
    reset_default_registry,
    typo_max_distance,
    typo_warn_enabled,
)
from backend.core.ouroboros.governance.flag_registry_seed import SEED_SPECS
from backend.core.ouroboros.governance.help_dispatcher import (
    dispatch_help_command,
    dispatcher_enabled,
    get_default_verb_registry,
    reset_default_verb_registry,
)


_REPO_ROOT = Path(subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True, check=True,
).stdout.strip())


_ARC_FILES = (
    "backend/core/ouroboros/governance/flag_registry.py",
    "backend/core/ouroboros/governance/flag_registry_seed.py",
    "backend/core/ouroboros/governance/help_dispatcher.py",
)


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if (key.startswith("JARVIS_FLAG_REGISTRY")
                or key.startswith("JARVIS_HELP_DISPATCHER")
                or key.startswith("JARVIS_FLAG_TYPO")
                or key.startswith("JARVIS_IDE_")):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_verb_registry()
    yield
    reset_default_registry()
    reset_default_verb_registry()


# ===========================================================================
# A. AUTHORITY INVARIANTS (6 pins)
# ===========================================================================


class TestGraduation_A_AuthorityInvariants:

    @pytest.mark.parametrize("relpath", list(_ARC_FILES))
    def test_arc_file_authority_free(self, relpath: str):
        src = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"{relpath} authority violations: {bad}"

    def test_flag_get_handlers_authority_free(self):
        """The 4 flag GET handler methods we added to ide_observability.py
        must not reference authority modules in their bodies."""
        ide_path = _REPO_ROOT / "backend/core/ouroboros/governance/ide_observability.py"
        src = ide_path.read_text(encoding="utf-8")
        for handler in (
            "_handle_flags_list", "_handle_flag_detail",
            "_handle_flags_unregistered", "_handle_verbs_list",
        ):
            assert f"async def {handler}" in src, f"handler missing: {handler}"
            idx = src.index(f"async def {handler}")
            window = src[idx:idx + 4096]
            for forbidden in _AUTHORITY_MODULES:
                assert f".{forbidden} " not in window, (
                    f"{handler} references authority module {forbidden}"
                )

    def test_sse_bridges_authority_free(self):
        """publish_flag_typo_event, publish_flag_registered_event, and
        bridge_flag_registry_to_broker must not reference authority
        modules."""
        stream_path = _REPO_ROOT / "backend/core/ouroboros/governance/ide_observability_stream.py"
        src = stream_path.read_text(encoding="utf-8")
        for fn in (
            "publish_flag_typo_event", "publish_flag_registered_event",
            "bridge_flag_registry_to_broker",
        ):
            assert f"def {fn}" in src, f"function missing: {fn}"
            idx = src.index(f"def {fn}")
            window = src[idx:idx + 4096]
            for forbidden in _AUTHORITY_MODULES:
                assert f".{forbidden} " not in window, (
                    f"{fn} references authority module {forbidden}"
                )


# ===========================================================================
# B. BEHAVIORAL INVARIANTS (10 pins)
# ===========================================================================


class TestGraduation_B_BehavioralInvariants:

    def test_master_off_disables_dispatcher(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        assert is_enabled() is False
        assert dispatcher_enabled() is False
        r = dispatch_help_command("/help flags")
        assert r.ok is False

    def test_master_off_still_serves_help(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        r = dispatch_help_command("/help help")
        assert r.ok

    def test_duplicate_registration_override_by_default(self):
        r = FlagRegistry()
        spec = FlagSpec(
            name="JARVIS_X", type=FlagType.BOOL, default=False,
            description="desc", category=Category.EXPERIMENTAL,
            source_file="t.py",
        )
        r.register(spec)
        r.register(spec)  # Override-in-place — must not raise

    def test_duplicate_registration_strict_mode_raises(self):
        r = FlagRegistry()
        spec = FlagSpec(
            name="JARVIS_X", type=FlagType.BOOL, default=False,
            description="desc", category=Category.EXPERIMENTAL,
            source_file="t.py",
        )
        r.register(spec)
        with pytest.raises(ValueError):
            r.register(spec, override=False)

    def test_typed_accessor_malformed_fallback(self, monkeypatch):
        r = FlagRegistry()
        r.register(FlagSpec(
            name="JARVIS_N", type=FlagType.INT, default=42,
            description="int", category=Category.CAPACITY,
            source_file="t.py",
        ))
        monkeypatch.setenv("JARVIS_N", "banana")
        assert r.get_int("JARVIS_N") == 42

    def test_levenshtein_threshold_enforced(self, monkeypatch):
        r = FlagRegistry()
        r.register(FlagSpec(
            name="JARVIS_POSTURE_ENABLED", type=FlagType.BOOL,
            default=True, description="t", category=Category.SAFETY,
            source_file="t.py",
        ))
        monkeypatch.setenv("JARVIS_FLAG_TYPO_MAX_DISTANCE", "1")
        # Distance 2 → no suggestion under threshold=1
        suggestions = r.suggest_similar("JARVIS_POSTURX_ENABL_EDX")
        assert suggestions == []

    def test_levenshtein_symmetric(self):
        assert levenshtein_distance("abc", "bac") == levenshtein_distance("bac", "abc")

    def test_filter_category_exclusive(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        r = ensure_seeded()
        safety = r.list_by_category(Category.SAFETY)
        assert all(s.category is Category.SAFETY for s in safety)
        timing = r.list_by_category(Category.TIMING)
        assert not set(s.name for s in safety) & set(s.name for s in timing)

    def test_thread_safe_concurrent_register(self):
        import threading
        r = FlagRegistry()
        errors = []

        def worker(i):
            try:
                for j in range(10):
                    r.register(FlagSpec(
                        name=f"JARVIS_T{i}_{j}", type=FlagType.BOOL,
                        default=False, description="t",
                        category=Category.EXPERIMENTAL, source_file="t.py",
                    ))
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        assert len(r.list_all()) == 40

    def test_json_export_stability(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        r = ensure_seeded()
        payload = json.loads(r.to_json())
        # Export is sorted → deterministic
        names = [f["name"] for f in payload["flags"]]
        assert names == sorted(names)


# ===========================================================================
# C. GRADUATION-SPECIFIC INVARIANTS (8 pins)
# ===========================================================================


class TestGraduation_C_SpecificInvariants:

    def test_default_is_literal_python_true(self):
        """Catch drift to "true" string / 1 int / True stringified in
        code review."""
        from backend.core.ouroboros.governance import flag_registry
        src = inspect.getsource(flag_registry.is_enabled)
        assert 'JARVIS_FLAG_REGISTRY_ENABLED", True' in src, (
            "is_enabled() default must be Python literal True post-graduation"
        )

    def test_is_enabled_default_true(self):
        assert is_enabled() is True

    def test_seed_at_least_50_flags(self):
        assert len(SEED_SPECS) >= 50, (
            f"graduation pin: seed count must be ≥50, got {len(SEED_SPECS)}"
        )

    def test_seed_has_all_9_direction_inferrer_flags(self):
        names = {s.name for s in SEED_SPECS}
        expected = {
            "JARVIS_DIRECTION_INFERRER_ENABLED",
            "JARVIS_POSTURE_PROMPT_INJECTION_ENABLED",
            "JARVIS_POSTURE_OBSERVER_INTERVAL_S",
            "JARVIS_POSTURE_HYSTERESIS_WINDOW_S",
            "JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS",
            "JARVIS_POSTURE_CONFIDENCE_FLOOR",
            "JARVIS_POSTURE_OVERRIDE_MAX_H",
            "JARVIS_POSTURE_HISTORY_SIZE",
            "JARVIS_POSTURE_WEIGHTS_OVERRIDE",
        }
        assert expected <= names, f"missing: {expected - names}"

    def test_seed_covers_all_8_categories(self):
        categories = {s.category for s in SEED_SPECS}
        assert categories == set(Category)

    def test_seed_reaches_all_4_postures_in_relevance(self):
        postures: set = set()
        for s in SEED_SPECS:
            postures.update(s.posture_relevance.keys())
        expected = {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}
        assert expected <= postures

    def test_every_seed_spec_has_non_empty_source_file(self):
        for s in SEED_SPECS:
            assert s.source_file, f"{s.name} missing source_file"

    def test_every_seed_spec_has_description_over_10_chars(self):
        for s in SEED_SPECS:
            assert len(s.description) > 10, f"{s.name} description too short"

    def test_typo_max_distance_default_3(self):
        assert typo_max_distance() == 3


# ===========================================================================
# C'. DOCSTRING BIT-ROT GUARDS (3 pins)
# ===========================================================================


class TestGraduation_C_DocstringGuards:

    def test_registry_docstring_cites_authority_free(self):
        from backend.core.ouroboros.governance import flag_registry
        doc = flag_registry.__doc__ or ""
        assert ("authority" in doc.lower() and
                ("free" in doc.lower() or "zero" in doc.lower()))

    def test_registry_docstring_cites_tier_0(self):
        from backend.core.ouroboros.governance import flag_registry
        doc = flag_registry.__doc__ or ""
        assert "Tier 0" in doc

    def test_help_dispatcher_docstring_cites_read_only(self):
        from backend.core.ouroboros.governance import help_dispatcher
        doc = help_dispatcher.__doc__ or ""
        assert "read-only" in doc.lower()


# ===========================================================================
# D. SCHEMA VERSION DISCIPLINE (3 pins)
# ===========================================================================


class TestGraduation_D_SchemaDiscipline:

    def test_registry_schema_literal_1_0(self):
        assert FLAG_REGISTRY_SCHEMA_VERSION == "1.0"

    def test_json_export_carries_schema(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        payload = json.loads(ensure_seeded().to_json())
        assert payload["schema_version"] == "1.0"

    def test_sse_frame_schema_version_literal(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            STREAM_SCHEMA_VERSION, publish_flag_typo_event,
            reset_default_broker,
        )
        reset_default_broker()
        publish_flag_typo_event("JARVIS_X", "JARVIS_Y", 1)
        assert STREAM_SCHEMA_VERSION == "1.0"


# ===========================================================================
# E. INTEGRATION INVARIANTS (3 pins)
# ===========================================================================


class TestGraduation_E_IntegrationInvariants:

    def test_report_typos_publishes_sse(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_TYPO_DETECTED,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        registry = ensure_seeded()
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVR_INTERVAL_S", "600")
        before = broker.published_count
        registry.report_typos()
        assert broker.published_count > before
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_FLAG_TYPO_DETECTED in types

    def test_bridge_publishes_on_new_registration(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_FLAG_REGISTERED,
            bridge_flag_registry_to_broker,
            get_default_broker, reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        r = FlagRegistry()
        bridge_flag_registry_to_broker(registry=r)
        r.register(FlagSpec(
            name="JARVIS_BRIDGE_GRAD_TEST", type=FlagType.BOOL,
            default=False, description="bridge grad pin",
            category=Category.EXPERIMENTAL, source_file="t.py",
        ))
        types = [e.event_type for e in broker._history]
        assert EVENT_TYPE_FLAG_REGISTERED in types

    @pytest.mark.asyncio
    async def test_get_surface_double_gated(self, monkeypatch):
        """GET /observability/flags requires BOTH ide_observability
        AND flag_registry masters; flipping either → 403."""
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()

        def make_req():
            return SimpleNamespace(
                remote="127.0.0.1",
                headers={"Origin": "http://localhost:1234"},
                query={}, match_info={},
            )

        # Gate 1 off
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
        resp = await router._handle_flags_list(make_req())
        assert resp.status == 403
        # Gate 1 on, Gate 2 off
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        resp = await router._handle_flags_list(make_req())
        assert resp.status == 403
        # Both on
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        resp = await router._handle_flags_list(make_req())
        assert resp.status == 200


# ===========================================================================
# F. FULL-REVERT MATRIX (2 pins — the graduation centerpiece)
# ===========================================================================


class TestGraduation_F_FullRevertMatrix:

    @pytest.mark.asyncio
    async def test_full_revert_matrix_lockstep(self, monkeypatch):
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        ensure_seeded()
        router = IDEObservabilityRouter()

        def make_req():
            return SimpleNamespace(
                remote="127.0.0.1",
                headers={"Origin": "http://localhost:1234"},
                query={}, match_info={},
            )

        # GRADUATED state (master true by default) — all surfaces active
        r_repl = dispatch_help_command("/help flags")
        assert r_repl.ok
        resp = await router._handle_flags_list(make_req())
        assert resp.status == 200
        resp = await router._handle_verbs_list(make_req())
        assert resp.status == 200
        # Typo warning enabled
        assert typo_warn_enabled() is True

        # REVERT: single env flip kills all surfaces
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")

        r_repl_off = dispatch_help_command("/help flags")
        assert r_repl_off.ok is False

        resp = await router._handle_flags_list(make_req())
        assert resp.status == 403
        resp = await router._handle_verbs_list(make_req())
        assert resp.status == 403
        resp = await router._handle_flags_unregistered(make_req())
        assert resp.status == 403

        assert typo_warn_enabled() is False

    def test_help_help_still_works_master_off(self, monkeypatch):
        """Discoverability exception — /help help must remain accessible
        so operators can find the flag name."""
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        r = dispatch_help_command("/help help")
        assert r.ok
        assert "JARVIS_FLAG_REGISTRY_ENABLED" in r.text


# ===========================================================================
# G. CLAUDE.md DOC GUARD (3 pins)
# ===========================================================================


class TestGraduation_G_ClaudeMdEntry:

    def test_claude_md_mentions_flag_registry(self):
        claude_md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "FlagRegistry" in claude_md

    def test_claude_md_mentions_help_dispatcher(self):
        claude_md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "/help" in claude_md

    def test_claude_md_mentions_master_flag(self):
        claude_md = (_REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        assert "JARVIS_FLAG_REGISTRY_ENABLED" in claude_md
