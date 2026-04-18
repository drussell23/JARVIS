"""MutationCache — catalog + outcome cache regression spine."""
from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import mutation_cache as MC
from backend.core.ouroboros.governance.mutation_tester import (
    Mutant, enumerate_mutants,
)


@pytest.fixture(autouse=True)
def _clean_caches_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MUTATION_CACHE_DIR", str(tmp_path / "cache"))
    for k in list(os.environ.keys()):
        if k.startswith("JARVIS_MUTATION_CACHE_") and k != "JARVIS_MUTATION_CACHE_DIR":
            monkeypatch.delenv(k, raising=False)
    MC._catalog_lru.clear()
    MC._outcome_lru.clear()
    yield


def _write_py(path: Path, src: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(src), encoding="utf-8")


def test_file_hash_stable_and_sensitive(tmp_path):
    p = tmp_path / "a.py"
    p.write_text("x = 1\n")
    h1 = MC.file_hash(p)
    h2 = MC.file_hash(p)
    assert h1 == h2
    assert len(h1) == 64
    p.write_text("x = 2\n")
    assert MC.file_hash(p) != h1


def test_file_hash_missing_returns_empty(tmp_path):
    assert MC.file_hash(tmp_path / "missing.py") == ""


def test_composite_hash_order_independent(tmp_path):
    a = tmp_path / "a.py"; a.write_text("A")
    b = tmp_path / "b.py"; b.write_text("B")
    h1 = MC.files_composite_hash([a, b])
    h2 = MC.files_composite_hash([b, a])
    assert h1 == h2


def test_catalog_miss_then_put_then_hit(tmp_path):
    sut = tmp_path / "s.py"
    _write_py(sut, "def f(x): return x == 1")
    h1, miss = MC.get_catalog(sut)
    assert miss is None
    assert h1 != ""
    mutants = enumerate_mutants(sut)
    assert mutants
    MC.put_catalog(h1, mutants)
    h2, hit = MC.get_catalog(sut)
    assert h2 == h1
    assert hit is not None and len(hit) == len(mutants)
    assert hit[0].op == mutants[0].op
    assert hit[0].patched_src == mutants[0].patched_src


def test_catalog_invalidated_on_content_change(tmp_path):
    sut = tmp_path / "s.py"
    _write_py(sut, "def f(): return True")
    h1, _ = MC.get_catalog(sut)
    MC.put_catalog(h1, enumerate_mutants(sut))
    # Rewrite — new hash, cache miss expected.
    _write_py(sut, "def f(): return False")
    h2, hit = MC.get_catalog(sut)
    assert h2 != h1
    assert hit is None


def test_catalog_persists_to_disk_across_process(tmp_path):
    sut = tmp_path / "s.py"
    _write_py(sut, "def f(x): return x > 0")
    h, _ = MC.get_catalog(sut)
    MC.put_catalog(h, enumerate_mutants(sut))
    # Simulate process restart by clearing the RAM LRU.
    MC._catalog_lru.clear()
    h2, hit = MC.get_catalog(sut)
    assert h2 == h
    assert hit is not None


def test_outcome_miss_then_put_then_hit():
    sut_hash = "a" * 64
    tests_hash = "b" * 64
    assert MC.get_outcomes(sut_hash, tests_hash) is None
    MC.put_outcomes(sut_hash, tests_hash, {"k1": "caught", "k2": "survived"})
    hit = MC.get_outcomes(sut_hash, tests_hash)
    assert hit == {"k1": "caught", "k2": "survived"}


def test_outcome_separate_by_tests_hash():
    MC.put_outcomes("s", "t1", {"k": "caught"})
    MC.put_outcomes("s", "t2", {"k": "survived"})
    assert MC.get_outcomes("s", "t1") == {"k": "caught"}
    assert MC.get_outcomes("s", "t2") == {"k": "survived"}


def test_disable_bypasses_catalog_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_MUTATION_CACHE_DISABLED", "1")
    sut = tmp_path / "s.py"
    _write_py(sut, "def f(): return 1")
    # put then get — disabled kill switch means get returns None.
    _, _ = MC.get_catalog(sut)
    MC.put_catalog("anyhash", enumerate_mutants(sut))
    _, hit = MC.get_catalog(sut)
    assert hit is None


def test_invalidate_outcomes_clears_ram_and_disk():
    MC.put_outcomes("s", "t", {"k": "caught"})
    assert MC.get_outcomes("s", "t") is not None
    MC.invalidate_outcomes()
    assert MC.get_outcomes("s", "t") is None


def test_invalidate_catalog(tmp_path):
    sut = tmp_path / "s.py"
    _write_py(sut, "def f(): return 1")
    h, _ = MC.get_catalog(sut)
    MC.put_catalog(h, enumerate_mutants(sut))
    MC.invalidate_catalog(sut)
    # After invalidation: RAM empty, disk entry gone.
    _, hit = MC.get_catalog(sut)
    assert hit is None


def test_lru_evicts_oldest(monkeypatch):
    lru = MC._LRU(maxsize=2)
    lru.put("a", 1)
    lru.put("b", 2)
    lru.put("c", 3)  # evicts "a"
    assert lru.get("a") is None
    assert lru.get("b") == 2
    assert lru.get("c") == 3


def test_cache_stats_exposes_counts():
    MC._catalog_lru.put("x", ())
    MC._outcome_lru.put("y", {})
    stats = MC.cache_stats()
    assert stats["catalog_ram"] == 1
    assert stats["outcomes_ram"] == 1
