"""764 sessions with a summary produced exactly ONE notebook, ten weeks stale.

Two defects stacked, and neither was visible:

  * `report.ipynb` was written to a shared `notebooks/` directory under a FIXED
    name, so every session overwrote the previous one.
  * the failure was swallowed by a bare `except` on the shutdown hook, so the
    overwriting stopped working entirely and nobody heard about it.

A report is worth little; evidence that it is missing is worth a lot. These hold
both halves.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _summary(tmp_path: Path) -> Path:
    path = tmp_path / "summary.json"
    path.write_text(json.dumps({
        "session_id": "bt-test", "schema_version": 2,
        "session_outcome": "complete", "ops": [],
    }))
    return path


class TestSessionIsolation:
    def test_the_notebook_lands_in_the_session_directory(self, tmp_path):
        """Beside the `summary.json` it is derived from. A global folder is what
        let 764 sessions collapse into one file."""
        from backend.core.ouroboros.battle_test.notebook_generator import (
            NotebookGenerator,
        )
        out = NotebookGenerator(summary_path=_summary(tmp_path)).generate(
            output_dir=tmp_path)
        assert Path(out).parent == tmp_path
        assert Path(out).exists()

    def test_two_sessions_do_not_overwrite_each_other(self, tmp_path):
        """The actual data loss, reproduced: same filename, different sessions."""
        from backend.core.ouroboros.battle_test.notebook_generator import (
            NotebookGenerator,
        )
        first, second = tmp_path / "s1", tmp_path / "s2"
        for d in (first, second):
            d.mkdir()
        a = NotebookGenerator(summary_path=_summary(first)).generate(
            output_dir=first)
        b = NotebookGenerator(summary_path=_summary(second)).generate(
            output_dir=second)
        assert Path(a) != Path(b)
        assert Path(a).exists() and Path(b).exists()

    def test_the_harness_passes_its_own_session_dir(self):
        """Structural, by AST: the comments here name `notebooks/` in prose to
        explain what was wrong, so a substring search matches the explanation."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import harness as h

        src = inspect.getsource(h)
        tree = ast.parse(src)
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "attr", "") == "generate"
        ]
        assert calls, "the harness no longer generates a notebook"
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "output_dir":
                    assert "_session_dir" in ast.dump(kw.value), (
                        "the notebook is being written outside the session "
                        "directory again")


class TestAtomicWrites:
    def test_a_completed_write_leaves_no_temp_file(self, tmp_path):
        from backend.core.ouroboros.battle_test.notebook_generator import (
            _atomic_write,
        )
        _atomic_write(tmp_path / "report.ipynb",
                      lambda tmp: tmp.write_text("{}"))
        assert not list(tmp_path.glob(".*tmp")), "temp litter left behind"

    def test_an_interrupted_write_leaves_NO_partial_artifact(self, tmp_path):
        """A truncated `.ipynb` is worse than an absent one: it looks like an
        artifact and fails only when someone opens it months later."""
        from backend.core.ouroboros.battle_test.notebook_generator import (
            _atomic_write,
        )
        target = tmp_path / "report.ipynb"

        def _die(tmp: Path) -> None:
            tmp.write_text('{"cells": [')      # a half-written notebook
            raise KeyboardInterrupt("SIGTERM mid-write")

        with pytest.raises(KeyboardInterrupt):
            _atomic_write(target, _die)
        assert not target.exists(), "a partial notebook was committed"
        assert not list(tmp_path.glob(".*tmp"))

    def test_an_existing_notebook_survives_a_failed_rewrite(self, tmp_path):
        """`os.replace` is atomic: a reader sees the OLD file or the complete new
        one, never a half-written one."""
        from backend.core.ouroboros.battle_test.notebook_generator import (
            _atomic_write,
        )
        target = tmp_path / "report.ipynb"
        target.write_text("ORIGINAL")
        with pytest.raises(RuntimeError):
            _atomic_write(target,
                          lambda tmp: (_ for _ in ()).throw(RuntimeError("x")))
        assert target.read_text() == "ORIGINAL"


class TestTheFaultTombstone:
    def test_a_failure_writes_a_tombstone_in_that_session(self, tmp_path):
        """Teardown may have unmounted the UI, so there is no reliable surface for
        a panic. The traceback goes next to the artifact that is missing."""
        from backend.core.ouroboros.battle_test.harness import (
            _write_notebook_fault,
        )
        try:
            raise RuntimeError("no space left on device")
        except RuntimeError as exc:
            assert _write_notebook_fault(tmp_path, exc) is True
        tomb = tmp_path / ".notebook_fault.log"
        assert tomb.exists()
        body = tomb.read_text()
        assert "no space left on device" in body
        assert "RuntimeError" in body and "Traceback" in body

    def test_the_tombstone_never_raises_into_the_shutdown_sequence(self):
        """A tombstone that can take down teardown is worse than no tombstone."""
        from backend.core.ouroboros.battle_test.harness import (
            _write_notebook_fault,
        )
        assert _write_notebook_fault(Path("/definitely/not/here"),
                                     RuntimeError("x")) is False
        assert _write_notebook_fault(None, RuntimeError("x")) is False

    def test_the_harness_no_longer_swallows_the_failure_silently(self):
        """The bare `except` logged a warning nobody reads from a shutdown hook —
        which is precisely how ten weeks passed. It must now leave an artifact."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import harness as h

        src = inspect.getsource(h)
        idx = src.find("Notebook generation failed")
        assert idx != -1, "the notebook failure handler is gone"
        window = src[idx:idx + 400]
        assert "_write_notebook_fault" in window, (
            "the failure is logged but leaves no tombstone — the exact silence "
            "this fix exists to end")
