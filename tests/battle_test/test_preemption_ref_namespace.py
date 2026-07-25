"""Hazard #70033 — the daemon must never mutate the shared stash stack.

Observed 2026-07-24, live: an agent ran `git stash push` then `git stash pop`
while the preemption-shield daemon was running. Between those two commands the
daemon called `git stash store`, which pushes onto `refs/stash` — a SHARED,
ORDER-SENSITIVE stack. `git stash pop` takes `stash@{0}`, so the agent received
the DAEMON's snapshot instead of their own and got a merge conflict in
`awe_trigger.py`.

The working tree was never the issue: `stash create` + `stash store` are both
non-destructive by construction (that was the 2026-07-18 fix). The shared
NAMESPACE was the issue. These tests run against a REAL git repo — a mocked
subprocess would happily "prove" a stack that was never exercised.
"""

from __future__ import annotations

import subprocess

import pytest

from backend.core.ouroboros.battle_test import graceful_preemption as gp


def _git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def repo(tmp_path):
    """A real, minimal git repo with one commit."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "tracked.py").write_text("x = 1\n")
    _git(tmp_path, "add", "tracked.py")
    _git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_snapshot_does_not_touch_the_shared_stash_stack(repo):
    """THE REGRESSION: after a daemon snapshot, `git stash list` must be empty."""
    (repo / "tracked.py").write_text("x = 2\n")

    result = gp.git_safety_stash(str(repo))
    assert result.startswith("snapshot:"), result

    listed = _git(repo, "stash", "list").stdout.strip()
    assert listed == "", f"daemon polluted the shared stash stack: {listed!r}"


def test_agent_stash_pop_returns_the_agents_own_work(repo):
    """The exact live sequence: agent pushes, daemon snapshots, agent pops.
    The agent must get THEIR content back, not the daemon's."""
    (repo / "tracked.py").write_text("AGENT WORK\n")
    _git(repo, "stash", "push", "-q", "-m", "agent-work")

    # Daemon fires mid-flight, twice, on a different tree state.
    (repo / "tracked.py").write_text("DAEMON SAW THIS\n")
    assert gp.git_safety_stash(str(repo)).startswith("snapshot:")
    (repo / "tracked.py").write_text("DAEMON SAW THIS TOO\n")
    assert gp.git_safety_stash(str(repo)).startswith("snapshot:")

    # Agent restores. Reset the tree first so the pop applies cleanly.
    _git(repo, "checkout", "--", "tracked.py")
    pop = _git(repo, "stash", "pop")
    assert pop.returncode == 0, f"pop failed: {pop.stderr}"

    assert (repo / "tracked.py").read_text() == "AGENT WORK\n", (
        "the agent got someone else's stash — hazard #70033 is not fixed"
    )


def test_snapshot_leaves_the_working_tree_untouched(repo):
    """Non-destructiveness is preserved (the 2026-07-18 property)."""
    (repo / "tracked.py").write_text("dirty\n")
    (repo / "untracked.py").write_text("new\n")

    gp.git_safety_stash(str(repo))

    assert (repo / "tracked.py").read_text() == "dirty\n"
    assert (repo / "untracked.py").read_text() == "new\n"


def test_snapshot_is_recoverable_from_the_private_namespace(repo):
    """Recovery must survive the move off the stack — apply by RAW SHA."""
    from backend.core.ouroboros.governance.workspace_checkpoint import apply_stash_ref

    (repo / "tracked.py").write_text("RECOVER ME\n")
    out = gp.git_safety_stash(str(repo))
    assert out.startswith("snapshot:")

    refs = gp.list_preemption_refs(str(repo))
    assert refs, "snapshot left no listable ref"
    _refname, sha = refs[0]

    # Throw the work away, then restore it from the snapshot.
    _git(repo, "checkout", "--", "tracked.py")
    assert (repo / "tracked.py").read_text() == "x = 1\n"

    assert apply_stash_ref(str(repo), sha) is True
    assert (repo / "tracked.py").read_text() == "RECOVER ME\n"


def test_clean_tree_snapshots_nothing(repo):
    assert gp.git_safety_stash(str(repo)) == "tree_clean"
    assert gp.list_preemption_refs(str(repo)) == []


def test_namespace_retention_is_bounded(repo, monkeypatch):
    """A long-lived daemon must not grow the ref namespace without bound."""
    monkeypatch.setenv("JARVIS_PREEMPTION_SNAPSHOT_RETAIN", "3")

    for i in range(6):
        (repo / "tracked.py").write_text(f"rev {i}\n")
        gp.git_safety_stash(str(repo))

    refs = gp.list_preemption_refs(str(repo))
    assert len(refs) <= 3, f"retention not enforced: {len(refs)} refs"
    assert _git(repo, "stash", "list").stdout.strip() == ""


def test_concurrent_daemon_snapshots_never_collide(repo):
    """Many snapshots in the same second must not clobber one another — the ref
    name carries the content SHA, not just a timestamp."""
    shas = set()
    for i in range(4):
        (repo / "tracked.py").write_text(f"content {i}\n")
        out = gp.git_safety_stash(str(repo))
        assert out.startswith("snapshot:")
        shas.add(out.split(":", 1)[1])

    refs = gp.list_preemption_refs(str(repo))
    assert len(refs) == len(shas) == 4, (
        f"distinct snapshots collapsed: {len(refs)} refs for {len(shas)} shas"
    )
