"""The ONE applier, and what a diff measures against the source it was shown.

Reactor's reward loads ``diff_apply`` by path and grades a diff by placing
it -- so these pin (a) that providers and the module are one applier, (b)
that the module loads outside the package (Reactor's venv), and (c) that
the reader understands the prompts O+V's builders actually render, current
and legacy, because the corpus the trainer replays is full of legacy ones.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import diff_apply as da
from backend.core.ouroboros.governance import providers as pv
from backend.core.ouroboros.governance import source_lines as sl

BAR = sl.SEPARATOR
SRC = "import os\n\n\ndef f(x):\n    return x + 1\n\n\ndef g():\n    return os.sep\n"
GOOD = "@@ -4,2 +4,2 @@\n def f(x):\n-    return x + 1\n+    return x + 2\n"
HALLUCINATED = "@@ -4,2 +4,2 @@\n def f(y, z):\n-    return y * z\n+    return 0\n"


def _section(path: str, body: str, meta: str = "") -> str:
    return (f"## File: {path} [SHA-256: abc123def456] [{len(body.encode())} bytes, "
            f"{body.count(chr(10))} lines]{meta}\n```\n{body}\n```\n\n## Output Schema\n")


def test_providers_and_the_module_are_one_applier() -> None:
    assert pv._apply_unified_diff is da.apply_unified_diff
    assert pv._parse_unified_hunks is da.parse_unified_hunks
    assert da.apply_unified_diff(SRC, GOOD) == SRC.replace("x + 1", "x + 2")


def test_the_module_loads_by_path_outside_the_package(tmp_path: Path) -> None:
    """Reactor's venv cannot import `backend`: the file and its sibling must
    load by path alone."""
    spec = importlib.util.spec_from_file_location("_probe_diff_apply", da.__file__)
    mod = importlib.util.module_from_spec(spec)
    saved = {k: sys.modules.pop(k) for k in list(sys.modules) if k.startswith("backend")}
    try:
        sys.modules["_probe_diff_apply"] = mod
        import builtins
        real = builtins.__import__

        def no_backend(name, *a, **k):
            if name.startswith("backend"):
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = no_backend
        try:
            spec.loader.exec_module(mod)
        finally:
            builtins.__import__ = real
        assert mod.apply_unified_diff(SRC, GOOD) == SRC.replace("x + 1", "x + 2")
    finally:
        sys.modules.update(saved)
        sys.modules.pop("_probe_diff_apply", None)


def test_a_numbered_whole_snapshot_reads_back_exactly() -> None:
    known = da.source_from_prompt(_section("m.py", sl.number(SRC)), "m.py")
    assert known.complete and known.text == SRC


def test_a_legacy_unnumbered_snapshot_reads_back_even_with_a_fence_inside() -> None:
    body = 'DOC = """\n```\nexample\n```\n"""\n' + SRC
    known = da.source_from_prompt(_section("m.py", body), "m.py")
    assert known.complete and known.text == body


def test_a_truncated_snapshot_keeps_only_whole_lines_at_their_positions(tmp_path: Path) -> None:
    f = tmp_path / "big.py"
    f.write_text("".join(f"v{i} = {i}\n" for i in range(1, 2001)))
    shown = pv._read_with_truncation(f, max_chars=4000, head_chars=1000, tail_chars=1000, numbered=True)
    known = da.source_from_prompt(_section("big.py", shown), "big.py")
    assert not known.complete
    assert known.lines[1] == "v1 = 1" and known.lines[2000] == "v2000 = 2000"
    cut = [n for n in range(1, 2001) if n not in known.lines]
    assert all(known.lines[n] == f"v{n} = {n}" for n in known.lines)
    assert cut, "the boundary fragments are dropped"


def test_a_lean_region_reads_in_both_numberings() -> None:
    region_new = "\n".join(sl.number_lines(SRC.splitlines()[3:5], 4))
    legacy = "\n".join(f"{n:4d} | {t}" for n, t in zip((4, 5), SRC.splitlines()[3:5]))
    for region in (region_new, legacy):
        prompt = (f"## Target: m.py [SHA-256: abc] [{len(SRC)} bytes, 9 lines]\n\n"
                  "## Structural Index (what already exists — DO NOT duplicate)\n\n- L4 def f(x)\n"
                  f"### Target Region (use `read_file(\"m.py\")` for full content)\n```\n"
                  f"[... 3 lines above ...]\n{region}\n[... 4 lines below ...]\n```\n")
        known = da.source_from_prompt(prompt, "m.py")
        assert known.lines == {4: "def f(x):", 5: "    return x + 1"} and not known.complete


def test_an_ast_outline_is_not_the_file() -> None:
    assert da.source_from_prompt(_section("m.py", SRC, " [AST-SLICED]"), "m.py") is None


def test_alignment_separates_a_landing_diff_from_an_invented_one() -> None:
    known = da.source_from_prompt(_section("m.py", sl.number(SRC)), "m.py")
    good = da.alignment(known, GOOD)
    assert good["applies"] is True and good["patched"] == SRC.replace("x + 1", "x + 2")
    assert good["hunks"][0]["header_error"] == 0
    bad = da.alignment(known, HALLUCINATED)
    assert bad["applies"] is False and bad["patched"] is None
    h = bad["hunks"][0]
    assert h["checkable"] and not h["placed"] and h["present"] == 0.0


def test_a_header_far_from_where_the_hunk_lands_is_measured() -> None:
    known = da.source_from_prompt(_section("m.py", sl.number(SRC)), "m.py")
    rep = da.alignment(known, GOOD.replace("@@ -4,2 +4,2 @@", "@@ -1,2 +1,2 @@"))
    assert rep["applies"] and rep["hunks"][0]["header_error"] == 3


def test_a_hunk_aimed_outside_a_partial_region_is_not_judged() -> None:
    known = da.KnownSource({4: "def f(x):", 5: "    return x + 1"}, complete=False)
    far = "@@ -200,2 +200,2 @@\n def h():\n-    return 1\n+    return 2\n"
    rep = da.alignment(known, far)
    assert rep["applies"] is None and not rep["hunks"][0]["checkable"]
    near = da.alignment(known, GOOD)
    assert near["hunks"][0]["placed"] and near["patched"] is None


def test_copied_numbers_are_counted_and_still_land() -> None:
    known = da.source_from_prompt(_section("m.py", sl.number(SRC)), "m.py")
    echoed = (f"@@ -1,2 +1,2 @@\n {sl.number_lines(['def f(x):'], 4)[0]}\n"
              f"-{sl.number_lines(['    return x + 1'], 5)[0]}\n+    return x + 2\n")
    rep = da.alignment(known, echoed)
    assert rep["applies"] and rep["hunks"][0]["echoed"] == 2


def test_the_reader_understands_the_full_builder_it_serves(tmp_path: Path, monkeypatch) -> None:
    """Rendered by O+V's own builder, read back by the module: the two
    cannot drift apart unnoticed."""
    monkeypatch.setenv("JARVIS_NUMBERED_SOURCE_ENABLED", "true")
    (tmp_path / "m.py").write_text(SRC)
    from backend.core.ouroboros.governance.op_context import OperationContext
    ctx = OperationContext.create(target_files=("m.py",), description="make f add two",
                                  op_id="op-diff-apply-probe")
    prompt = pv._build_codegen_prompt(ctx, repo_root=tmp_path)
    known = da.source_from_prompt(prompt, "m.py")
    assert known is not None and known.complete and known.text == SRC
