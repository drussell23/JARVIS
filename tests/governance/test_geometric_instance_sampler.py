"""Spine — SWE-Bench-Pro GeometricInstanceSampler (Stage 2).

Pins the self-curating discriminator-pair sampler:

  * **Determinism** — same dataset → same (known-good, known-hard)
    pair across repeated calls (reproducible rubric baseline).
  * **Geometry correctness** — known-good is the smallest
    single-file gold patch; known-hard is the largest multi-file
    gold patch.
  * **Canonical composition (AST)** — gold-patch *file* geometry
    goes through the canonical ``extract_diff_targets`` (no
    hand-rolled diff path-parsing); the dataset scan goes through
    ``iter_all_dataset_records`` (no parallel loader); the HF
    ``datasets.load_dataset`` call lives in EXACTLY ONE place.
  * **Fail-open (§7)** — empty / single-pool / degenerate datasets
    yield ``None``, never raise.
  * **Master-flag gate** — sampler reads nothing when the loader
    master flag is off.
  * **harness_inject tier order** — CSV > geometric > first-N.
  * **FlagRegistry seed** present, default-False, SAFETY.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import (
    dataset_loader,
    geometric_sampler,
)
from backend.core.ouroboros.governance.swe_bench_pro.geometric_sampler import (
    PatchGeometry,
    compute_patch_geometry,
    sample_discriminator_pair,
)

_LOADER_SRC = (
    Path(dataset_loader.__file__).read_text(encoding="utf-8")
)
_SAMPLER_SRC = (
    Path(geometric_sampler.__file__).read_text(encoding="utf-8")
)


# ---------------------------------------------------------------------------
# Fixtures — synthetic dataset with known geometry
# ---------------------------------------------------------------------------


def _diff(files):
    """Build a synthetic unified diff with the given
    ``{path: n_changed_lines}`` shape."""
    parts = []
    for path, n in files.items():
        parts.append(f"diff --git a/{path} b/{path}")
        parts.append(f"--- a/{path}")
        parts.append(f"+++ b/{path}")
        parts.append(f"@@ -1,{n} +1,{n} @@")
        for i in range(n):
            parts.append(f"-old{i}")
            parts.append(f"+new{i}")
    return "\n".join(parts) + "\n"


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    """Write a local JSONL with 4 problems of known geometry +
    enable the master flag."""
    rows = [
        # tiny single-file (5 changed lines) — THE known-good
        {"instance_id": "org__small-1", "gold_patch":
            _diff({"a.py": 5})},
        # bigger single-file (40 lines) — single-file but not min
        {"instance_id": "org__mid-2", "gold_patch":
            _diff({"b.py": 40})},
        # small multi-file (2 files, 12 lines) — multi but not max
        {"instance_id": "org__multi-3", "gold_patch":
            _diff({"c.py": 4, "d.py": 8})},
        # huge multi-file (3 files, 120 lines) — THE known-hard
        {"instance_id": "org__huge-4", "gold_patch":
            _diff({"e.py": 50, "f.py": 40, "g.py": 30})},
    ]
    p = tmp_path / "dataset.jsonl"
    p.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(p),
    )
    # HF not set → HF iterator is empty; pure-local scan.
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    return rows


# ---------------------------------------------------------------------------
# Geometry correctness
# ---------------------------------------------------------------------------


def test_compute_patch_geometry_counts_files_and_lines():
    g = compute_patch_geometry(
        "x", _diff({"one.py": 3, "two.py": 7}),
    )
    assert g.changed_files == 2
    # 3+7 hunk pairs → each pair = 1 '-' + 1 '+' = 2 lines
    assert g.changed_lines == (3 + 7) * 2
    assert g.is_multi_file and not g.is_single_file


def test_compute_patch_geometry_single_file():
    g = compute_patch_geometry("y", _diff({"solo.py": 4}))
    assert g.changed_files == 1
    assert g.is_single_file and not g.is_multi_file


def test_compute_patch_geometry_empty_is_zero_never_raises():
    g = compute_patch_geometry("z", "")
    assert g == PatchGeometry("z", 0, 0)
    # garbage in → zero-geometry out, no raise
    g2 = compute_patch_geometry("z2", "not a diff at all\n@@@@\n")
    assert g2.instance_id == "z2"


# ---------------------------------------------------------------------------
# Selection + determinism
# ---------------------------------------------------------------------------


def test_selects_smallest_single_and_largest_multi(dataset):
    s = sample_discriminator_pair()
    assert s is not None
    assert s.known_good_id == "org__small-1"  # smallest single-file
    assert s.known_hard_id == "org__huge-4"   # largest multi-file
    assert s.known_good_geometry.is_single_file
    assert s.known_hard_geometry.is_multi_file
    assert s.known_hard_geometry.changed_lines > \
        s.known_good_geometry.changed_lines
    # injection order: known-good first
    assert s.instance_ids == ["org__small-1", "org__huge-4"]


def test_deterministic_repeated_calls(dataset):
    a = sample_discriminator_pair()
    b = sample_discriminator_pair()
    assert a is not None and b is not None
    assert a.to_dict() == b.to_dict()


def test_tiebreak_is_deterministic_on_instance_id(tmp_path, monkeypatch):
    # Two single-file patches with IDENTICAL geometry — the
    # lexicographically-smallest id must win, every time.
    rows = [
        {"instance_id": "org__bbb", "gold_patch": _diff({"a.py": 5})},
        {"instance_id": "org__aaa", "gold_patch": _diff({"a.py": 5})},
        {"instance_id": "org__m", "gold_patch":
            _diff({"x.py": 9, "y.py": 9})},
    ]
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(p))
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    s = sample_discriminator_pair()
    assert s is not None
    assert s.known_good_id == "org__aaa"  # smaller id wins the tie


# ---------------------------------------------------------------------------
# Fail-open (§7)
# ---------------------------------------------------------------------------


def test_master_flag_off_yields_none(dataset, monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "false")
    assert sample_discriminator_pair() is None


def test_no_single_file_yields_none(tmp_path, monkeypatch):
    rows = [{"instance_id": "org__m", "gold_patch":
             _diff({"x.py": 4, "y.py": 4})}]
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(p))
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    assert sample_discriminator_pair() is None


def test_no_multi_file_yields_none(tmp_path, monkeypatch):
    rows = [{"instance_id": "org__s", "gold_patch":
             _diff({"x.py": 4})}]
    p = tmp_path / "ds.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(p))
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    assert sample_discriminator_pair() is None


def test_empty_dataset_yields_none(tmp_path, monkeypatch):
    p = tmp_path / "empty.jsonl"
    p.write_text("")
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH", str(p))
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_HF_DATASET", raising=False)
    assert sample_discriminator_pair() is None


# ---------------------------------------------------------------------------
# Bounded scan
# ---------------------------------------------------------------------------


def test_scan_is_bounded_by_env(dataset, monkeypatch):
    # Cap at 1 record → only the first row visible → no multi-file
    # candidate → None (proves the cap is honored at call time).
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_SAMPLER_MAX_SCAN", "1")
    assert sample_discriminator_pair() is None


# ---------------------------------------------------------------------------
# AST — canonical composition (no parallel logic, single seam)
# ---------------------------------------------------------------------------


def test_ast_sampler_composes_canonical_extract_diff_targets():
    assert (
        "from backend.core.ouroboros.governance.repair_tree_production "
        "import" in _SAMPLER_SRC
        and "extract_diff_targets" in _SAMPLER_SRC
    ), "sampler MUST compose canonical extract_diff_targets"
    # No hand-rolled diff path-parsing: the module must not import re
    # to dissect '+++ b/' / '--- a/' headers itself.
    tree = ast.parse(_SAMPLER_SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    assert "re" not in imported, (
        "sampler MUST NOT hand-roll regex diff parsing — "
        "extract_diff_targets is the single source of truth"
    )


def test_ast_sampler_composes_iter_all_dataset_records():
    assert "iter_all_dataset_records" in _SAMPLER_SRC, (
        "sampler MUST scan via the composed iter_all_dataset_records "
        "(no parallel dataset loader)"
    )


def test_ast_hf_load_dataset_call_is_single_seam():
    """``datasets.load_dataset`` must appear EXACTLY ONCE in the
    loader — _load_from_huggingface + iter_all_dataset_records both
    compose _iter_hf_records (single source of truth for the HF
    load call shape)."""
    occurrences = _LOADER_SRC.count("datasets.load_dataset(")
    assert occurrences == 1, (
        f"datasets.load_dataset must be a single seam; found "
        f"{occurrences} call sites"
    )


def test_ast_iter_all_records_is_master_flag_gated():
    tree = ast.parse(_LOADER_SRC)
    fn = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "iter_all_dataset_records"
        ):
            fn = node
            break
    assert fn is not None
    seg = ast.get_source_segment(_LOADER_SRC, fn) or ""
    assert "swe_bench_pro_enabled()" in seg, (
        "iter_all_dataset_records MUST short-circuit on the master "
        "flag (no dataset I/O when the feature is off)"
    )


# ---------------------------------------------------------------------------
# harness_inject tier ordering
# ---------------------------------------------------------------------------


def test_harness_inject_tier_order(dataset, monkeypatch):
    from backend.core.ouroboros.governance.swe_bench_pro import (
        harness_inject,
    )

    pair = ["org__small-1", "org__huge-4"]

    # Tier 2 OFF → legacy first-N path (ambient cache state may vary;
    # the load-bearing invariant is that the sampler is NOT consulted,
    # i.e. the result is not the geometric pair).
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED",
        raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", raising=False)
    assert harness_inject._resolve_instance_ids() != pair

    # Tier 2 ON → exactly the geometric pair (overrides legacy).
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED", "true")
    assert harness_inject._resolve_instance_ids() == pair

    # Tier 1 CSV override beats the sampler.
    monkeypatch.setenv(
        "JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS", "explicit__id-9")
    assert harness_inject._resolve_instance_ids() == ["explicit__id-9"]


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_flag_seed_present_default_false():
    captured = []

    class _Reg:
        def register(self, spec):
            captured.append(spec)

    n = geometric_sampler.register_flags(_Reg())
    assert n == 1
    spec = captured[0]
    assert spec.name == (
        "JARVIS_SWE_BENCH_PRO_GEOMETRIC_SAMPLER_ENABLED"
    )
    assert spec.default is False
    assert "geometric_sampler.py" in spec.source_file
