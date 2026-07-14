"""Task #4a — the dry_run probe strategy, on the hardened sandbox.

Before this, HypothesisProbe's only real strategy was ``lookup`` (read files,
match string/AST predicates). ``dry_run`` (and ``subagent_explore``) were
placeholders returning INCONCLUSIVE — so even with the probe enabled, O+V could
not run a hypothesis as an EXPERIMENT. This closes the dry_run half: it executes
a bounded, air-gapped, read-only probe in ``container_sandbox`` (via
``sandbox_exec``) and maps the outcome to a verdict.

These tests fake the sandbox BOUNDARY (sandbox_run_bash/sandbox_run_tests return
a controlled SandboxResult) — not the strategy — so the verdict-mapping logic is
what's under test. The container itself is exercised by its own suite; the live
fail-closed path (no container → INCONCLUSIVE) is proven separately and asserted
here too.
"""
from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance.sandbox_exec import SandboxResult
from backend.core.ouroboros.governance.verification import hypothesis_probe as hp
from backend.core.ouroboros.governance.verification.hypothesis_probe import (
    Hypothesis,
    _strategy_dry_run,
)


def _h(signal: str) -> Hypothesis:
    return Hypothesis(
        claim="the change behaves as expected",
        confidence_prior=0.5,
        test_strategy="dry_run",
        expected_signal=signal,
    )


def _run(coro):
    return asyncio.run(coro)


def _ok(stdout="", returncode=0):
    return SandboxResult(ok=(returncode == 0), stdout=stdout, stderr="",
                         returncode=returncode, denied=False, reason="")


def _denied():
    return SandboxResult(ok=False, stdout="", stderr="", returncode=None,
                         denied=True, reason="sandbox_unavailable:DISABLED")


# ── tests: verdict mapping ────────────────────────────────────────────

def test_tests_green_confirms(monkeypatch):
    async def fake(targets, *, worktree, docker_run=None):
        assert targets == ["tests/foo_test.py"]
        return _ok(returncode=0)
    monkeypatch.setattr(hp, "sandbox_run_tests", fake, raising=False)
    # patch the lazily-imported symbol in the module namespace it resolves to
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_tests",
        fake,
    )
    reason, cost, verdict = _run(_strategy_dry_run(_h("dry_run:tests:tests/foo_test.py")))
    assert verdict == "CONFIRMED"
    assert cost == 0.0


def test_tests_red_refutes(monkeypatch):
    async def fake(targets, *, worktree, docker_run=None):
        return _ok(returncode=1)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_tests", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:tests:a.py,b.py")))
    assert verdict == "REFUTED"


def test_bash_exit0_confirms(monkeypatch):
    async def fake(cmd, *, worktree, docker_run=None):
        return _ok(stdout="anything", returncode=0)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:bash:grep -q foo bar")))
    assert verdict == "CONFIRMED"


def test_bash_nonzero_refutes(monkeypatch):
    async def fake(cmd, *, worktree, docker_run=None):
        return _ok(returncode=2)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:bash:false")))
    assert verdict == "REFUTED"


def test_bash_substring_present_confirms(monkeypatch):
    async def fake(cmd, *, worktree, docker_run=None):
        return _ok(stdout="the answer is 42\n", returncode=0)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:bash:echo x::answer is 42")))
    assert verdict == "CONFIRMED"


def test_bash_substring_absent_refutes(monkeypatch):
    async def fake(cmd, *, worktree, docker_run=None):
        return _ok(stdout="nothing here", returncode=0)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:bash:echo x::MISSING")))
    assert verdict == "REFUTED"


def test_bash_substring_present_but_nonzero_refutes(monkeypatch):
    """Substring present but the command FAILED must not CONFIRM — both
    conditions are required."""
    async def fake(cmd, *, worktree, docker_run=None):
        return _ok(stdout="found it", returncode=1)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake,
    )
    _, _, verdict = _run(_strategy_dry_run(_h("dry_run:bash:x::found it")))
    assert verdict == "REFUTED"


# ── tests: fail-CLOSED (never CONFIRM without a real run) ─────────────

def test_sandbox_denied_is_inconclusive_not_confirmed(monkeypatch):
    """THE safety property: an unavailable sandbox yields INCONCLUSIVE, NEVER a
    fabricated CONFIRMED. Silence is not confirmation."""
    async def fake_bash(cmd, *, worktree, docker_run=None):
        return _denied()
    async def fake_tests(targets, *, worktree, docker_run=None):
        return _denied()
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", fake_bash,
    )
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_tests", fake_tests,
    )
    assert _run(_strategy_dry_run(_h("dry_run:bash:true")))[2] == "INCONCLUSIVE"
    assert _run(_strategy_dry_run(_h("dry_run:tests:a.py")))[2] == "INCONCLUSIVE"


def test_strategy_raise_is_inconclusive(monkeypatch):
    async def boom(cmd, *, worktree, docker_run=None):
        raise RuntimeError("container exploded")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.sandbox_exec.sandbox_run_bash", boom,
    )
    # Must not raise — fail-soft to INCONCLUSIVE.
    assert _run(_strategy_dry_run(_h("dry_run:bash:true")))[2] == "INCONCLUSIVE"


# ── tests: malformed signals ──────────────────────────────────────────

def test_malformed_signals_are_inconclusive():
    for sig in (
        "not_a_dry_run",
        "dry_run:garbage:x",
        "dry_run:tests:",
        "dry_run:bash:",
        "dry_run:",
        "",
    ):
        assert _run(_strategy_dry_run(_h(sig)))[2] == "INCONCLUSIVE", sig


# ── tests: the strategy is actually registered (no inert placeholder) ─

def test_dry_run_is_registered_and_not_a_placeholder():
    assert not hasattr(hp, "_strategy_dry_run_placeholder"), (
        "the dry_run placeholder must be gone — replaced by the real strategy"
    )
    # The registered strategy must be the real one.
    import inspect
    src = inspect.getsource(hp._register_seed_strategies)
    assert "execute=_strategy_dry_run," in src
    assert "_strategy_dry_run_placeholder" not in src
