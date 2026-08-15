"""One owner for the O+V lifecycle, and both claimants agree who it is.

The bug: `ov` and `unified_supervisor` both booted GovernedLoopService. On
2026-08-15 the supervisor's copy hydrated the operator's dial, exceeded a
fixed 30s budget by 1.8s, and was abandoned by `wait_for` while
`asyncio.shield` kept it running — leaving an orphaned governed loop inside
a process holding `_governed_loop = None`, reporting `GLS failed:` with an
empty reason because `TimeoutError` stringifies to "".

Neither owner was wrong; having two was.
"""
from __future__ import annotations

import ast
from pathlib import Path

from backend.core.ouroboros.governance import lifecycle_ownership as lo

REPO = Path(__file__).resolve().parents[2]


class TestTheDeclaredOwner:
    def test_ov_owns_it_by_default(self, monkeypatch):
        """The process an operator starts when they want O+V is the one
        whose whole reason to exist is this loop."""
        monkeypatch.delenv(lo.ENV_VAR, raising=False)
        assert lo.governance_owner() == lo.OWNER_OV
        assert lo.ov_owns_governance() is True
        assert lo.supervisor_owns_governance() is False

    def test_the_supervisor_can_be_given_it_back(self, monkeypatch):
        """An inversion an operator can perform without editing code — the
        fallback still exists, it just no longer runs unasked."""
        monkeypatch.setenv(lo.ENV_VAR, lo.OWNER_SUPERVISOR)
        assert lo.supervisor_owns_governance() is True
        assert lo.ov_owns_governance() is False

    def test_exactly_one_owner_can_hold_it(self, monkeypatch):
        """The property the whole module exists for. Not two, ever."""
        for owner in lo.VALID_OWNERS:
            monkeypatch.setenv(lo.ENV_VAR, owner)
            holders = [c for c in lo.VALID_OWNERS if lo.owns_governance(c)]
            assert holders == [owner], holders

    def test_an_unknown_owner_resolves_to_the_default(self, monkeypatch):
        """Read on a startup path: a typo in one env var must not be able to
        take the organism down."""
        monkeypatch.setenv(lo.ENV_VAR, "hud")
        assert lo.governance_owner() == lo.DEFAULT_OWNER

    def test_whitespace_and_case_are_not_a_different_owner(self, monkeypatch):
        monkeypatch.setenv(lo.ENV_VAR, "  SUPERVISOR  ")
        assert lo.supervisor_owns_governance() is True

    def test_an_empty_value_is_not_an_owner(self, monkeypatch):
        monkeypatch.setenv(lo.ENV_VAR, "   ")
        assert lo.governance_owner() == lo.DEFAULT_OWNER

    def test_it_never_raises_when_the_environment_is_unreadable(
            self, monkeypatch):
        class _Hostile(dict):
            def get(self, *_a, **_k):
                raise RuntimeError("environ exploded")

        monkeypatch.setattr(lo.os, "environ", _Hostile())
        assert lo.governance_owner() == lo.DEFAULT_OWNER

    def test_the_bypass_note_names_the_owner_and_the_way_back(
            self, monkeypatch):
        """A supervisor log that never mentions governance again must say
        where it went, not leave the operator to conclude it broke."""
        monkeypatch.delenv(lo.ENV_VAR, raising=False)
        note = lo.bypass_note(lo.OWNER_SUPERVISOR)
        assert lo.OWNER_OV in note
        assert lo.ENV_VAR in note and lo.OWNER_SUPERVISOR in note


class TestBothClaimantsRouteThroughOnePredicate:
    """A wiring pin, and honest about being one: it proves the two
    construction sites CONSULT the predicate, while the tests above prove
    what the predicate answers. Gating only one site would leave the race
    intact on whichever boot path won that particular boot — and the
    supervisor cannot be imported to check this behaviourally, because
    importing it re-execs the interpreter (`_ensure_venv_python` fires at
    import, not just under `__main__`).
    """

    @staticmethod
    def _kernel_class() -> ast.ClassDef:
        tree = ast.parse((REPO / "unified_supervisor.py").read_text(
            encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "JarvisSystemKernel":
                return node
        raise AssertionError("JarvisSystemKernel not found")

    def test_every_governed_loop_construction_is_gated(self):
        kernel = self._kernel_class()
        # Each `GovernedLoopService(...)` call, and the guard that must
        # dominate it somewhere in its enclosing function.
        constructing = []
        for fn in ast.walk(kernel):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds = any(
                isinstance(n, ast.Call)
                and getattr(n.func, "id", "") == "GovernedLoopService"
                for n in ast.walk(fn))
            if not builds:
                continue
            gated = any(
                isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "_owns_governance_loop"
                for n in ast.walk(fn))
            constructing.append((fn.name, gated))

        assert constructing, "no GovernedLoopService construction found"
        ungated = [name for name, gated in constructing if not gated]
        assert not ungated, (
            f"these construct the governed loop without consulting "
            f"_owns_governance_loop: {ungated} — one ungated site restores "
            f"the dual ownership this closes")

    def test_the_predicate_delegates_rather_than_reimplementing(self):
        """A second spelling of the rule is a second rule. The supervisor
        must ASK `lifecycle_ownership`, not parse the env itself."""
        kernel = self._kernel_class()
        pred = next(
            f for f in kernel.body
            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
            and f.name == "_owns_governance_loop")
        imported = {
            alias.name
            for n in ast.walk(pred) if isinstance(n, ast.ImportFrom)
            for alias in n.names
        }
        assert "supervisor_owns_governance" in imported
        assert not any(
            isinstance(n, ast.Attribute) and n.attr == "environ"
            for n in ast.walk(pred)), "reads the environment directly"
