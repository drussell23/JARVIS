from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import target_stratification as ts


@pytest.fixture
def warm_index(tmp_path, monkeypatch):
    """Build a tiny real coverage index so file_has_test_coverage
    takes the WARM path."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mymod.py").write_text(
        "def test_a():\n    pass\n",
    )
    (tmp_path / "mymod.py").write_text("x = 1\n")
    ts._cached_scan_root.cache_clear()
    # Force a synchronous index build for the test root (worker fn
    # is exposed for the off-loop builder; call it directly here).
    ts._install_coverage_index_for_tests(tmp_path)  # see Step 3
    return tmp_path


def test_warm_path_single_resolve(warm_index, monkeypatch):
    """WARM lookup must issue at most ONE Path.resolve chain
    (the candidate file), not four."""
    calls = {"n": 0}
    real_resolve = Path.resolve

    def counting_resolve(self, *a, **kw):
        calls["n"] += 1
        return real_resolve(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    assert ts.file_has_test_coverage("mymod.py", warm_index) is True
    assert calls["n"] <= 1, f"warm path did {calls['n']} resolve() chains"


def test_cached_scan_root_caches(warm_index, monkeypatch):
    ts._cached_scan_root.cache_clear()
    r1 = ts._cached_scan_root(str(warm_index))
    r2 = ts._cached_scan_root(str(warm_index))
    assert r1 is r2  # same object — cache hit
    info = ts._cached_scan_root.cache_info()
    assert info.hits >= 1 and info.misses == 1


def test_warm_semantics_unchanged(warm_index):
    assert ts.file_has_test_coverage("mymod.py", warm_index) is True
    assert ts.file_has_test_coverage("nocov.py", warm_index) is False
    # non-.py and test_ inputs still treated as covered
    assert ts.file_has_test_coverage("README.md", warm_index) is True
    assert ts.file_has_test_coverage("test_mymod.py", warm_index) is True
