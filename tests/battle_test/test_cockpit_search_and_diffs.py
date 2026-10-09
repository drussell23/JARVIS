"""Search and diffs in the cockpit, the way Claude Code shows them (2026-10-09).

Found by replaying the live soak's own CommProtocol stream through SerpentFlow
and reading what the cockpit received:

* every landing ended ``💀 shed none`` / ``[✗] failed: none`` -- a success
  POSTMORTEM read as a death;
* diffs almost never appeared: the AUTO lens printed only the focused op, so a
  concurrent op's change was silent;
* an ``Update`` header printed twice per file, once with a worktree path;
* the diff was its own flat format, its gutter restarted after an elision, and
  it ended in a numbered ``... truncated`` line;
* ``▸ sandbox`` / ``▸ ledger`` / ``▸ brain_routing`` lines around every op;
* search walked ``.worktrees/`` on a host without ``rg``, so one match came
  back five times, and a malformed pattern read as "no matches".
"""
from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from backend.core.ouroboros.battle_test.serpent_flow import (
    SerpentFlow, _actionable_suggestion, _split_unified_diff,
)
from backend.core.ouroboros.battle_test.tool_render_registry import (
    ToolStatus, get_descriptor, is_elision_line,
)
from backend.core.ouroboros.battle_test.tool_render_store import BoundedBodyStore
from backend.core.ouroboros.battle_test.tool_render_view import compose
from backend.core.ouroboros.governance.comm_protocol import CommProtocol


@pytest.fixture(autouse=True)
def _unicode(monkeypatch):
    from backend.core.ouroboros.ui import theme
    monkeypatch.setattr(theme, "supports_unicode", lambda env=None: True)


def _flow(tmp_path: Path):
    flow = SerpentFlow(repo_path=tmp_path)
    flow.console = Console(file=StringIO(), width=120, color_system=None)
    lines: list = []
    flow.markup_mirror = lines.append
    return flow, lines


def _plain(lines) -> list:
    return [Text.from_markup(ln).plain for ln in lines]


def _diff(path: str, n: int) -> str:
    return (f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{n} @@\n"
            + "".join(f"+line {i}\n" for i in range(1, n + 1)))


# ------------------------------------------------------------------ lifecycle


@pytest.mark.parametrize("payload,failed", [
    ({"root_cause": "none", "failed_phase": None}, False),
    ({"root_cause": "no_op_cosmetic", "failed_phase": None}, False),
    ({"root_cause": "boom", "failed_phase": "VERIFY"}, True),
    ({"root_cause": "x", "failed_phase": "unknown"}, True),
    (None, False),
])
def test_postmortem_failure_is_the_phase_not_the_text(payload, failed):
    assert CommProtocol.postmortem_is_failure(payload) is failed


def test_a_landing_is_not_painted_as_a_death(tmp_path):
    import asyncio
    from backend.core.ouroboros.battle_test.serpent_flow import SerpentTransport
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    flow, lines = _flow(tmp_path)
    tr = SerpentTransport(flow)

    def msg(kind, payload, seq):
        return CommMessage(msg_type=kind, op_id="op-1", seq=seq,
                           causal_parent_seq=None, payload=payload)
    asyncio.run(tr.send(msg(MessageType.INTENT, {"goal": "g", "target_files": ["a.py"],
                                                 "risk_tier": "SAFE_AUTO"}, 1)))
    asyncio.run(tr.send(msg(MessageType.DECISION, {"outcome": "applied"}, 2)))
    asyncio.run(tr.send(msg(MessageType.POSTMORTEM,
                            {"root_cause": "none", "failed_phase": None}, 3)))
    text = "\n".join(_plain(lines))
    assert "shed" not in text and "failed" not in text


def test_a_restated_intent_does_not_reopen_the_op(tmp_path):
    flow, lines = _flow(tmp_path)
    flow.op_started("op-1", "goal", ["a.py"], "SAFE_AUTO")
    before = len(lines)
    flow.op_started("op-1", "goal", ["a.py"], "SAFE_AUTO")
    assert len(lines) == before


def test_phase_transitions_stay_out_of_the_transcript(tmp_path):
    flow, lines = _flow(tmp_path)
    flow.op_started("op-1", "goal", ["a.py"], "SAFE_AUTO")
    before = len(lines)
    for phase in ("GATE", "VERIFY", "sandbox", "ledger", "brain_routing", "complete"):
        flow.op_phase("op-1", phase)
    assert len(lines) == before
    flow.op_phase("op-1", "APPROVE")
    assert "Awaiting approval" in _plain(lines)[-1]


def test_one_closure_line_per_landing(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_OP_COLLAPSE_ENABLED", "true")
    flow, lines = _flow(tmp_path)
    flow.op_started("op-1", "goal", ["a.py"], "SAFE_AUTO")
    flow.show_diff("a.py", diff_text=_diff("a.py", 3), op_id="op-1")
    flow.op_completed("op-1", files_changed=[])
    text = _plain(lines)
    assert not any("✨ evolved" in t or "[✓]" in t for t in text)
    closing = [t for t in text if "evolved" in t]
    assert len(closing) == 1 and "1 file evolved" in closing[0]


# ------------------------------------------------------------------ diffs


def test_a_concurrent_ops_change_renders_despite_the_lens(tmp_path):
    """The product of an op is never noise, focused or not."""
    flow, lines = _flow(tmp_path)
    flow.op_started("op-a", "goal a", ["a.py"], "SAFE_AUTO")
    flow.op_started("op-b", "goal b", ["b.py"], "SAFE_AUTO")   # takes AUTO focus
    flow.show_diff("a.py", diff_text=_diff("a.py", 2), op_id="op-a")
    text = _plain(lines)
    assert any("Update(a.py)" in t for t in text)
    assert any("line 1" in t for t in text)


def test_each_file_renders_once_and_by_its_repo_path(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path / "wt")], check=True)
    (tmp_path / "wt" / "tests").mkdir()
    target = tmp_path / "wt" / "tests" / "t.py"
    target.write_text("x\n")
    flow, lines = _flow(tmp_path)
    flow.op_started("op-1", "goal", ["tests/t.py"], "SAFE_AUTO")
    flow.show_diff("tests/t.py", op_id="op-1")                       # announced, no diff
    flow.show_diff(str(target), diff_text=_diff(str(target), 2), op_id="op-1")
    flow.op_completed("op-1", files_changed=[])
    headers = [t for t in _plain(lines) if "Update(" in t]
    assert headers == [h for h in headers if "Update(tests/t.py)" in h]
    assert len(headers) == 1


def test_the_gutter_resumes_at_the_right_line_after_an_elision():
    r = compose("edit_file", "t.py", _diff("t.py", 60), store=BoundedBodyStore(), op_id="op")
    numbers = [int(t.split()[0]) for t in _plain(r.body_lines_markup)
               if t.split() and t.split()[0].isdigit()]
    assert numbers[-1] == 60 and numbers == sorted(numbers)


def test_context_keeps_its_indentation_and_a_list_item_is_not_a_removal():
    diff = ("--- a/d.md\n+++ b/d.md\n@@ -3,3 +3,3 @@\n"
            "     - item stays context\n-old\n+new\n")
    body = _plain(compose("edit_file", "d.md", diff, store=BoundedBodyStore(),
                          op_id="op").body_lines_markup)
    item = next(t for t in body if "item stays context" in t)
    assert "    - item stays context" in item          # indentation kept
    assert item.split()[0] == "3"                       # numbered as context (new side)
    assert next(t for t in body if "old" in t).split()[0] == "4"   # the removal follows it


def test_hunk_headers_are_gaps_not_text():
    diff = ("--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n"
            "@@ -40,2 +40,2 @@\n d\n+e\n")
    body = _plain(compose("edit_file", "x.py", diff, store=BoundedBodyStore(),
                          op_id="op").body_lines_markup)
    assert not any("@@" in t for t in body)
    assert any(t.strip() == "⋮" for t in body)


def test_a_multi_file_patch_splits_and_a_sql_comment_stays_in_its_hunk():
    patch = ("diff --git a/a.sql b/a.sql\n--- a/a.sql\n+++ b/a.sql\n@@ -1,2 +1,1 @@\n"
             "--- old comment\n keep\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+y\n")
    assert [p for p, _ in _split_unified_diff(patch)] == ["a.sql", "b.py"]
    assert _split_unified_diff("def f():\n    return 1\n") == []


def test_a_clipped_diff_ends_in_an_elision_not_a_numbered_line():
    from backend.core.ouroboros.ui.text_bounds import clip_lines
    text, _ = clip_lines(_diff("t.py", 2000), 5000)
    assert is_elision_line(text.splitlines()[-1])


# ------------------------------------------------------------------ summaries


@pytest.mark.parametrize("tool,result,expected", [
    ("read_file", "# f.py  (lines 1-165 of 165)\n1: x", "Read 165 lines"),
    ("read_file", "# f.py  (lines 1-40 of 165)\n1: x", "Read 40 of 165 lines"),
    ("search_code", "a.py:1: x\na.py:9: y\nb.py:3: z", "Found 3 matches in 2 files"),
    ("search_code", "(no matches for pattern='q' glob=*.py). Try a broader file_glob",
     "no matches for pattern='q' glob=*.py). Try a broader file_glob"),
    ("glob_files", "(no matches)", "no matches"),
    ("glob_files", "a.py\nb.py", "Found 2 files"),
    ("edit_file", "+a\n+b", "Added 2 lines"),
    ("edit_file", "-a", "Removed 1 line"),
    ("edit_file", "+a\n-b\n-c", "1 addition and 2 removals"),
])
def test_summaries_read_like_claude_code_and_never_count_tool_notes(tool, result, expected):
    out = get_descriptor(tool).summarize_result(result, ToolStatus.SUCCESS)
    assert out.startswith(expected[:20])


# ------------------------------------------------------------------ search


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text(".worktrees/\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "mod.py").write_text("needle = 1\n")
    copy = repo / ".worktrees" / "unit-1" / "pkg"
    copy.mkdir(parents=True)
    (copy / "mod.py").write_text("needle = 1\n")
    return repo


def test_search_honours_gitignore_without_rg(tmp_path, monkeypatch):
    import shutil
    from backend.core.ouroboros.governance.tool_executor import ToolExecutor
    repo = _git_repo(tmp_path)
    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n: None if n == "rg" else real_which(n))
    ex = ToolExecutor.__new__(ToolExecutor)
    ex._repo_root = repo
    out = ex._search_code({"pattern": "needle", "file_glob": "*.py"})
    assert out.splitlines() == ["pkg/mod.py:1:needle = 1"]
    bad = ex._search_code({"pattern": "foo(", "file_glob": "*.py"})
    assert bad.startswith("(search error")


def test_the_walker_prunes_what_git_ignores(tmp_path):
    from backend.core.ouroboros.governance import bounded_walker as bw
    repo = _git_repo(tmp_path)
    found = bw.bounded_glob(repo, "mod.py").matches
    assert [Path(p).relative_to(repo).as_posix() for p in found] == ["pkg/mod.py"]
    assert not any(".worktrees" in p for p in bw.iter_bounded_files(
        repo, max_scanned=1000, timeout_s=5))


def test_the_walker_is_unchanged_outside_git(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import bounded_walker as bw
    (tmp_path / ".worktrees").mkdir()
    (tmp_path / ".worktrees" / "mod.py").write_text("x")
    assert len(bw.bounded_glob(tmp_path, "mod.py").matches) == 1


# ------------------------------------------------------------------ advice


@pytest.mark.parametrize("reason,expected", [
    ("background_accepted:background_dw_blocked_by_topology:Catalog", "No lane could serve"),
    ("file lock held", "File lock held"),
])
def test_advice_matches_whole_tokens(reason, expected):
    assert _actionable_suggestion(reason, "", 1.0).startswith(expected)
