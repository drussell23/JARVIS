"""Regression spine — Slice 6 Task 2: the deterministic AST test→source
attribution bridge. Every case here maps to a mandate: no path heuristics
(a tmp repo whose layout deliberately breaks tests/→src/ mirroring),
alias/relative/indirect handling, multi-source bounding, and fail-fast
on unresolvable attribution."""
from __future__ import annotations

import json
import textwrap
import time

import pytest

import backend.core.ouroboros.governance.intent.test_source_attribution as tsa
from backend.core.ouroboros.governance.intent.test_source_attribution import (
    Attribution,
    AttributionUnresolved,
    attribute_test_to_sources,
    attribution_enabled,
    attribution_status,
    prewarm_module_map,
    unattributed_test_scope_violation,
)


@pytest.fixture()
def repo(tmp_path):
    """A tmp repo whose test path deliberately does NOT mirror the source
    path (mandate 1: any tests/foo→src/foo heuristic would fail here)."""
    src = tmp_path / "backend" / "core" / "widgets"
    src.mkdir(parents=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (tmp_path / "backend" / "core" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    (src / "gadget.py").write_text("def spin():\n    return 1\n")
    (src / "helper.py").write_text("def aid():\n    return 2\n")
    tdir = tmp_path / "tests" / "unit_checks"   # ≠ backend/core/widgets
    tdir.mkdir(parents=True)
    return tmp_path, tdir


def _write_test(tdir, body: str, name: str = "test_gadget.py"):
    p = tdir / name
    p.write_text(textwrap.dedent(body))
    return str(p)


def test_direct_from_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)
    assert attr.test_locus == "tests/unit_checks/test_gadget.py"
    assert attr.method == "direct_import"


def test_aliased_module_import_resolves(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import backend.core.widgets.gadget as g
        def test_spin():
            assert g.spin() == 1
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci


def test_multi_source_carries_all_bounded(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "8")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert set(attr.source_loci) == {
        "backend/core/widgets/gadget.py",
        "backend/core/widgets/helper.py",
    }


def test_max_source_cap_enforced(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    monkeypatch.setenv("JARVIS_ATTRIBUTION_MAX_SOURCE_FILES", "1")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert len(attr.source_loci) == 1


def test_patch_target_secondary_signal(repo, monkeypatch) -> None:
    """mock.patch target strings recover indirection (the ~17% class)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest import mock
        def test_spun():
            with mock.patch("backend.core.widgets.gadget.spin", return_value=9):
                assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci
    assert "patch_target" in attr.evidence_kinds


def test_pure_patch_method_label_is_honest(repo, monkeypatch) -> None:
    """A patch-only attribution (zero direct imports) must label its
    method 'patch_target' — never falsely claim direct-import evidence
    by piggybacking on the direct+patch combined label."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest import mock
        def test_spun():
            with mock.patch("backend.core.widgets.gadget.spin", return_value=9):
                assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.method == "patch_target"
    assert attr.evidence_kinds == ("patch_target",)


def test_direct_and_patch_combined_method_label(repo, monkeypatch) -> None:
    """When both direct-import and patch-target evidence are present in
    the ranked (bounded) result, method reflects both kinds present."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest import mock
        from backend.core.widgets.helper import aid
        def test_both():
            with mock.patch("backend.core.widgets.gadget.spin", return_value=9):
                assert aid() == 2
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.method == "direct_import+patch_target"
    assert set(attr.evidence_kinds) == {"direct_import", "patch_target"}


def test_rest_client_patch_call_not_attributed(repo, monkeypatch) -> None:
    """A REST-client-shaped `.patch(...)` call (e.g. `client.patch(path)`
    where `client` is not `mock`/`unittest.mock`) must NOT be treated as
    a mock.patch target — receiver identity, not bare call-name, gates
    the match (avoids the REST-client false-positive class)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        def test_calls_rest_client(client):
            resp = client.patch("backend.core.widgets.gadget.spin")
            assert resp
    """)
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "no_first_party_source_imports"


def test_bare_setattr_call_not_attributed(repo, monkeypatch) -> None:
    """Bare `setattr(obj, "attr", val)` (stdlib builtin, no receiver
    identity) must never be treated as a patch target — even when its
    string argument is dotted and even in the contrived bare-Name-first-
    arg form."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        class Obj:
            pass

        def test_bare_setattr():
            obj = Obj()
            setattr(obj, "backend.core.widgets.gadget.spin", 1)
            setattr("backend.core.widgets.gadget.spin", "x")
            assert True
    """)
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "no_first_party_source_imports"


def test_monkeypatch_setattr_still_attributes(repo, monkeypatch) -> None:
    """The canonical monkeypatch.setattr shape must remain a positive
    signal (receiver-identity tightening must not regress it)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        def test_patches(monkeypatch):
            monkeypatch.setattr("backend.core.widgets.gadget.spin", lambda: 1)
            assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci
    assert attr.method == "patch_target"


def test_bare_patch_import_still_attributes(repo, monkeypatch) -> None:
    """`from unittest.mock import patch; patch(...)` (bare Name callee)
    must remain a positive signal."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from unittest.mock import patch
        def test_spun():
            with patch("backend.core.widgets.gadget.spin", return_value=9):
                assert True
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert "backend/core/widgets/gadget.py" in attr.source_loci
    assert attr.method == "patch_target"


def test_traceback_frames_rank_first(repo, monkeypatch) -> None:
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.gadget import spin
        from backend.core.widgets.helper import aid
        def test_both():
            assert spin() + aid() == 3
    """)
    attr = attribute_test_to_sources(
        tf, repo_root=str(root),
        traceback_frames=("backend/core/widgets/helper.py",),
    )
    assert attr.source_loci[0] == "backend/core/widgets/helper.py"


def test_test_infra_imports_excluded(repo, monkeypatch) -> None:
    """Importing a sibling test helper must NOT attribute to test infra —
    classification is config-driven via JARVIS_TEST_DIR_NAMES (mandate 1:
    no hardcoded directory assumption)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    (tdir / "__init__.py").write_text("")
    (root / "tests" / "__init__.py").write_text("")
    (tdir / "helpers.py").write_text("X = 1\n")
    tf = _write_test(tdir, """
        from tests.unit_checks.helpers import X
        from backend.core.widgets.gadget import spin
        def test_spin():
            assert spin() == X
    """)
    attr = attribute_test_to_sources(tf, repo_root=str(root))
    assert attr.source_loci == ("backend/core/widgets/gadget.py",)


def test_unresolved_no_first_party_imports(repo, monkeypatch) -> None:
    """Fail-fast (mandate 4): stdlib-only test → typed error, never a
    silent test-file-scoped fallback."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        import os
        def test_env():
            assert os.sep
    """)
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "no_first_party_source_imports"


def test_unresolved_parse_error(repo, monkeypatch) -> None:
    root, tdir = repo
    tf = _write_test(tdir, "def broken(:\n")
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(tf, repo_root=str(root))
    assert exc.value.reason == "parse_error"


def test_unresolved_missing_file(repo) -> None:
    root, _ = repo
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources("tests/nope/test_ghost.py", repo_root=str(root))
    assert exc.value.reason == "test_file_missing"


def test_unresolved_test_outside_root(repo) -> None:
    root, _ = repo
    with pytest.raises(AttributionUnresolved) as exc:
        attribute_test_to_sources(
            "/absolute/elsewhere/test_x.py", repo_root=str(root),
        )
    assert exc.value.reason == "test_outside_root"


def test_deterministic_across_calls(repo, monkeypatch) -> None:
    """Same inputs → identical output tuple (mandate 1: deterministic)."""
    root, tdir = repo
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    tf = _write_test(tdir, """
        from backend.core.widgets.helper import aid
        from backend.core.widgets.gadget import spin
        def test_both():
            assert spin() + aid() == 3
    """)
    a = attribute_test_to_sources(tf, repo_root=str(root))
    b = attribute_test_to_sources(tf, repo_root=str(root))
    assert a == b


def test_master_switch(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    assert attribution_enabled() is False
    monkeypatch.delenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", raising=False)
    assert attribution_enabled() is True


# ---- the Task-5 gate predicate (pure function, unit-tested here) ----

def _ev(status: str, test_locus: str = "tests/unit_checks/test_gadget.py") -> str:
    return json.dumps({"attribution": {
        "schema_version": 1, "status": status, "test_locus": test_locus,
        "source_loci": [], "method": "", "reason": "no_first_party_source_imports",
    }})


def test_gate_fires_on_unresolved_test_only_mutation(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    msg = unattributed_test_scope_violation(
        _ev("unresolved"), ["tests/unit_checks/test_gadget.py"],
    )
    assert msg is not None and "unresolved" in msg


def test_gate_silent_when_resolved(monkeypatch) -> None:
    assert unattributed_test_scope_violation(
        _ev("resolved"), ["tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_silent_when_candidate_touches_source(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    assert unattributed_test_scope_violation(
        _ev("unresolved"),
        ["backend/core/widgets/gadget.py", "tests/unit_checks/test_gadget.py"],
    ) is None


def test_gate_fail_soft_on_malformed_evidence() -> None:
    assert unattributed_test_scope_violation("{not json", ["x.py"]) is None
    assert unattributed_test_scope_violation("", ["x.py"]) is None


def test_gate_off_switch_returns_none_even_for_unresolved(monkeypatch) -> None:
    """JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED=false must short-circuit the
    gate to None even for an otherwise-firing unresolved test-only
    mutation."""
    monkeypatch.setenv("JARVIS_ATTRIBUTION_SCOPE_GATE_ENABLED", "false")
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    assert unattributed_test_scope_violation(
        _ev("unresolved"), ["tests/unit_checks/test_gadget.py"],
    ) is None


# ---- C1: off-loop module-map pre-warm (single-flight + offload seam) ----

@pytest.fixture(autouse=True)
def _clear_map_cache():
    """Each pre-warm/module-map test starts with a cold cache."""
    tsa._MAP_CACHE.clear()
    yield
    tsa._MAP_CACHE.clear()


@pytest.mark.asyncio
async def test_prewarm_populates_cache_via_offload_seam(repo, monkeypatch):
    """4a: prewarm routes the build through cooperative_fs_io.offload and
    populates the SAME _MAP_CACHE the sync path reads."""
    root, _tdir = repo
    root = str(root)
    import backend.core.ouroboros.governance.cooperative_fs_io as cfio

    seen = {"called": False, "fn": None}
    real_offload = cfio.offload

    async def _spy_offload(fn, *args, **kwargs):
        seen["called"] = True
        seen["fn"] = fn
        return await real_offload(fn, *args, **kwargs)

    monkeypatch.setattr(cfio, "offload", _spy_offload)
    assert root not in tsa._MAP_CACHE

    await prewarm_module_map(root)

    assert seen["called"] is True
    assert seen["fn"] is tsa._build_and_cache_module_map
    # Same cache the in-loop sync path reads is now warm.
    assert root in tsa._MAP_CACHE
    assert isinstance(tsa._MAP_CACHE[root][1], dict)


@pytest.mark.asyncio
async def test_get_module_map_is_cache_hit_after_prewarm(repo, monkeypatch):
    """4b: after prewarm, the in-loop _get_module_map must NOT rebuild —
    monkeypatch build_module_to_path to raise; a cache hit never calls it."""
    root, _tdir = repo
    root = str(root)
    await prewarm_module_map(root)
    assert root in tsa._MAP_CACHE

    def _boom(*_a, **_k):
        raise AssertionError("build_module_to_path must not be called on a hit")

    monkeypatch.setattr(tsa, "build_module_to_path", _boom)
    mapping = tsa._get_module_map(root)  # must be a pure dict hit
    assert isinstance(mapping, dict)


@pytest.mark.asyncio
async def test_prewarm_noop_when_already_warm(repo, monkeypatch):
    """A fresh cache entry short-circuits prewarm before touching offload
    (no redundant crawl on back-to-back red cycles)."""
    root, _tdir = repo
    root = str(root)
    await prewarm_module_map(root)  # first warm

    import backend.core.ouroboros.governance.cooperative_fs_io as cfio
    called = {"n": 0}
    real = cfio.offload

    async def _count(fn, *a, **k):
        called["n"] += 1
        return await real(fn, *a, **k)

    monkeypatch.setattr(cfio, "offload", _count)
    await prewarm_module_map(root)  # warm -> must not offload again
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_prewarm_fail_soft_on_offload_error(repo, monkeypatch):
    """An OffloadError result leaves the cache untouched (inline path still
    works); prewarm never raises."""
    root, _tdir = repo
    root = str(root)
    import backend.core.ouroboros.governance.cooperative_fs_io as cfio

    async def _err(fn, *a, **k):
        return cfio.OffloadError(
            fn_name="build", exc_type="OSError", message="boom", cpu_bound=False,
        )

    monkeypatch.setattr(cfio, "offload", _err)
    await prewarm_module_map(root)  # must not raise
    assert root not in tsa._MAP_CACHE


# ---- I2: gate predicate normalizes ABSOLUTE candidate paths ----

def test_gate_fires_on_absolute_test_infra_candidate(tmp_path, monkeypatch):
    """An ABSOLUTE test-infra candidate path must still trip the gate once
    repo_root is supplied (without it, module became Users.x… — not test-
    classified — and the blind-mutation gate silently no-op'd)."""
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    root = str(tmp_path)
    abs_conftest = str(tmp_path / "tests" / "conftest.py")
    # Sanity: without repo_root the absolute path defeats classification.
    assert unattributed_test_scope_violation(
        _ev("unresolved"), [abs_conftest],
    ) is None
    # With repo_root the absolute path normalizes and the gate fires.
    msg = unattributed_test_scope_violation(
        _ev("unresolved"), [abs_conftest], repo_root=root,
    )
    assert msg is not None and "unresolved" in msg


def test_gate_silent_on_absolute_source_candidate(tmp_path, monkeypatch):
    """An ABSOLUTE SOURCE-file candidate must NOT trip the gate (no false
    positive) even with repo_root supplied."""
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    root = str(tmp_path)
    abs_source = str(tmp_path / "backend" / "mod" / "engine.py")
    assert unattributed_test_scope_violation(
        _ev("unresolved"), [abs_source], repo_root=root,
    ) is None


# ---- Slice 7 — single evidence parser ----


class TestAttributionStatus:
    """Slice 7 — single evidence parser consumed by the coverage gate."""

    def test_resolved(self):
        j = json.dumps({"attribution": {"status": "resolved"}})
        assert attribution_status(j) == "resolved"

    def test_unresolved(self):
        j = json.dumps({"attribution": {"status": "unresolved"}})
        assert attribution_status(j) == "unresolved"

    def test_absent_attribution_block(self):
        assert attribution_status(json.dumps({"other": 1})) == ""

    def test_empty_and_none_ish(self):
        assert attribution_status("") == ""
        assert attribution_status("{}") == ""

    def test_malformed_json(self):
        assert attribution_status("{not json") == ""

    def test_non_dict_evidence(self):
        assert attribution_status("[1, 2]") == ""

    def test_non_dict_attribution_value(self):
        assert attribution_status(json.dumps({"attribution": "resolved"})) == ""


class _ForbiddenLock:
    """Trips the moment anything on the probe path touches the lock."""

    def __enter__(self):
        raise AssertionError("prewarm warm-path probe must not take _MAP_CACHE_LOCK")

    def __exit__(self, *args):
        return False

    def acquire(self, *args, **kwargs):
        raise AssertionError("prewarm warm-path probe must not take _MAP_CACHE_LOCK")

    def release(self):
        pass


@pytest.mark.asyncio
async def test_prewarm_warm_probe_never_touches_lock(monkeypatch):
    """Slice 7 fast-follow (Slice-6 final review): an in-flight executor
    build holds _MAP_CACHE_LOCK for the full ~7s crawl; probing under
    the lock would block the event loop for exactly that long."""
    root = "/definitely/fake/slice7-root"
    monkeypatch.setitem(
        tsa._MAP_CACHE, root, (time.monotonic(), {"m": "m.py"}),
    )
    monkeypatch.setattr(tsa, "_MAP_CACHE_LOCK", _ForbiddenLock())
    # Old code raises AssertionError from the `with _MAP_CACHE_LOCK:`
    # probe; fixed code returns without ever touching the lock.
    await tsa.prewarm_module_map(root)
