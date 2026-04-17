"""DiffPreviewRenderer V1 tests — Rich NOTIFY_APPLY preview.

Uses ``Console(record=True)`` to capture the rendered output as plain
text and assert on key strings / structures. This validates:
  • Header contents (op id, reason, stats roll-up, countdown)
  • File-tree appears only for multi-file changes (skipped for N=1)
  • Status badges (``[+ new]`` / ``[~ modified]`` / ``[− deleted]``)
  • Truncation at ``JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE``
  • Binary-file safeguard
  • Kill-switch + TTY gate (``should_render``)
  • Dump-path writes a real file and is silent on unset
  • AST canary that orchestrator.py + serpent_flow.py still call the
    V1 renderer — so a future refactor can't silently revert to the
    legacy 4000-char plain-text preview
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test import diff_preview as dp
from backend.core.ouroboros.battle_test.diff_preview import (
    DiffPreviewRenderer,
    FileChange,
    build_changes_from_candidate,
    dump_full_diff,
    preview_enabled,
    should_render,
)


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_UI_DIFF_PREVIEW_") or key.startswith(
            "JARVIS_DIFF_PREVIEW_"
        ):
            monkeypatch.delenv(key, raising=False)
    yield


def _record(renderable, width: int = 120) -> str:
    """Render a Rich renderable via Console(record=True) and return the
    exported plain text. ``force_terminal=True`` makes the recording
    path behave like a real TTY so styled text is emitted."""
    from rich.console import Console
    console = Console(record=True, width=width, force_terminal=True)
    console.print(renderable)
    return console.export_text()


# ---------------------------------------------------------------------------
# (1) Env gate + TTY gate
# ---------------------------------------------------------------------------


def test_preview_enabled_default_on():
    assert preview_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "off", "no"])
def test_preview_disabled_values(monkeypatch, value):
    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", value)
    assert preview_enabled() is False


def test_should_render_requires_terminal_on_console_when_provided():
    """When a Rich console is passed, its ``is_terminal`` wins over
    stdout heuristics — the TTY gate follows the explicit console."""
    from rich.console import Console
    # force_terminal=True → is_terminal=True
    c_term = Console(force_terminal=True)
    assert should_render(c_term) is True
    # file=<StringIO> → is_terminal=False
    import io
    c_noterm = Console(file=io.StringIO(), force_terminal=False)
    assert should_render(c_noterm) is False


def test_should_render_false_when_env_disabled(monkeypatch):
    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", "0")
    from rich.console import Console
    assert should_render(Console(force_terminal=True)) is False


# ---------------------------------------------------------------------------
# (2) FileChange status derivation
# ---------------------------------------------------------------------------


def test_status_derivation_new_file():
    c = FileChange(path="a.py", old_content="", new_content="print('hi')\n")
    assert c.status == "new"
    assert c.added_lines == 1
    assert c.removed_lines == 0


def test_status_derivation_modified():
    c = FileChange(path="a.py", old_content="a\nb\n", new_content="a\nc\n")
    assert c.status == "modified"
    assert c.added_lines == 1
    assert c.removed_lines == 1


def test_status_derivation_deleted():
    c = FileChange(path="a.py", old_content="a\nb\n", new_content="")
    assert c.status == "deleted"
    assert c.removed_lines == 2
    assert c.added_lines == 0


def test_status_derivation_unchanged():
    c = FileChange(path="a.py", old_content="a\n", new_content="a\n")
    assert c.status == "unchanged"


# ---------------------------------------------------------------------------
# (3) Header: op_id + reason + stats roll-up + countdown
# ---------------------------------------------------------------------------


def test_header_contains_op_id_and_reason_and_countdown():
    renderer = DiffPreviewRenderer()
    changes = [
        FileChange(path="foo.py", old_content="a\n", new_content="b\n"),
    ]
    panel = renderer.build(
        op_id="op-ABC-123",
        reason="single_file_small_diff",
        changes=changes,
        delay_remaining_s=4.7,
    )
    text = _record(panel, width=140)
    assert "⚠ NOTIFY_APPLY" in text
    assert "op-ABC-123" in text
    assert "single_file_small_diff" in text
    assert "Applying in" in text
    assert "4.7s" in text
    assert "/reject" in text


def test_header_stats_rollup_multifile():
    renderer = DiffPreviewRenderer()
    changes = [
        FileChange(path="a.py", old_content="x\n", new_content="x\ny\nz\n"),  # +2/-0
        FileChange(path="b.py", old_content="a\nb\n", new_content=""),         # new=deleted, -2
    ]
    panel = renderer.build(
        op_id="op-1", reason="multi", changes=changes, delay_remaining_s=5.0,
    )
    text = _record(panel, width=140)
    # Stats roll-up format: "+2/-2 lines across 2 files"
    assert re.search(r"\+2/-2 lines across 2 files", text)


def test_header_stats_singular_file_phrasing():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="a.py", old_content="a\n", new_content="b\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=3.0,
    ), width=140)
    assert "1 file" in text
    assert "1 files" not in text  # singular form


# ---------------------------------------------------------------------------
# (4) File tree — present only for multi-file
# ---------------------------------------------------------------------------


def test_file_tree_skipped_for_single_file():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="solo.py", old_content="a", new_content="b")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    # "Changed files (N)" tree header should NOT appear for single file.
    assert "Changed files (1)" not in text


def test_file_tree_present_for_multifile():
    renderer = DiffPreviewRenderer()
    changes = [
        FileChange(path="a.py", old_content="x", new_content="y"),
        FileChange(path="b.py", old_content="", new_content="z\n"),
        FileChange(path="c.py", old_content="q", new_content="r"),
    ]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=160)
    assert "Changed files (3)" in text
    # Every path named in the tree region.
    assert "a.py" in text
    assert "b.py" in text
    assert "c.py" in text


# ---------------------------------------------------------------------------
# (5) Status badges
# ---------------------------------------------------------------------------


def test_badge_new_file():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="new.py", old_content="", new_content="x\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "[+ new]" in text


def test_badge_modified_file():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="mod.py", old_content="a\n", new_content="b\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "[~ modified]" in text


def test_badge_deleted_file():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="del.py", old_content="a\nb\n", new_content="")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "[− deleted]" in text


# ---------------------------------------------------------------------------
# (6) Truncation at max_lines_per_file
# ---------------------------------------------------------------------------


def test_diff_body_truncates_long_file(monkeypatch):
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE", "40")
    renderer = DiffPreviewRenderer(max_lines_per_file=40)
    # Build an old/new pair that yields >100 diff lines.
    old = "\n".join(f"line {i} old" for i in range(200))
    new = "\n".join(f"line {i} new" for i in range(200))
    changes = [FileChange(path="big.py", old_content=old, new_content=new)]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=160)
    # Must include the omission marker noting N lines were omitted.
    assert "lines omitted" in text
    # And mention the cap variable so operators know where to tune.
    assert "JARVIS_DIFF_PREVIEW_MAX_LINES_PER_FILE" in text


def test_diff_body_no_truncation_marker_when_small():
    renderer = DiffPreviewRenderer(max_lines_per_file=500)
    changes = [FileChange(path="tiny.py", old_content="a\nb\n", new_content="a\nc\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "lines omitted" not in text


# ---------------------------------------------------------------------------
# (7) Binary safeguard
# ---------------------------------------------------------------------------


def test_binary_file_shows_sentinel_instead_of_diff():
    renderer = DiffPreviewRenderer()
    changes = [
        FileChange(
            path="blob.bin",
            old_content="",
            new_content="\x00\x01\x02BINARY",
            is_binary=True,
        ),
    ]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "binary file" in text.lower()
    # Raw diff content must NOT appear.
    assert "BINARY" not in text


def test_looks_binary_heuristic():
    assert dp._looks_binary("") is False
    assert dp._looks_binary("pure text\n" * 100) is False
    assert dp._looks_binary("x\x00y") is True
    # >30% nonprintable → binary
    nonprint = "\x01" * 100 + "abc"
    assert dp._looks_binary(nonprint) is True


# ---------------------------------------------------------------------------
# (8) Rationale surfaced per file
# ---------------------------------------------------------------------------


def test_rationale_appears_in_file_panel():
    renderer = DiffPreviewRenderer()
    changes = [
        FileChange(
            path="foo.py", old_content="a\n", new_content="b\n",
            rationale="extract retry helper into _retry_with_backoff",
        ),
    ]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "rationale" in text.lower()
    assert "_retry_with_backoff" in text


def test_no_rationale_line_when_absent():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="foo.py", old_content="a\n", new_content="b\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    # The "rationale:" label shouldn't appear when the rationale string is empty.
    assert "rationale:" not in text.lower()


# ---------------------------------------------------------------------------
# (9) Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_unchanged_file_shows_no_textual_changes():
    renderer = DiffPreviewRenderer()
    changes = [FileChange(path="x.py", old_content="same\n", new_content="same\n")]
    text = _record(renderer.build(
        op_id="op-1", reason="x", changes=changes, delay_remaining_s=1.0,
    ), width=140)
    assert "no textual changes" in text.lower() or "[= unchanged]" in text


def test_empty_changes_list_still_renders_header():
    """Degenerate empty-changes call should not crash; header still shows."""
    renderer = DiffPreviewRenderer()
    panel = renderer.build(
        op_id="op-1", reason="no-op", changes=[], delay_remaining_s=2.0,
    )
    text = _record(panel, width=120)
    assert "NOTIFY_APPLY" in text
    assert "op-1" in text


# ---------------------------------------------------------------------------
# (10) build_changes_from_candidate — legacy single + multi-file shapes
# ---------------------------------------------------------------------------


def test_build_changes_single_file_legacy_shape(tmp_path):
    """Legacy candidate with ``file_path`` + ``full_content`` only."""
    target = tmp_path / "foo.py"
    target.write_text("old content\n")
    candidate = {
        "file_path": "foo.py",
        "full_content": "new content\n",
    }
    changes = build_changes_from_candidate(candidate, tmp_path)
    assert len(changes) == 1
    c = changes[0]
    assert c.path == "foo.py"
    assert c.old_content == "old content\n"
    assert c.new_content == "new content\n"
    assert c.status == "modified"


def test_build_changes_multi_file_shape(tmp_path):
    """``files: [...]`` list — each entry becomes a FileChange."""
    (tmp_path / "a.py").write_text("A\n")
    (tmp_path / "b.py").write_text("B\n")
    candidate = {
        "file_path": "a.py",
        "full_content": "A_new\n",
        "files": [
            {"file_path": "a.py", "full_content": "A_new\n", "rationale": "rA"},
            {"file_path": "b.py", "full_content": "B_new\n", "rationale": "rB"},
            {"file_path": "c.py", "full_content": "C_new\n", "rationale": "rC"},
        ],
    }
    changes = build_changes_from_candidate(candidate, tmp_path)
    assert [c.path for c in changes] == ["a.py", "b.py", "c.py"]
    # c.py doesn't exist on disk → status "new"
    new_file = next(c for c in changes if c.path == "c.py")
    assert new_file.status == "new"
    assert new_file.old_content == ""
    assert new_file.rationale == "rC"


def test_build_changes_handles_missing_file_on_disk(tmp_path):
    """Candidate points at a path that doesn't exist — treated as new file."""
    candidate = {"file_path": "new_file.py", "full_content": "x = 1\n"}
    changes = build_changes_from_candidate(candidate, tmp_path)
    assert len(changes) == 1
    assert changes[0].status == "new"
    assert changes[0].old_content == ""


def test_build_changes_respects_multi_file_disable(tmp_path, monkeypatch):
    """With JARVIS_MULTI_FILE_GEN_ENABLED=false, ``files: [...]`` is
    ignored and only the primary file_path is emitted."""
    monkeypatch.setenv("JARVIS_MULTI_FILE_GEN_ENABLED", "false")
    (tmp_path / "a.py").write_text("A\n")
    candidate = {
        "file_path": "a.py",
        "full_content": "A_new\n",
        "files": [
            {"file_path": "a.py", "full_content": "A_new\n"},
            {"file_path": "b.py", "full_content": "B_new\n"},
        ],
    }
    changes = build_changes_from_candidate(candidate, tmp_path)
    assert [c.path for c in changes] == ["a.py"]


# ---------------------------------------------------------------------------
# (11) Dump-to-disk — JARVIS_DIFF_PREVIEW_DUMP_PATH
# ---------------------------------------------------------------------------


def test_dump_full_diff_writes_file_when_path_set(tmp_path, monkeypatch):
    dump_dir = tmp_path / "diffs"
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(dump_dir))
    changes = [
        FileChange(
            path="foo.py", old_content="a\nb\n", new_content="a\nc\n",
            rationale="test rationale",
        ),
    ]
    result = dump_full_diff(op_id="op-dump-1", changes=changes)
    assert result is not None
    assert result.is_file()
    content = result.read_text()
    assert "op-dump-1" in content
    assert "foo.py" in content
    assert "test rationale" in content
    # Unified diff format markers present.
    assert "---" in content and "+++" in content


def test_dump_full_diff_silent_when_path_unset():
    """No env set → returns None, no file written."""
    changes = [FileChange(path="foo.py", old_content="", new_content="x\n")]
    result = dump_full_diff(op_id="op-x", changes=changes)
    assert result is None


def test_dump_full_diff_sanitizes_op_id_for_filename(tmp_path, monkeypatch):
    """Op id with suspicious chars must not escape the dump dir."""
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(tmp_path))
    changes = [FileChange(path="x.py", old_content="", new_content="y")]
    result = dump_full_diff(
        op_id="../../etc/passwd", changes=changes,
    )
    # Must still land *inside* tmp_path — sanitization kept it safe.
    assert result is not None
    assert result.is_file()
    assert tmp_path in result.parents


def test_dump_full_diff_binary_file_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(tmp_path))
    changes = [
        FileChange(
            path="blob.bin", old_content="",
            new_content="\x00\x01\x02binary",
            is_binary=True,
        ),
    ]
    result = dump_full_diff(op_id="op-bin", changes=changes)
    assert result is not None
    content = result.read_text()
    assert "binary" in content.lower()
    # Raw binary content must NOT appear.
    assert "\x00" not in content


# ---------------------------------------------------------------------------
# (12) AST canary — orchestrator + serpent_flow still call V1 surface
# ---------------------------------------------------------------------------


def _read(path_parts: tuple) -> str:
    base = Path(__file__).resolve().parent.parent.parent
    return (base.joinpath(*path_parts)).read_text(encoding="utf-8")


def test_orchestrator_calls_build_changes_from_candidate():
    """Static guard: orchestrator.py must invoke
    ``build_changes_from_candidate`` on the NOTIFY_APPLY path, else
    the rich preview regresses silently to legacy plain text."""
    src = _read((
        "backend", "core", "ouroboros", "governance", "orchestrator.py",
    ))
    assert "build_changes_from_candidate" in src, (
        "orchestrator.py no longer references build_changes_from_candidate — "
        "NOTIFY_APPLY preview will silently regress to the legacy 4000-char "
        "plain-text path."
    )
    # Confirm it's CALLED, not just imported.
    tree = ast.parse(src)
    called = any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id == "build_changes_from_candidate"
        for n in ast.walk(tree)
    )
    assert called


def test_serpent_flow_defines_show_notify_apply_preview():
    """Static guard: SerpentFlow must still expose the V1 method."""
    src = _read((
        "backend", "core", "ouroboros", "battle_test", "serpent_flow.py",
    ))
    tree = ast.parse(src)
    found = any(
        isinstance(n, ast.AsyncFunctionDef)
        and n.name == "show_notify_apply_preview"
        for n in ast.walk(tree)
    )
    assert found, (
        "SerpentFlow.show_notify_apply_preview missing — orchestrator's "
        "NOTIFY_APPLY path will fall through to legacy plain-sleep."
    )


def test_orchestrator_calls_show_notify_apply_preview():
    src = _read((
        "backend", "core", "ouroboros", "governance", "orchestrator.py",
    ))
    assert "show_notify_apply_preview" in src


# ---------------------------------------------------------------------------
# (13) Functional end-to-end — drive show_notify_apply_preview directly
# ---------------------------------------------------------------------------
#
# These tests drive the async wrapper that the orchestrator's
# NOTIFY_APPLY hook calls, mirroring the production call pattern
# (op_id + reason + changes + delay + cancel_check). They cover the
# three concrete paths:
#   • TTY + env on + full changes  → rich Live path + dump written
#   • cancel_check returns True    → short-circuit, dump still attempted
#   • env off (kill switch)        → plain-sleep fallback, no dump


def _build_serpent_flow_for_test():
    """Minimal SerpentFlow instance suitable for async wrapper tests.

    Uses ``force_terminal=True`` so ``should_render`` passes its TTY gate.
    Avoids REPL / prompt_toolkit side effects which aren't part of the
    preview path.
    """
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow
    flow = SerpentFlow(
        session_id="bt-test",
        branch_name="",
        cost_cap_usd=0.1,
        idle_timeout_s=10.0,
    )
    # Force the console into terminal mode so should_render() passes.
    # Rich respects this flag even when stdout isn't actually a tty.
    from rich.console import Console
    flow.console = Console(
        record=True, width=140, force_terminal=True,
    )
    return flow


def test_show_notify_apply_preview_completes_and_writes_dump(
    tmp_path, monkeypatch,
):
    """End-to-end: short delay, env-enabled TTY console, dump path set.
    Must return False (no cancel), write the dump file, and have an
    op-id-bearing file on disk matching the canonical dump name."""
    import asyncio

    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(tmp_path))

    flow = _build_serpent_flow_for_test()
    changes = [
        FileChange(
            path="demo.py",
            old_content="def foo():\n    return 1\n",
            new_content="def foo():\n    return 2\n",
            rationale="numeric correction",
        ),
    ]

    async def _run():
        return await flow.show_notify_apply_preview(
            op_id="op-functional-1",
            reason="single_file_small_diff",
            changes=changes,
            delay_s=0.3,   # keep test fast; wrapper ticks at 250ms cadence
            cancel_check=None,
        )

    cancelled = asyncio.run(_run())
    assert cancelled is False
    dump_file = tmp_path / "op-functional-1.diff"
    assert dump_file.is_file(), (
        f"dump file missing; dir contents: {list(tmp_path.iterdir())}"
    )
    content = dump_file.read_text()
    assert "op-functional-1" in content
    assert "demo.py" in content
    assert "numeric correction" in content


def test_show_notify_apply_preview_honors_cancel_check(tmp_path, monkeypatch):
    """cancel_check returning True must short-circuit the countdown
    quickly (well under delay_s). Dump still attempted before the
    cancellation so operators can review the rejected candidate."""
    import asyncio
    import time

    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", "1")
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(tmp_path))

    flow = _build_serpent_flow_for_test()
    changes = [
        FileChange(path="x.py", old_content="a\n", new_content="b\n"),
    ]

    # Cancel becomes True after the first tick — ensures the polling
    # path fires and the wrapper returns quickly (not at delay_s).
    cancel_called = {"n": 0}

    def _cancel_check():
        cancel_called["n"] += 1
        # First call: not yet; second call: yes.
        return cancel_called["n"] >= 2

    async def _run():
        t0 = time.monotonic()
        result = await flow.show_notify_apply_preview(
            op_id="op-cancel-1",
            reason="test_cancel",
            changes=changes,
            delay_s=5.0,  # long nominal delay to prove cancel short-circuits
            cancel_check=_cancel_check,
        )
        elapsed = time.monotonic() - t0
        return (result, elapsed)

    result, elapsed = asyncio.run(_run())
    assert result is True, "cancel_check returning True must yield True"
    assert elapsed < 2.0, (
        f"cancel should short-circuit; elapsed={elapsed:.2f}s"
    )
    # Dump was attempted pre-countdown, so file should exist.
    assert (tmp_path / "op-cancel-1.diff").is_file()


def test_show_notify_apply_preview_plain_fallback_when_kill_switch_off(
    tmp_path, monkeypatch,
):
    """Kill-switch env=0 forces the plain-sleep fallback path: no
    rich Live widget is constructed, no exception raised, and the
    legacy delay still runs so /reject still has its window.

    Note: the dump path is an **orthogonal** gate — if
    ``JARVIS_DIFF_PREVIEW_DUMP_PATH`` is set, the dump is still
    written regardless of kill-switch state (operators may want
    the disk artifact for review even when they've disabled the
    rich Live panel). That's by design, not a bug.
    """
    import asyncio
    import time

    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", "0")
    # Intentionally NOT setting the dump path here — proves the plain
    # fallback path doesn't produce on-disk artifacts when no dump is
    # requested (the common headless-CI case).

    flow = _build_serpent_flow_for_test()
    changes = [FileChange(path="x.py", old_content="a\n", new_content="b\n")]

    async def _run():
        t0 = time.monotonic()
        result = await flow.show_notify_apply_preview(
            op_id="op-killswitch",
            reason="r",
            changes=changes,
            delay_s=0.3,
            cancel_check=None,
        )
        return (result, time.monotonic() - t0)

    result, elapsed = asyncio.run(_run())
    assert result is False
    # Delay still honored (operator still has a /reject window even
    # in fallback mode).
    assert elapsed >= 0.25, (
        f"fallback must still honor delay_s; elapsed={elapsed:.2f}s"
    )
    # No dump file because no dump path was configured for this test.
    assert not (tmp_path / "op-killswitch.diff").exists()


def test_show_notify_apply_preview_dump_is_orthogonal_to_kill_switch(
    tmp_path, monkeypatch,
):
    """Dump path set + kill-switch off: dump still writes even though
    the rich panel is suppressed. Operators who run headless but want
    an on-disk artifact for review can combine these settings."""
    import asyncio

    monkeypatch.setenv("JARVIS_UI_DIFF_PREVIEW_ENABLED", "0")
    monkeypatch.setenv("JARVIS_DIFF_PREVIEW_DUMP_PATH", str(tmp_path))

    flow = _build_serpent_flow_for_test()
    changes = [FileChange(path="y.py", old_content="a\n", new_content="b\n")]

    async def _run():
        return await flow.show_notify_apply_preview(
            op_id="op-dump-only",
            reason="r",
            changes=changes,
            delay_s=0.2,
            cancel_check=None,
        )

    asyncio.run(_run())
    # Dump file landed even though rich was disabled.
    assert (tmp_path / "op-dump-only.diff").is_file()
