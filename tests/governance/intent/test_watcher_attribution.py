"""Slice 6 Task 4 — the pin (test_watcher.py:444 `target_files=(f.file_path,)`)
is replaced by attributed scope. Uses a real tmp repo (no mocks of the
attributor — feedback_fakes_must_mirror_real_contract)."""
from __future__ import annotations

import textwrap

import pytest

from backend.core.ouroboros.governance.intent.test_watcher import (
    TestFailure,
    TestWatcher,
)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_DIR_NAMES", "tests")
    src = tmp_path / "backend" / "mod"
    src.mkdir(parents=True)
    (tmp_path / "backend" / "__init__.py").write_text("")
    (src / "__init__.py").write_text("")
    (src / "engine.py").write_text("def go():\n    return 1\n")
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_engine.py").write_text(textwrap.dedent("""
        from backend.mod.engine import go
        def test_go():
            assert go() == 1
    """))
    return tmp_path


def _fail(path: str) -> TestFailure:
    return TestFailure(
        test_id=f"{path}::test_go",
        file_path=path,
        error_text="AssertionError: boom",
    )


def _stable_signal(watcher, failure):
    """Two consecutive runs → stable signal on the second."""
    assert watcher.process_failures([failure]) == []
    signals = watcher.process_failures([failure])
    assert len(signals) == 1
    return signals[0]


def test_resolved_scope_contains_source_and_test(repo) -> None:
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == (
        "backend/mod/engine.py",   # source-locus FIRST (primary repair target)
        "tests/test_engine.py",    # test-locus retained (legit test fixes stay in scope)
    )
    att = sig.evidence["attribution"]
    assert att["status"] == "resolved"
    assert att["source_loci"] == ["backend/mod/engine.py"]
    assert att["test_locus"] == "tests/test_engine.py"


def test_unresolved_fail_fast_keeps_test_scope_and_marks_evidence(repo) -> None:
    (repo / "tests" / "test_lonely.py").write_text(
        "import os\ndef test_x():\n    assert os.sep\n"
    )
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_lonely.py"))
    assert sig.target_files == ("tests/test_lonely.py",)
    att = sig.evidence["attribution"]
    assert att["status"] == "unresolved"
    assert att["reason"] == "no_first_party_source_imports"


def test_master_switch_off_is_byte_identical_legacy(repo, monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == ("tests/test_engine.py",)
    assert sig.evidence["attribution"]["status"] == "disabled"


def test_attributor_crash_never_blocks_signal(repo, monkeypatch) -> None:
    """A broken attributor must degrade to legacy scope, not eat the
    signal (fail-soft on unexpected faults; fail-FAST is reserved for
    the typed AttributionUnresolved)."""
    import backend.core.ouroboros.governance.intent.test_watcher as tw

    def _boom(*a, **k):
        raise RuntimeError("attributor exploded")

    monkeypatch.setattr(tw, "attribute_test_to_sources", _boom)
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.target_files == ("tests/test_engine.py",)


def test_evidence_signature_unchanged_for_dedup_continuity(repo) -> None:
    """The dedup 'signature' field format must not change (existing
    dedup behavior keyed on it)."""
    w = TestWatcher(repo="jarvis", repo_path=str(repo))
    sig = _stable_signal(w, _fail("tests/test_engine.py"))
    assert sig.evidence["signature"] == "AssertionError: boom:tests/test_engine.py"


# ---- C1: poll_once pre-warms the module-map OFF-loop, red-cycles only ----

import backend.core.ouroboros.governance.intent.test_watcher as tw_mod


async def _run_poll_with_spy(repo_root, monkeypatch, *, red: bool):
    """Drive one poll_once with run_pytest/parse stubbed; count prewarms."""
    w = TestWatcher(repo="jarvis", repo_path=str(repo_root))

    async def _fake_run_pytest(*_a, **_k):
        return ("output", 1 if red else 0)

    monkeypatch.setattr(
        w, "run_pytest", _fake_run_pytest, raising=True,
    )
    failures = [_fail("tests/test_engine.py")] if red else []
    monkeypatch.setattr(
        w, "parse_pytest_output", lambda *_a, **_k: failures, raising=True,
    )

    calls = {"n": 0}

    async def _spy_prewarm(root):
        calls["n"] += 1

    monkeypatch.setattr(tw_mod, "prewarm_module_map", _spy_prewarm, raising=True)
    await w.poll_once()
    return calls["n"]


async def test_red_poll_prewarms_exactly_once(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "true")
    n = await _run_poll_with_spy(repo, monkeypatch, red=True)
    assert n == 1


async def test_green_poll_never_prewarms(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "true")
    n = await _run_poll_with_spy(repo, monkeypatch, red=False)
    assert n == 0


async def test_red_poll_skips_prewarm_when_attribution_disabled(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_TEST_SOURCE_ATTRIBUTION_ENABLED", "false")
    n = await _run_poll_with_spy(repo, monkeypatch, red=True)
    assert n == 0
