"""Regression spine for v3.7 Phase 2 Phase A — SWE-Bench-Pro dataset loader.

Pins the load-bearing structural invariants for
:mod:`backend.core.ouroboros.governance.swe_bench_pro.dataset_loader`:

* ``LoadOutcome`` 4-value taxonomy bytes-pinned (AST class-body
  walk asserts the value-set strings exactly)
* ``ProblemSpec`` frozen dataclass + symmetric to_dict/from_dict
  round-trip (§33.5)
* Master flag ``JARVIS_SWE_BENCH_PRO_ENABLED`` defaults FALSE per
  §33.1 — load_problem short-circuits to ``(None, MISSING)``
  before any I/O when unset
* Cache write/read round-trip via tmp_path (atomic write contract)
* ``list_cached_problems`` + ``clear_cache`` behaviors
* Local-JSONL acquisition path (PRIMARY) — finds known instance,
  returns LOADED, populates cache
* Garbage tolerance — malformed JSONL line is skipped, not crashed
* AST-pinned authority asymmetry — module MUST NOT import
  policy substrates (orchestrator/iron_gate/repair_engine/etc)
* AST-pinned canonical surface composition — only stdlib +
  optional lazy-import of ``datasets``
* FlagRegistry self-registration via ``register_flags(stub)``
  registers exactly 5 specs (master + cache_path + local_dataset_path
  + hf_dataset + hf_split)
"""
from __future__ import annotations

import ast
import inspect
import json
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import (
    LoadOutcome,
    ProblemSpec,
    SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION,
    cache_dir,
    clear_cache,
    list_cached_problems,
    load_problem,
    swe_bench_pro_enabled,
)
from backend.core.ouroboros.governance.swe_bench_pro import dataset_loader


# Source-bytes for AST pins — read once.
_LOADER_SRC = Path(inspect.getfile(dataset_loader)).read_text(encoding="utf-8")
_LOADER_AST = ast.parse(_LOADER_SRC)


# ===========================================================================
# Closed taxonomy pin — LoadOutcome
# ===========================================================================


def test_load_outcome_taxonomy_is_closed_four_values():
    """Closed-taxonomy pin: LoadOutcome MUST have EXACTLY 4 values
    with the canonical string keys.  Adding a 5th value silently
    breaks downstream consumers; AST pin forces explicit version
    bump on any extension."""
    values = {m.value for m in LoadOutcome}
    assert values == {
        "loaded", "loaded_from_cache", "fetch_failed", "missing",
    }, f"LoadOutcome taxonomy drift; got {sorted(values)}"


def test_load_outcome_class_body_ast_bytes_pinned():
    """AST-walk pin: the enum class body MUST contain exactly 4
    assignments with the canonical names.  Catches reorders /
    silent renames at the source level (not just the runtime
    value set)."""
    cls_node = None
    for node in ast.walk(_LOADER_AST):
        if isinstance(node, ast.ClassDef) and node.name == "LoadOutcome":
            cls_node = node
            break
    assert cls_node is not None, "LoadOutcome class MUST exist in source"
    names = [
        a.targets[0].id for a in cls_node.body
        if isinstance(a, ast.Assign)
        and len(a.targets) == 1
        and isinstance(a.targets[0], ast.Name)
    ]
    assert names == [
        "LOADED", "LOADED_FROM_CACHE", "FETCH_FAILED", "MISSING",
    ], f"LoadOutcome class-body assignments drifted; got {names}"


# ===========================================================================
# Schema version pin
# ===========================================================================


def test_schema_version_constant_pinned():
    """v1 schema version pin.  Bumped on any breaking field change
    (additive changes preserved via ProblemSpec.metadata)."""
    assert SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION == "swe_bench_pro_problem.v1"


# ===========================================================================
# ProblemSpec — frozen + symmetric to_dict/from_dict (§33.5)
# ===========================================================================


def _sample_spec(instance_id: str = "astropy__astropy-12907") -> ProblemSpec:
    return ProblemSpec(
        instance_id=instance_id,
        repo="astropy/astropy",
        repo_url="https://github.com/astropy/astropy.git",
        base_commit="d16bfe05a744909de4b27f5875fe0d4ed41ce607",
        problem_statement="Models with no inputs cannot be evaluated.",
        test_patch="--- a/test.py\n+++ b/test.py\n@@ ...",
        gold_patch="--- a/m.py\n+++ b/m.py\n@@ ...",
        difficulty="medium",
        metadata={"version": "5.0", "hints": ["check separate_with_units"]},
    )


def test_problem_spec_is_frozen():
    spec = _sample_spec()
    with pytest.raises(Exception):
        # Frozen dataclass should reject attribute mutation
        spec.instance_id = "different_id"  # type: ignore[misc]


def test_problem_spec_round_trip():
    spec = _sample_spec()
    data = spec.to_dict()
    rebuilt = ProblemSpec.from_dict(data)
    assert rebuilt == spec


def test_problem_spec_round_trip_via_json():
    """Cache files are JSON-serialized; ensure the spec survives a
    full json.dumps -> json.loads cycle (no non-JSON-encodable
    fields snuck in)."""
    spec = _sample_spec()
    blob = json.dumps(spec.to_dict())
    rebuilt = ProblemSpec.from_dict(json.loads(blob))
    assert rebuilt == spec


def test_problem_spec_from_dict_requires_instance_id():
    """instance_id is the load-bearing identifier; from_dict MUST
    raise ValueError on missing/empty/non-str."""
    with pytest.raises(ValueError):
        ProblemSpec.from_dict({})
    with pytest.raises(ValueError):
        ProblemSpec.from_dict({"instance_id": ""})
    with pytest.raises(ValueError):
        ProblemSpec.from_dict({"instance_id": 123})  # type: ignore


def test_problem_spec_from_dict_tolerates_missing_optionals():
    """Optional fields default to safe values."""
    spec = ProblemSpec.from_dict({"instance_id": "x"})
    assert spec.instance_id == "x"
    assert spec.difficulty == "unknown"
    assert spec.metadata == {}
    assert spec.schema_version == SWE_BENCH_PRO_PROBLEM_SCHEMA_VERSION


def test_problem_spec_metadata_round_trips_unknown_fields():
    """Forward-compat: arbitrary metadata fields preserved verbatim."""
    spec = ProblemSpec.from_dict({
        "instance_id": "x",
        "metadata": {"future_field": "future_value", "n": 42},
    })
    assert spec.metadata == {"future_field": "future_value", "n": 42}


# ===========================================================================
# Master flag (§33.1 default-FALSE)
# ===========================================================================


def test_master_flag_defaults_false(monkeypatch):
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_ENABLED", raising=False)
    assert swe_bench_pro_enabled() is False


def test_master_flag_off_short_circuits_load_problem(monkeypatch):
    """Production-byte-identical contract: when master flag is off,
    load_problem MUST return (None, MISSING) without performing
    any I/O.  Set CACHE_PATH to a non-existent dir to verify
    no disk access occurs."""
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_ENABLED", raising=False)
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH",
        "/tmp/this-path-must-not-be-touched-when-flag-off",
    )
    spec, outcome = load_problem("anything")
    assert spec is None
    assert outcome == LoadOutcome.MISSING


def test_master_flag_on_load_with_no_dataset_returns_missing(
    monkeypatch, tmp_path,
):
    """Flag ON but no dataset + no cache → LoadOutcome.MISSING."""
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH",
        str(tmp_path / "missing-dataset.jsonl"),
    )
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    spec, outcome = load_problem("nothing__here-1")
    assert spec is None
    assert outcome == LoadOutcome.MISSING


# ===========================================================================
# Cache I/O — atomic write + read round-trip
# ===========================================================================


def _setup_isolated_env(monkeypatch, tmp_path):
    """Helper: master flag ON + cache + dataset under tmp_path."""
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH",
        str(tmp_path / "dataset.jsonl"),
    )
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)


def test_cache_dir_resolves_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "x"),
    )
    assert cache_dir() == tmp_path / "x"


def test_cache_write_read_round_trip(monkeypatch, tmp_path):
    """Writing a spec then reading via load_problem (with dataset
    absent) returns LOADED_FROM_CACHE."""
    _setup_isolated_env(monkeypatch, tmp_path)
    spec = _sample_spec("test__round-trip-1")
    # Use private helper directly — substrate semantics
    assert dataset_loader._write_cache(spec) is True
    rebuilt, outcome = load_problem("test__round-trip-1")
    assert rebuilt == spec
    assert outcome == LoadOutcome.LOADED_FROM_CACHE


def test_cache_write_creates_parent_dirs(monkeypatch, tmp_path):
    deep = tmp_path / "deep" / "nested" / "cache"
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(deep))
    spec = _sample_spec("dir_creation__test-1")
    assert dataset_loader._write_cache(spec) is True
    assert deep.is_dir()


def test_malformed_cache_file_returns_none_not_raise(
    monkeypatch, tmp_path,
):
    """Defensive: malformed JSON in a cache file MUST be ignored,
    not raised."""
    _setup_isolated_env(monkeypatch, tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "bad__id-1.json").write_text("{not valid json", encoding="utf-8")
    spec, outcome = load_problem("bad__id-1")
    assert spec is None
    # outcome is MISSING — we couldn't load from cache OR find in
    # the dataset path (which doesn't exist either)
    assert outcome == LoadOutcome.MISSING


# ===========================================================================
# Local-JSONL acquisition (PRIMARY path)
# ===========================================================================


def test_load_from_local_jsonl_finds_known_instance(monkeypatch, tmp_path):
    _setup_isolated_env(monkeypatch, tmp_path)
    dataset = tmp_path / "dataset.jsonl"
    spec = _sample_spec("local_jsonl__test-1")
    dataset.write_text(json.dumps(spec.to_dict()) + "\n", encoding="utf-8")
    rebuilt, outcome = load_problem("local_jsonl__test-1")
    assert rebuilt == spec
    assert outcome == LoadOutcome.LOADED


def test_load_from_local_jsonl_caches_on_first_load(monkeypatch, tmp_path):
    """First load from JSONL → LOADED + cached.  Second load → LOADED_FROM_CACHE."""
    _setup_isolated_env(monkeypatch, tmp_path)
    dataset = tmp_path / "dataset.jsonl"
    spec = _sample_spec("cache_population__test-1")
    dataset.write_text(json.dumps(spec.to_dict()) + "\n", encoding="utf-8")
    _, outcome1 = load_problem("cache_population__test-1")
    assert outcome1 == LoadOutcome.LOADED
    _, outcome2 = load_problem("cache_population__test-1")
    assert outcome2 == LoadOutcome.LOADED_FROM_CACHE


def test_local_jsonl_skips_malformed_lines(monkeypatch, tmp_path):
    _setup_isolated_env(monkeypatch, tmp_path)
    dataset = tmp_path / "dataset.jsonl"
    spec = _sample_spec("malformed_skip__test-1")
    contents = (
        "this is not json\n"
        + json.dumps(spec.to_dict()) + "\n"
        + "{still not json\n"
    )
    dataset.write_text(contents, encoding="utf-8")
    rebuilt, outcome = load_problem("malformed_skip__test-1")
    assert rebuilt == spec
    assert outcome == LoadOutcome.LOADED


# ===========================================================================
# list_cached_problems + clear_cache
# ===========================================================================


def test_list_cached_problems_empty(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    assert list_cached_problems() == []


def test_list_cached_problems_after_writes(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    for instance_id in ("z__id-1", "a__id-1", "m__id-1"):
        dataset_loader._write_cache(_sample_spec(instance_id))
    cached = list_cached_problems()
    # Sorted output
    assert cached == ["a__id-1", "m__id-1", "z__id-1"]


def test_list_cached_problems_skips_underscore_prefixed_files(
    monkeypatch, tmp_path,
):
    """Underscore-prefixed files are reserved (e.g. future _index.json)."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "_reserved.json").write_text(
        json.dumps({"instance_id": "should_not_appear"}), encoding="utf-8",
    )
    dataset_loader._write_cache(_sample_spec("real__id-1"))
    cached = list_cached_problems()
    assert "should_not_appear" not in cached
    assert "real__id-1" in cached


def test_clear_cache_single(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    dataset_loader._write_cache(_sample_spec("clear__id-1"))
    dataset_loader._write_cache(_sample_spec("clear__id-2"))
    assert clear_cache("clear__id-1") == 1
    cached = list_cached_problems()
    assert "clear__id-1" not in cached
    assert "clear__id-2" in cached


def test_clear_cache_all(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    for i in range(3):
        dataset_loader._write_cache(_sample_spec(f"clearall__id-{i}"))
    assert clear_cache(None) == 3
    assert list_cached_problems() == []


def test_clear_cache_missing_dir_returns_zero(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH",
        str(tmp_path / "missing"),
    )
    assert clear_cache() == 0


# ===========================================================================
# list_cached_problems — union semantics over cache + LOCAL_DATASET_PATH
# ===========================================================================
#
# Closes the bug found by the v3.7 stage-1 wiring-validation soak
# (bt-2026-05-13-025330): the harness boot hook calls
# list_cached_problems() to enumerate available instance_ids; before
# this fix, that function only scanned the cache dir, missing fixture
# IDs declared in JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH. Operators
# were forced onto CSV-override workarounds. The fix:
# list_cached_problems() is now the single source of truth — returns
# cache_ids ∪ jsonl_instance_ids whenever LOCAL_DATASET_PATH is set
# and readable.


def test_list_cached_problems_fixture_only_no_cache(monkeypatch, tmp_path):
    """Operator binding: fixture-only config (no cache dir populated)
    yields a non-empty list. This is THE acceptance bar that closed
    the workaround-inducing bug."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "no-cache"),
    )
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text(
        json.dumps(_sample_spec("fixture_only__test-1").to_dict()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(dataset),
    )
    assert list_cached_problems() == ["fixture_only__test-1"]


def test_list_cached_problems_union_with_cache(monkeypatch, tmp_path):
    """Cache + JSONL both populated → union, sorted, deduped."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    # Cache: two instances
    dataset_loader._write_cache(_sample_spec("only_in_cache__id-1"))
    dataset_loader._write_cache(_sample_spec("shared__id-1"))
    # JSONL: one shared + one unique
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text(
        json.dumps(_sample_spec("shared__id-1").to_dict()) + "\n"
        + json.dumps(_sample_spec("only_in_jsonl__id-1").to_dict()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(dataset),
    )
    result = list_cached_problems()
    assert result == sorted([
        "only_in_cache__id-1",
        "only_in_jsonl__id-1",
        "shared__id-1",
    ])


def test_list_cached_problems_unset_dataset_path_returns_cache_only(
    monkeypatch, tmp_path,
):
    """When LOCAL_DATASET_PATH is unset, enumeration uses cache only —
    byte-identical to pre-fix behavior for the no-fixture case."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    dataset_loader._write_cache(_sample_spec("cache_only__test-1"))
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", raising=False,
    )
    assert list_cached_problems() == ["cache_only__test-1"]


def test_list_cached_problems_missing_jsonl_file_falls_back_to_cache(
    monkeypatch, tmp_path,
):
    """LOCAL_DATASET_PATH points at a nonexistent file — enumeration
    silently treats that source as empty (fail-open per-source)."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    dataset_loader._write_cache(_sample_spec("only_cache__test-1"))
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH",
        str(tmp_path / "does-not-exist.jsonl"),
    )
    assert list_cached_problems() == ["only_cache__test-1"]


def test_list_cached_problems_skips_jsonl_malformed_lines(
    monkeypatch, tmp_path,
):
    """Malformed JSONL lines + non-dict records + records missing
    instance_id are silently skipped — only well-formed records
    contribute to the enumeration."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "no-cache"),
    )
    dataset = tmp_path / "noisy.jsonl"
    dataset.write_text(
        "this is not json\n"
        + json.dumps(_sample_spec("ok__id-1").to_dict()) + "\n"
        + "42\n"  # non-dict
        + json.dumps({"no_instance_id": True}) + "\n"
        + json.dumps(_sample_spec("ok__id-2").to_dict()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(dataset),
    )
    assert list_cached_problems() == ["ok__id-1", "ok__id-2"]


def test_list_cached_problems_jsonl_bounded_scan(monkeypatch, tmp_path):
    """The bounded scan ceiling (_LOCAL_JSONL_MAX_ROWS) caps line
    enumeration. We monkeypatch to a tiny value to keep the test
    fast while validating the cap honors operator binding ("bounded
    scan — cap lines/bytes if needed")."""
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "no-cache"),
    )
    monkeypatch.setattr(dataset_loader, "_LOCAL_JSONL_MAX_ROWS", 3)
    dataset = tmp_path / "big.jsonl"
    rows = [
        json.dumps(_sample_spec(f"capped__id-{i}").to_dict())
        for i in range(10)
    ]
    dataset.write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(dataset),
    )
    result = list_cached_problems()
    # Only first 3 lines parsed; capped__id-3..9 omitted.
    assert result == ["capped__id-0", "capped__id-1", "capped__id-2"]


def test_list_cached_problems_load_problem_can_resolve_enumerated_ids(
    monkeypatch, tmp_path,
):
    """Operator binding: list_cached_problems() is the single source
    of truth for "what IDs can load_problem resolve." This pin asserts
    every id returned can in fact be loaded — there is no surface
    where the enumeration lies to consumers."""
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH", str(tmp_path / "cache"),
    )
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False,
    )
    dataset = tmp_path / "fixture.jsonl"
    dataset.write_text(
        json.dumps(_sample_spec("truth__id-1").to_dict()) + "\n"
        + json.dumps(_sample_spec("truth__id-2").to_dict()) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(dataset),
    )
    for iid in list_cached_problems():
        spec, outcome = load_problem(iid)
        assert spec is not None, (
            f"list_cached_problems claimed {iid!r} but load_problem "
            f"returned None (outcome={outcome.value}) — single-source-"
            f"of-truth contract violated"
        )


# ===========================================================================
# Authority asymmetry (§1 Boundary) — AST-pinned forbidden imports
# ===========================================================================


_FORBIDDEN_IMPORT_PREFIXES = (
    # Phase A is read-only data acquisition — must NOT import
    # policy substrates (those would invert the authority direction)
    ".governance.orchestrator",
    ".governance.iron_gate",
    ".governance.change_engine",
    ".governance.candidate_generator",
    ".governance.policy_engine",
    ".governance.risk_tier",
    ".governance.repair_engine",
    # No L2-exercise dep either — different arc
    ".governance.l2_exercise_seed",
)


def test_forbidden_imports_not_present():
    """Authority asymmetry pin: dataset_loader MUST NOT import any
    policy substrate.  Phase A is read-only data; orchestration
    happens in Phase B+."""
    found_forbidden = []
    for node in ast.walk(_LOADER_AST):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for prefix in _FORBIDDEN_IMPORT_PREFIXES:
                # Strip leading "backend.core.ouroboros" canonical
                # path for pattern matching
                normalized = module
                if normalized.startswith("backend.core.ouroboros"):
                    normalized = normalized[len("backend.core.ouroboros"):]
                if normalized.endswith(prefix) or prefix in normalized:
                    found_forbidden.append((module, prefix))
    assert found_forbidden == [], (
        f"Phase A dataset_loader has forbidden authority-inverting "
        f"imports: {found_forbidden}"
    )


def test_no_eager_datasets_import():
    """Composition pin: ``datasets`` library MUST be imported lazily
    inside ``_load_from_huggingface``, NOT at module top level.
    Hard top-level import would make ``datasets`` a hard dep."""
    top_level_datasets = [
        node for node in _LOADER_AST.body
        if isinstance(node, ast.Import)
        and any(a.name == "datasets" for a in node.names)
    ] + [
        node for node in _LOADER_AST.body
        if isinstance(node, ast.ImportFrom)
        and (node.module or "") == "datasets"
    ]
    assert top_level_datasets == [], (
        f"``datasets`` MUST be imported lazily inside "
        f"_load_from_huggingface; found top-level imports: "
        f"{top_level_datasets}"
    )


# ===========================================================================
# FlagRegistry self-registration
# ===========================================================================


class _FakeRegistry:
    """Minimal duck-typed stub for register_flags testing."""

    def __init__(self):
        self.registered = []

    def register(self, spec):
        self.registered.append(spec)


def test_register_flags_registers_six_specs():
    """Phase A ships 5 env knobs (master + cache_path +
    local_dataset_path + hf_dataset + hf_split); Stage 2 adds the
    bounded full-dataset scan ceiling (sampler_max_scan) → 6.
    Drift here = a knob was added/removed without a Phase tag."""
    reg = _FakeRegistry()
    count = dataset_loader.register_flags(reg)
    assert count == 6
    names = sorted(s.name for s in reg.registered)
    assert names == sorted([
        "JARVIS_SWE_BENCH_PRO_ENABLED",
        "JARVIS_SWE_BENCH_PRO_CACHE_PATH",
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH",
        "JARVIS_SWE_BENCH_PRO_HF_DATASET",
        "JARVIS_SWE_BENCH_PRO_HF_SPLIT",
        "JARVIS_SWE_BENCH_PRO_SAMPLER_MAX_SCAN",
    ])


def test_register_flags_master_default_is_false():
    """§33.1 contract: master flag default MUST be False."""
    reg = _FakeRegistry()
    dataset_loader.register_flags(reg)
    master = next(
        s for s in reg.registered
        if s.name == "JARVIS_SWE_BENCH_PRO_ENABLED"
    )
    assert master.default is False


def test_register_flags_fail_open_on_registry_failure():
    """register_flags MUST swallow individual registry failures and
    continue.  Boot-time fail-open contract."""

    class _BrokenRegistry:
        def __init__(self):
            self.calls = 0

        def register(self, spec):
            self.calls += 1
            raise RuntimeError("synthetic registry failure")

    reg = _BrokenRegistry()
    count = dataset_loader.register_flags(reg)
    assert count == 0  # nothing succeeded
    assert reg.calls == 6  # but all 6 were attempted (no early-exit)
