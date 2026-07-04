from __future__ import annotations

from types import SimpleNamespace

from backend.core.ouroboros.governance.dw_transport_hedge import (
    resolve_hedge_arm_policy,
)


def _ctx(**over):
    base = dict(
        op_id="op-test", task_complexity="", provider_route="background",
        is_read_only=False, target_files=("backend/foo.py",), repo_root=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_a1_shape_op_resolves_to_rt_priority_with_defer():
    """The exact A1 failure shape: BACKGROUND write-intent op, complexity
    unset. Pre-fix: prefer_fast=False -> batch preempts -> 0 TOOL OUTPUT.
    Post-fix: RT priority + deferred batch."""
    c = _ctx()
    p = resolve_hedge_arm_policy(
        complexity=str(getattr(c, "task_complexity", "") or ""),
        route=str(getattr(c, "provider_route", "") or ""),
        is_read_only=bool(getattr(c, "is_read_only", False)),
        target_files=tuple(getattr(c, "target_files", ()) or ()),
        repo_root=getattr(c, "repo_root", None),
    )
    assert p.prefer_fast is True
    assert p.defer_stable is True


def test_read_only_bg_op_keeps_legacy_batch_speed():
    c = _ctx(is_read_only=True, target_files=())
    p = resolve_hedge_arm_policy(
        complexity="", route="background", is_read_only=True,
        target_files=(), repo_root=None,
    )
    assert p.prefer_fast is False


def test_provider_call_site_passes_defer_stable():
    import inspect
    from backend.core.ouroboros.governance import doubleword_provider as dw
    src = inspect.getsource(dw)
    assert "resolve_hedge_arm_policy" in src, "resolver not wired into the provider"
    assert "defer_stable=_arm_policy.defer_stable" in src, "defer not threaded to hedged_race"
    assert src.count("is_read_only=bool(getattr(context") >= 2, (
        "is_read_only not threaded into compute_tool_loop_suppressed call sites"
    )
    # Stratification-leg liveness guard: OperationContext carries no repo_root
    # attribute, so the resolver call MUST fall back to the provider-instance
    # root (self._repo_root) or the Tier-4 stratification branch is dead code
    # on 100% of live dispatches. Scope the check to the resolver call region
    # so an unrelated self._repo_root use elsewhere cannot mask a regression.
    call_idx = src.index("_policy_resolve(")
    call_region = src[call_idx:call_idx + 1200]
    assert "repo_root=" in call_region, "resolver call lost its repo_root argument"
    assert "self._repo_root" in call_region, (
        "stratification leg dormant: resolver call does not consult "
        "self._repo_root (context has no repo_root attribute)"
    )
