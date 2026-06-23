"""Tests for the Autonomous Epistemic Memory Matrix pure logic (no live model/gh)."""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "epistemic_memory_ingest",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "epistemic_memory_ingest.py",
)
em = importlib.util.module_from_spec(_SPEC)
sys.modules["epistemic_memory_ingest"] = em
_SPEC.loader.exec_module(em)  # type: ignore[union-attr]


_DIFF = """diff --git a/m.py b/m.py
+++ b/m.py
@@ -10,3 +10,6 @@ def made_forward_progress(self):
-        return self.emitted_count >= 1
+        return self.emitted_count >= 1 or self.emitted_this_tick >= 1
+    async def new_async_helper(self):
+        return 1
"""


def test_parse_diff_structure():
    st = em.parse_diff_structure(_DIFF)
    assert "m.py" in st["files"]
    assert any("made_forward_progress" in s for s in st["symbols"])
    assert any("new_async_helper" in s for s in st["symbols"])
    assert st["added"] >= 3 and st["removed"] >= 1


def test_parse_diff_failsoft():
    assert em.parse_diff_structure(None)["files"] == []
    assert em.parse_diff_structure("garbage\nno diff")["symbols"] == []


def test_deterministic_lesson_never_empty():
    st = {"symbols": ["def foo"], "files": ["x.py"]}
    assert em._deterministic_lesson(st, "fix the thing").strip()
    assert em._deterministic_lesson({"symbols": [], "files": []}, "").strip()


def test_inject_is_append_only_and_dedup():
    with tempfile.TemporaryDirectory() as d:
        cr = str(pathlib.Path(d) / ".cursorrules")
        ok1, r1 = em.inject_constraint("Always gate on dispatched_this_tick.", pr_number=1, sha="aaaa1111", cursorrules_path=cr)
        ok2, r2 = em.inject_constraint("always gate on DISPATCHED_THIS_TICK.", pr_number=1, sha="aaaa1111", cursorrules_path=cr)  # same lesson (case/space norm)
        ok3, r3 = em.inject_constraint("Never block a valid request on a sanitize error.", pr_number=2, sha="bbbb", cursorrules_path=cr)
        body = pathlib.Path(cr).read_text()
        assert ok1 and "injected" in r1
        assert (not ok2) and "duplicate" in r2          # content-hash dedup
        assert ok3
        assert em._BEGIN in body and em._END in body
        assert body.count("- [#") == 2                  # only the two distinct rules
        # append-only: the END marker is last in the block, both rules before it
        assert body.index("dispatched_this_tick") < body.index(em._END)
        assert body.index("sanitize error") < body.index(em._END)


def test_rule_line_has_provenance_and_hash():
    line = em._rule_line("Always X because Y.", 69662, "2390b5cdeadbeef")
    assert "[#69662]" in line and "sha:2390b5c" in line and "<!--h:" in line


@pytest.mark.asyncio
async def test_abstract_lesson_falls_back_without_model(monkeypatch):
    # force the model path off -> deterministic, never raises
    lesson, source = await em.abstract_lesson(
        {"symbols": ["def g"], "files": ["a.py"]}, title="t", body="b", use_model=False)
    assert source == "deterministic" and lesson.strip()


@pytest.mark.asyncio
async def test_abstract_lesson_model_failure_is_failsoft(monkeypatch):
    # model "available" but raises -> still returns a deterministic lesson
    def _boom(*a, **k):
        raise RuntimeError("ollama down")
    monkeypatch.setattr(em, "_call_ollama_sync", _boom)
    lesson, source = await em.abstract_lesson(
        {"symbols": [], "files": []}, title="t", body="b", use_model=True)
    assert source == "deterministic" and lesson.strip()
