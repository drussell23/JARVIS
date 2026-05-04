"""Slice 1 regression spine for FlagRegistry + seed registrations.

Pins carried into Slice 4 graduation. Structured as:
  - Registration + lookup shapes
  - Typed accessors (bool / int / float / str / json) + malformed fallback
  - Filters (category / posture / search)
  - Levenshtein typo detection
  - Unregistered env scan
  - JSON export round-trip + schema
  - Thread-safety (bounded stress)
  - Seed-specific pins
  - Authority invariant (grep)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

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
from backend.core.ouroboros.governance.flag_registry_seed import (
    SEED_SPECS,
    seed_default_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_FLAG_REGISTRY") or key.startswith("JARVIS_FLAG_TYPO"):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    yield
    reset_default_registry()


@pytest.fixture
def registry() -> FlagRegistry:
    return FlagRegistry()


def _sample_spec(
    name: str = "JARVIS_SAMPLE_FLAG",
    type: FlagType = FlagType.BOOL,
    default=False,
    category: Category = Category.SAFETY,
    posture_relevance=None,
) -> FlagSpec:
    return FlagSpec(
        name=name, type=type, default=default, description="test spec",
        category=category, source_file="test.py", example="true",
        since="v1.0",
        posture_relevance=posture_relevance or {},
    )


# ---------------------------------------------------------------------------
# Registration + lookup
# ---------------------------------------------------------------------------


class TestRegistrationLookup:

    def test_register_then_get_spec(self, registry: FlagRegistry):
        spec = _sample_spec()
        registry.register(spec)
        assert registry.get_spec("JARVIS_SAMPLE_FLAG") is spec

    def test_get_spec_missing_returns_none(self, registry: FlagRegistry):
        assert registry.get_spec("JARVIS_MISSING") is None

    def test_register_rejects_non_flagspec(self, registry: FlagRegistry):
        with pytest.raises(TypeError):
            registry.register("not-a-spec")  # type: ignore[arg-type]

    def test_duplicate_registration_default_override(self, registry: FlagRegistry):
        s1 = _sample_spec(default=False)
        s2 = _sample_spec(default=True)
        registry.register(s1)
        registry.register(s2)  # override=True default
        assert registry.get_spec(s1.name).default is True

    def test_duplicate_registration_with_override_false_raises(self, registry: FlagRegistry):
        spec = _sample_spec()
        registry.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(spec, override=False)

    def test_list_all_sorted_alphabetically(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_B"))
        registry.register(_sample_spec("JARVIS_A"))
        registry.register(_sample_spec("JARVIS_C"))
        names = [s.name for s in registry.list_all()]
        assert names == ["JARVIS_A", "JARVIS_B", "JARVIS_C"]

    def test_bulk_register(self, registry: FlagRegistry):
        specs = [_sample_spec(f"JARVIS_X{i}") for i in range(5)]
        registry.bulk_register(specs)
        assert len(registry.list_all()) == 5


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:

    def test_list_by_category(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_A", category=Category.SAFETY))
        registry.register(_sample_spec("JARVIS_B", category=Category.TIMING))
        registry.register(_sample_spec("JARVIS_C", category=Category.SAFETY))
        safety = [s.name for s in registry.list_by_category(Category.SAFETY)]
        assert safety == ["JARVIS_A", "JARVIS_C"]

    def test_find_matches_name_substring(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_POSTURE_OBSERVER"))
        registry.register(_sample_spec("JARVIS_IDE_OBS"))
        hits = [s.name for s in registry.find("obs")]
        assert "JARVIS_POSTURE_OBSERVER" in hits
        assert "JARVIS_IDE_OBS" in hits

    def test_find_case_insensitive(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_POSTURE"))
        assert registry.find("POSTURE") and registry.find("posture")

    def test_find_empty_query_returns_empty(self, registry: FlagRegistry):
        registry.register(_sample_spec())
        assert registry.find("") == []

    def test_relevant_to_posture(self, registry: FlagRegistry):
        registry.register(_sample_spec(
            "JARVIS_HARDEN_FLAG",
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ))
        registry.register(_sample_spec(
            "JARVIS_EXPLORE_FLAG",
            posture_relevance={"EXPLORE": Relevance.CRITICAL},
        ))
        registry.register(_sample_spec(
            "JARVIS_IGNORED_FLAG",
            posture_relevance={"HARDEN": Relevance.IGNORED},
        ))
        harden = [s.name for s in registry.relevant_to_posture("HARDEN")]
        assert "JARVIS_HARDEN_FLAG" in harden
        assert "JARVIS_IGNORED_FLAG" not in harden  # IGNORED dropped at RELEVANT floor
        assert "JARVIS_EXPLORE_FLAG" not in harden  # different posture

    def test_relevant_to_posture_case_insensitive(self, registry: FlagRegistry):
        registry.register(_sample_spec(
            "JARVIS_F", posture_relevance={"HARDEN": Relevance.CRITICAL},
        ))
        assert registry.relevant_to_posture("harden")
        assert registry.relevant_to_posture(" HARDEN ")


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------


class TestTypedAccessors:

    def test_get_bool_from_env(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_X", "true")
        assert registry.get_bool("JARVIS_X", default=False) is True

    def test_get_bool_uses_spec_default(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_X", default=True))
        # No env → use spec default
        assert registry.get_bool("JARVIS_X") is True

    def test_get_bool_false_literals(self, registry: FlagRegistry, monkeypatch):
        for val in ("false", "0", "no", "off", ""):
            monkeypatch.setenv("JARVIS_Y", val)
            assert registry.get_bool("JARVIS_Y", default=True) is False

    def test_get_int_happy(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_N", "42")
        assert registry.get_int("JARVIS_N", default=0) == 42

    def test_get_int_malformed_falls_back(self, registry: FlagRegistry, monkeypatch):
        registry.register(_sample_spec(
            "JARVIS_N", type=FlagType.INT, default=99,
        ))
        monkeypatch.setenv("JARVIS_N", "not-an-int")
        assert registry.get_int("JARVIS_N") == 99

    def test_get_int_minimum_enforced(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_N", "-5")
        assert registry.get_int("JARVIS_N", default=0, minimum=0) == 0

    def test_get_float_happy(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_F", "3.14")
        assert registry.get_float("JARVIS_F", default=0.0) == pytest.approx(3.14)

    def test_get_float_malformed(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_F", "banana")
        assert registry.get_float("JARVIS_F", default=1.5) == pytest.approx(1.5)

    def test_get_str_from_env(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_S", "hello world")
        assert registry.get_str("JARVIS_S") == "hello world"

    def test_get_str_default_from_spec(self, registry: FlagRegistry):
        registry.register(_sample_spec(
            "JARVIS_S", type=FlagType.STR, default="spec-default",
        ))
        assert registry.get_str("JARVIS_S") == "spec-default"

    def test_get_json_happy(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_J", json.dumps({"a": 1}))
        assert registry.get_json("JARVIS_J") == {"a": 1}

    def test_get_json_malformed_fallback(self, registry: FlagRegistry, monkeypatch):
        registry.register(_sample_spec(
            "JARVIS_J", type=FlagType.JSON, default={"safe": True},
        ))
        monkeypatch.setenv("JARVIS_J", "{not json")
        assert registry.get_json("JARVIS_J") == {"safe": True}

    def test_accessor_records_read(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_READ_ME", default=True))
        registry.get_bool("JARVIS_READ_ME")
        stats = registry.stats()
        assert stats["read_count"] >= 1


# ---------------------------------------------------------------------------
# Levenshtein + typo detection
# ---------------------------------------------------------------------------


class TestLevenshtein:

    def test_levenshtein_zero_on_equal(self):
        assert levenshtein_distance("JARVIS", "JARVIS") == 0

    def test_levenshtein_one_substitution(self):
        assert levenshtein_distance("POSTURE", "PASTURE") == 1

    def test_levenshtein_insertion(self):
        assert levenshtein_distance("POSTUR", "POSTURE") == 1

    def test_levenshtein_symmetric(self):
        assert levenshtein_distance("abc", "xyzz") == levenshtein_distance("xyzz", "abc")

    def test_levenshtein_empty(self):
        assert levenshtein_distance("", "abc") == 3
        assert levenshtein_distance("abc", "") == 3
        assert levenshtein_distance("", "") == 0


class TestTypoDetection:

    def test_suggest_similar_finds_nearby(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED", default=True))
        suggestions = registry.suggest_similar("JARVIS_POSTUR_ENABLED")
        assert suggestions
        name, dist = suggestions[0]
        assert name == "JARVIS_POSTURE_ENABLED"
        assert dist == 1

    def test_suggest_similar_excludes_exact_match(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_X", default=True))
        assert all(s[0] != "JARVIS_X" for s in registry.suggest_similar("JARVIS_X"))

    def test_suggest_similar_threshold_respected(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED"))
        # Distance > 3 → no suggestion
        result = registry.suggest_similar("JARVIS_TOTALLY_UNRELATED_LONG_NAME",
                                          max_distance=3)
        assert result == []

    def test_suggest_similar_sorts_by_distance(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED"))
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLEDX"))
        hits = registry.suggest_similar("JARVIS_POSTURE_ENABLE", max_distance=3)
        # Nearest first
        assert hits[0][1] <= hits[-1][1]


# ---------------------------------------------------------------------------
# Unregistered env scan
# ---------------------------------------------------------------------------


class TestUnregisteredEnv:

    def test_unregistered_env_lists_jarvis_only(self, registry: FlagRegistry, monkeypatch):
        monkeypatch.setenv("JARVIS_FOO_BAR", "1")
        monkeypatch.setenv("PATH", "/usr/bin")  # noise, not a jarvis flag
        hits = [name for name, _ in registry.unregistered_env()]
        assert "JARVIS_FOO_BAR" in hits
        assert "PATH" not in hits

    def test_unregistered_env_skips_registered(self, registry: FlagRegistry, monkeypatch):
        registry.register(_sample_spec("JARVIS_KNOWN", default=True))
        monkeypatch.setenv("JARVIS_KNOWN", "true")
        names = [name for name, _ in registry.unregistered_env()]
        assert "JARVIS_KNOWN" not in names

    def test_unregistered_env_includes_suggestions(self, registry: FlagRegistry, monkeypatch):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED"))
        monkeypatch.setenv("JARVIS_POSTUR_ENABLED", "true")  # typo
        hits = dict(registry.unregistered_env())
        suggestions = hits.get("JARVIS_POSTUR_ENABLED", [])
        assert suggestions
        assert suggestions[0][0] == "JARVIS_POSTURE_ENABLED"

    def test_report_typos_only_when_warn_enabled(
        self, registry: FlagRegistry, monkeypatch,
    ):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED"))
        monkeypatch.setenv("JARVIS_POSTUR_ENABLED", "1")

        # Master off → warnings silent
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        assert registry.report_typos() == []

        # Master + warn on → reports
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_FLAG_TYPO_WARN_ENABLED", "true")
        emitted = registry.report_typos()
        assert emitted
        assert emitted[0][0] == "JARVIS_POSTUR_ENABLED"

    def test_report_typos_deduplicates_per_session(
        self, registry: FlagRegistry, monkeypatch,
    ):
        registry.register(_sample_spec("JARVIS_POSTURE_ENABLED"))
        monkeypatch.setenv("JARVIS_POSTUR_ENABLED", "1")
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")

        first = registry.report_typos()
        second = registry.report_typos()
        assert first
        assert second == []  # already reported this session


# ---------------------------------------------------------------------------
# Master switch + env helpers
# ---------------------------------------------------------------------------


class TestMasterSwitch:

    def test_is_enabled_default_true_post_graduation(self):
        """Post-Slice-4 graduation: default flipped false → true."""
        assert is_enabled() is True

    def test_is_enabled_true_via_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        assert is_enabled() is True

    def test_typo_warn_requires_master(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "false")
        monkeypatch.setenv("JARVIS_FLAG_TYPO_WARN_ENABLED", "true")
        # Master off silences typo warnings even when sub-gate is on
        assert typo_warn_enabled() is False

    def test_typo_warn_both_flags(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_FLAG_TYPO_WARN_ENABLED", "true")
        assert typo_warn_enabled() is True

    def test_typo_max_distance_default(self):
        assert typo_max_distance() == 3

    def test_typo_max_distance_env(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_TYPO_MAX_DISTANCE", "5")
        assert typo_max_distance() == 5

    def test_typo_max_distance_floors_at_1(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_TYPO_MAX_DISTANCE", "0")
        assert typo_max_distance() == 1


# ---------------------------------------------------------------------------
# JSON export + stats
# ---------------------------------------------------------------------------


class TestExportStats:

    def test_to_json_round_trip(self, registry: FlagRegistry):
        registry.register(_sample_spec(
            "JARVIS_A", category=Category.TIMING,
            posture_relevance={"HARDEN": Relevance.CRITICAL},
        ))
        payload = json.loads(registry.to_json())
        assert payload["schema_version"] == "1.0"
        assert payload["total"] == 1
        flag = payload["flags"][0]
        assert flag["name"] == "JARVIS_A"
        assert flag["category"] == "timing"
        assert flag["posture_relevance"] == {"HARDEN": "critical"}

    def test_stats_counts_rollups(self, registry: FlagRegistry):
        registry.register(_sample_spec("JARVIS_A", category=Category.SAFETY))
        registry.register(_sample_spec("JARVIS_B", category=Category.SAFETY))
        registry.register(_sample_spec("JARVIS_C", category=Category.TIMING))
        stats = registry.stats()
        assert stats["total"] == 3
        assert stats["by_category"]["safety"] == 2
        assert stats["by_category"]["timing"] == 1

    def test_schema_version_literal_1_0(self):
        assert FLAG_REGISTRY_SCHEMA_VERSION == "1.0"


# ---------------------------------------------------------------------------
# Thread safety (bounded stress)
# ---------------------------------------------------------------------------


class TestThreadSafety:

    def test_concurrent_register_and_lookup(self, registry: FlagRegistry):
        errors = []

        def worker(i: int):
            try:
                for j in range(20):
                    spec = _sample_spec(f"JARVIS_T{i}_{j}")
                    registry.register(spec)
                    assert registry.get_spec(spec.name) is not None
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        assert not errors, f"thread-safety violation: {errors}"
        assert len(registry.list_all()) == 8 * 20


# ---------------------------------------------------------------------------
# Singleton + seed
# ---------------------------------------------------------------------------


class TestSingletonAndSeed:

    def test_default_registry_singleton(self):
        r1 = get_default_registry()
        r2 = get_default_registry()
        assert r1 is r2

    def test_reset_drops_singleton(self):
        r1 = get_default_registry()
        reset_default_registry()
        r2 = get_default_registry()
        assert r1 is not r2

    def test_ensure_seeded_installs_specs(self):
        reset_default_registry()
        r = ensure_seeded()
        # All seed specs present. Use >= because seed_default_registry
        # also runs _discover_module_provided_flags (added after the
        # original SEED_SPECS-only contract); the static seed list is
        # the floor, not the ceiling.
        assert len(r.list_all()) >= len(SEED_SPECS)
        # And every static seed spec name MUST be in the registry —
        # discovery never silently drops a SEED_SPECS entry.
        seed_names = {s.name for s in SEED_SPECS}
        registry_names = {s.name for s in r.list_all()}
        assert seed_names.issubset(registry_names)

    def test_ensure_seeded_idempotent(self):
        reset_default_registry()
        ensure_seeded()
        count_after_first = len(get_default_registry().list_all())
        ensure_seeded()  # noop second time
        count_after_second = len(get_default_registry().list_all())
        assert count_after_first == count_after_second


# ---------------------------------------------------------------------------
# Seed content pins — must hold at graduation time
# ---------------------------------------------------------------------------


class TestSeedContentPins:

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
        missing = expected - names
        assert not missing, f"seed missing DirectionInferrer flags: {missing}"

    def test_seed_covers_all_8_categories(self):
        categories = {s.category for s in SEED_SPECS}
        assert categories == set(Category), (
            f"seed missing categories: {set(Category) - categories}"
        )

    def test_seed_reaches_all_4_postures_in_relevance(self):
        postures_seen = set()
        for s in SEED_SPECS:
            postures_seen.update(s.posture_relevance.keys())
        expected = {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}
        assert postures_seen & expected == expected, (
            f"seed posture coverage gap: {expected - postures_seen}"
        )

    def test_every_seed_spec_has_non_empty_source_file(self):
        for s in SEED_SPECS:
            assert s.source_file, f"{s.name} missing source_file"

    def test_every_seed_spec_has_description(self):
        for s in SEED_SPECS:
            assert s.description and len(s.description) > 10, (
                f"{s.name} needs a real description"
            )

    def test_seed_count_at_least_40(self):
        """Slice 4 graduation pin — seed must cover a meaningful surface."""
        assert len(SEED_SPECS) >= 40

    def test_all_seed_names_start_with_JARVIS_(self):
        for s in SEED_SPECS:
            assert s.name.startswith("JARVIS_"), (
                f"{s.name} must use JARVIS_ prefix"
            )


# ---------------------------------------------------------------------------
# Authority invariant (grep-pinned Slice 4)
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


class TestAuthorityInvariant:

    @pytest.mark.parametrize("relpath", [
        "backend/core/ouroboros/governance/flag_registry.py",
        "backend/core/ouroboros/governance/flag_registry_seed.py",
    ])
    def test_arc_file_authority_free(self, relpath: str):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (repo_root / relpath).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        # Skip common false-positives: nothing in the
                        # arc files should reference these modules at all.
                        bad.append((forbidden, line))
        assert not bad, f"{relpath} authority import violations: {bad}"
