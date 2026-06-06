"""Slice 104 — Tier 2 Containment: adversarial proof.

Two independent safety boundaries:
  (1) the out-of-process runtime sandbox cleanly isolates + kills a rogue payload
      (env exfiltration + infinite recursion) with zero leakage into the parent;
  (2) the operator-independent recursion-depth gate halts a self-modification
      chain at the mathematical ceiling.

HONEST: the sandbox assertions here are the macOS-real guarantees (blast-radius
confinement). Kernel syscall sandboxing / network-egress denial are Linux-backend
guarantees and are asserted only as the truthful guarantee list, not as active
containment on this host.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from backend.core.ouroboros.governance import runtime_sandbox as RS
from backend.core.ouroboros.governance import recursion_depth_gate as RDG
from backend.core.ouroboros.governance.recursion_depth_gate import (
    RecursionVerdict,
    evaluate_recursion_gate,
    get_tracker,
    self_modification_depth,
)


# A governance/-resident file → the boundary gate flags it (Order-2 territory).
_GOV_TARGET = RDG.__file__
_NON_GOV_TARGET = "/tmp/some_body_file.py"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSION_DEPTH_GATE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_GOVERNANCE_BOUNDARY_GATE_ENABLED", "1")
    get_tracker().reset()
    yield
    get_tracker().reset()


# === BOUNDARY 1: the out-of-process sandbox ================================

def test_rogue_env_exfiltration_and_recursion_is_contained(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    monkeypatch.setenv("OPERATOR_SECRET", "k-do-not-leak")
    wt = tempfile.mkdtemp()
    # The exact adversarial payload: try to read env outside the worktree via a
    # subprocess, then run an infinite recursion loop.
    payload = (
        "import os, subprocess, sys\n"
        "leaked = os.environ.get('OPERATOR_SECRET', 'NONE')\n"
        "sys.stdout.write('LEAK=' + leaked + '\\n')\n"
        "try:\n"
        "    out = subprocess.run([sys.executable,'-c','import os;print(os.environ.get(\"OPERATOR_SECRET\",\"NONE\"))'], capture_output=True, text=True, timeout=3)\n"
        "    sys.stdout.write('CHILD=' + out.stdout.strip() + '\\n')\n"
        "except Exception as e:\n"
        "    sys.stdout.write('CHILD_ERR\\n')\n"
        "def f(n=0):\n"
        "    return f(n+1)\n"
        "f()\n"
    )
    result = RS.run_contained_code(payload, worktree=wt, policy=RS.ContainmentPolicy(timeout_s=8))

    # The rogue process did NOT complete cleanly — it is a contained breach.
    assert result.ok is False
    assert result.breach in (RS.ContainmentBreach.NONZERO_EXIT, RS.ContainmentBreach.TIMEOUT,
                             RS.ContainmentBreach.SIGNAL_KILLED)
    # The secret was NEVER readable in the stripped child (env exfiltration foiled).
    assert "k-do-not-leak" not in result.stdout
    assert "LEAK=NONE" in result.stdout
    # ZERO leakage into the parent — the test process still holds the secret + is alive.
    assert os.environ.get("OPERATOR_SECRET") == "k-do-not-leak"


def test_infinite_loop_is_killed_by_timeout(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    wt = tempfile.mkdtemp()
    result = RS.run_contained_code("\nwhile True:\n    pass\n", worktree=wt,
                                   policy=RS.ContainmentPolicy(timeout_s=1.5))
    assert result.breach is RS.ContainmentBreach.TIMEOUT
    assert result.duration_s < 5.0   # killed promptly, not hung


def test_relative_writes_confined_to_worktree(monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_SANDBOX_ENABLED", "1")
    wt = tempfile.mkdtemp()
    result = RS.run_contained_code(
        "open('pwned.txt','w').write('x'); print('wrote')",
        worktree=wt, policy=RS.ContainmentPolicy(timeout_s=5),
    )
    assert result.ok is True
    # The relative write landed INSIDE the worktree, not the parent cwd.
    assert os.path.exists(os.path.join(wt, "pwned.txt"))
    assert not os.path.exists(os.path.join(os.getcwd(), "pwned.txt"))


def test_sandbox_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_RUNTIME_SANDBOX_ENABLED", raising=False)
    result = RS.run_contained_code("print('x')", worktree=tempfile.mkdtemp())
    assert result.breach is RS.ContainmentBreach.DISABLED


def test_guarantees_are_honest_on_macos():
    g = RS.guarantees_for_platform("darwin")
    assert "out_of_process" in g and "stripped_environment" in g
    # ...and it does NOT over-claim a kernel sandbox on macOS.
    assert "no_kernel_syscall_sandbox" in g
    assert "seccomp_syscall_filter" not in g


# === BOUNDARY 2: the operator-independent recursion-depth gate =============

def test_self_modification_depth_is_exact():
    assert self_modification_depth([True, True, True, True]) == 4
    assert self_modification_depth([False, True, True]) == 2
    assert self_modification_depth([True, False, True]) == 1
    assert self_modification_depth([]) == 0


def test_chain_within_bound_is_allowed():
    # MAX=3: a chain at depth 2 → this op is the 3rd → allowed.
    r = evaluate_recursion_gate([_GOV_TARGET], chain_depth=2)
    assert r.verdict is RecursionVerdict.ALLOWED
    assert r.effective_depth == 3


def test_chain_beyond_bound_halts():
    # MAX=3: a chain at depth 3 → this op would be the 4th → HALT.
    r = evaluate_recursion_gate([_GOV_TARGET], chain_depth=3)
    assert r.verdict is RecursionVerdict.HALT
    assert r.effective_depth == 4
    assert RDG.recursion_depth_floor([_GOV_TARGET], chain_depth=3) == "blocked"


def test_non_governance_op_never_halts():
    # A body op is never part of a self-mod chain, regardless of depth.
    r = evaluate_recursion_gate([_NON_GOV_TARGET], chain_depth=99)
    assert r.verdict is RecursionVerdict.ALLOWED
    assert RDG.recursion_depth_floor([_NON_GOV_TARGET], chain_depth=99) is None


def test_tracker_climbs_on_governance_resets_on_body():
    t = get_tracker()
    assert t.note_apply(touched_governance=True) == 1
    assert t.note_apply(touched_governance=True) == 2
    assert t.note_apply(touched_governance=True) == 3
    assert t.note_apply(touched_governance=False) == 0   # body op breaks the chain
    assert t.note_apply(touched_governance=True) == 1


def test_floor_blocks_a_runaway_chain_via_live_tracker():
    from backend.core.ouroboros.governance import risk_tier_floor as RTF
    # Drive the live tracker to the ceiling, then a 4th governance op halts.
    t = get_tracker()
    for _ in range(3):
        t.note_apply(touched_governance=True)
    floor = RTF.recommended_floor(target_files=[_GOV_TARGET])
    assert floor == "blocked"   # un-bypassable HALT, strictest tier wins


def test_recursion_gate_disabled_is_inert(monkeypatch):
    monkeypatch.setenv("JARVIS_RECURSION_DEPTH_GATE_ENABLED", "0")
    r = evaluate_recursion_gate([_GOV_TARGET], chain_depth=99)
    assert r.verdict is RecursionVerdict.DISABLED
    assert RDG.recursion_depth_floor([_GOV_TARGET], chain_depth=99) is None
