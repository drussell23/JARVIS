"""Zero-push air-gap: policy, and the structural invariant that keeps it.

On 2026-09-07, 22 ``ouroboros/review/*`` branches reached ``origin`` from soaks
that were supposed to be isolated. The policy was real but it lived inside ONE
lane, while three other call sites pushed with none. A lane-local flag is not
an air-gap.

The scanner below is the part that matters long-term: it walks the AST of
``backend/`` and fails if any function builds a remote-push argv without
consulting :mod:`remote_push_guard`. Adding a fifth push site without a guard
breaks this test, which is the only thing that stops the same drift happening
again.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.remote_push_guard import (
    ENV_AIRGAP,
    ENV_AUTO_PUSH_BRANCH,
    ENV_LANE_PUSH,
    airgap_engaged,
    push_verdict,
    remote_push_allowed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

#: Names that count as consulting the policy.
GUARD_NAMES = frozenset({
    "remote_push_allowed", "push_verdict", "airgap_engaged", "remote_push_guard",
})

#: Files exempt from the scan, each for a stated reason.
EXEMPT = {
    # Defines the policy; cannot consult itself.
    "core/ouroboros/governance/remote_push_guard.py",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test states the environment it means. The air-gap is read live,
    so a leaked var from another test would silently change the verdict."""
    for var in (ENV_AIRGAP, ENV_LANE_PUSH, ENV_AUTO_PUSH_BRANCH):
        monkeypatch.delenv(var, raising=False)


# --------------------------------------------------------------------------
# The air-gap is closed by DEFAULT — the safe state is not opt-in
# --------------------------------------------------------------------------

def test_the_airgap_is_engaged_when_nothing_is_set():
    assert airgap_engaged() is True
    assert remote_push_allowed("any") is False


@pytest.mark.parametrize("value", ["", "  ", "true", "1", "yes", "on", "banana", "TRUE"])
def test_only_an_explicit_falsey_value_lowers_the_airgap(monkeypatch, value):
    """An unset, blank or unparseable value keeps the gap CLOSED. The failure
    being prevented is exactly 'nobody set the flag'."""
    monkeypatch.setenv(ENV_AIRGAP, value)
    assert airgap_engaged() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "OFF", " False "])
def test_an_explicit_falsey_value_lowers_it(monkeypatch, value):
    monkeypatch.setenv(ENV_AIRGAP, value)
    assert airgap_engaged() is False


# --------------------------------------------------------------------------
# ...and nothing lifts it
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "var,value",
    [
        (ENV_LANE_PUSH, "1"),
        (ENV_LANE_PUSH, "true"),
        (ENV_AUTO_PUSH_BRANCH, "main"),
        (ENV_AUTO_PUSH_BRANCH, "ouroboros/review/whatever"),
    ],
)
def test_no_lane_flag_can_override_the_airgap(monkeypatch, var, value):
    """The whole point of an air-gap: a stray flag elsewhere cannot open it."""
    monkeypatch.setenv(var, value)
    assert remote_push_allowed("orange_pr") is False
    assert "airgap" in push_verdict("orange_pr").reason


def test_with_the_airgap_down_an_operator_declaration_is_honoured(monkeypatch):
    monkeypatch.setenv(ENV_AIRGAP, "false")
    monkeypatch.setenv(ENV_AUTO_PUSH_BRANCH, "main")
    assert remote_push_allowed("auto_committer") is True


def test_with_the_airgap_down_and_no_declaration_it_still_refuses(monkeypatch):
    monkeypatch.setenv(ENV_AIRGAP, "false")
    assert remote_push_allowed("orange_pr") is False


def test_an_explicit_lane_disable_beats_a_push_branch(monkeypatch):
    monkeypatch.setenv(ENV_AIRGAP, "false")
    monkeypatch.setenv(ENV_LANE_PUSH, "0")
    monkeypatch.setenv(ENV_AUTO_PUSH_BRANCH, "main")
    assert remote_push_allowed("orange_pr") is False


def test_the_verdict_always_carries_a_reason():
    assert push_verdict("lane").reason
    assert push_verdict("lane").lane == "lane"


def test_the_orange_lane_delegates_rather_than_keeping_a_copy(monkeypatch):
    """The lane's own name for the policy must resolve to THIS policy — a
    second copy is how the two drifted apart the first time."""
    from backend.core.ouroboros.governance import orange_pr_reviewer

    monkeypatch.setenv(ENV_AIRGAP, "false")
    monkeypatch.setenv(ENV_AUTO_PUSH_BRANCH, "main")
    assert orange_pr_reviewer.remote_push_allowed() is True
    monkeypatch.setenv(ENV_AIRGAP, "true")
    assert orange_pr_reviewer.remote_push_allowed() is False


# --------------------------------------------------------------------------
# The structural invariant — every remote-push argv is guarded
# --------------------------------------------------------------------------

def _is_remote_push_argv(strings) -> bool:
    """True for an argv that pushes to a REMOTE.

    ``git stash push`` is local (it is how the repo saves work aside) and is
    not a remote act, so it never counts.
    """
    if "push" not in strings:
        return False
    if "stash" in strings:
        return False
    return "git" in strings


def _call_arg_strings(node: ast.Call):
    out = []
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.append(arg.value)
    return out


def _seq_strings(node) -> list:
    return [
        e.value for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]


def _find_push_sites(tree: ast.AST):
    """Every (lineno, kind) in *tree* that builds a remote-push argv."""
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_remote_push_argv(_call_arg_strings(node)):
                sites.append(node.lineno)
                continue
            # A git-runner helper called as ("push", ...) — e.g. _run_git.
            fname = ""
            if isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            elif isinstance(node.func, ast.Name):
                fname = node.func.id
            if "git" in fname.lower():
                args = _call_arg_strings(node)
                if args and args[0] == "push":
                    sites.append(node.lineno)
        elif isinstance(node, (ast.List, ast.Tuple)):
            if _is_remote_push_argv(_seq_strings(node)):
                sites.append(node.lineno)
    return sorted(set(sites))


def _guard_consulted(tree: ast.AST) -> bool:
    """True when the module references the policy anywhere in its own source."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in GUARD_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in GUARD_NAMES:
            return True
        if isinstance(node, ast.alias) and (node.name in GUARD_NAMES
                                            or node.asname in GUARD_NAMES):
            return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.endswith("remote_push_guard"):
                return True
    return False


def _scan():
    """Return {relative path: [line numbers]} for unguarded push sites."""
    offenders = {}
    for path in BACKEND.rglob("*.py"):
        rel = path.relative_to(BACKEND).as_posix()
        if rel in EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        sites = _find_push_sites(tree)
        if sites and not _guard_consulted(tree):
            offenders[rel] = sites
    return offenders


def test_the_scanner_actually_finds_push_sites():
    """A scanner that matches nothing would pass the invariant vacuously —
    this repo's most expensive failure shape. Prove it has teeth."""
    found = 0
    for path in BACKEND.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        found += len(_find_push_sites(tree))
    assert found >= 4, f"scanner found only {found} push sites; it has gone blind"


def test_every_remote_push_site_consults_the_policy():
    offenders = _scan()
    assert not offenders, (
        "these modules build a remote-push argv without consulting "
        "remote_push_guard — a push that no policy governs is exactly how 22 "
        "review branches reached origin:\n  "
        + "\n  ".join(f"{k}: lines {v}" for k, v in sorted(offenders.items()))
    )


def test_git_stash_push_is_not_treated_as_a_remote_act():
    assert _is_remote_push_argv(["git", "stash", "push", "-u"]) is False
    assert _is_remote_push_argv(["git", "push", "-u", "origin", "b"]) is True
