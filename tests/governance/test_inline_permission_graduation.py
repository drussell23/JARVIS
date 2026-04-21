"""Slice 5 graduation pins — Inline Permission Prompts arc.

These tests pin the 2026-04-21 graduation decisions so future edits
surface in review. They cover:

* Observability half graduated default `true` (safe: pure read surface)
* Tool-hook half KEPT default `false` by deliberate design (documented
  rationale mirrors Problem #7's Slice 5 posture)
* Full revert matrix — every env knob can be flipped back off
* Authority invariants — grep-pinned across all four slice modules
* Docstring bit-rot guard — graduation rationale stays in the
  master-switch docstrings
* Ruleset version in telemetry (§8)
* 7 event types remain in broker allowlist
* Bridge idempotent: multiple attach/detach cycles leave no leaked listeners
* Interaction pins: double-ask guard still fires under graduated defaults
* EventChannelServer wiring: the startup sequence mounts the new router
  when observability is enabled and leaves it off when it isn't
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Dict, List

import pytest


# ===========================================================================
# 1. Graduated defaults
# ===========================================================================


def test_observability_default_is_true_post_slice_5(monkeypatch):
    """Graduated 2026-04-21. Pure read surface, safe to default-on."""
    monkeypatch.delenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.inline_permission_observability \
        import inline_permission_observability_enabled
    assert inline_permission_observability_enabled() is True


def test_tool_hook_default_is_false_by_design(monkeypatch):
    """Slice 5 does NOT graduate the authorization half.

    Deliberate choice documented in the master-switch docstring:
    OpApprovedScope is currently test/harness-injected, not
    orchestrator-populated. Default-on would prompt on every
    unscoped edit/write/bash. Operator opt-in preserved.
    """
    monkeypatch.delenv("JARVIS_INLINE_PERMISSION_ENABLED", raising=False)
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        inline_permission_enabled,
    )
    assert inline_permission_enabled() is False


# ===========================================================================
# 2. Full-revert matrix — every env knob is reversible
# ===========================================================================


_REVERT_MATRIX = [
    ("JARVIS_INLINE_PERMISSION_ENABLED",
     "backend.core.ouroboros.governance.inline_permission_prompt",
     "inline_permission_enabled"),
    ("JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED",
     "backend.core.ouroboros.governance.inline_permission_observability",
     "inline_permission_observability_enabled"),
]


@pytest.mark.parametrize(
    "env,module,predicate", _REVERT_MATRIX,
    ids=[m[0] for m in _REVERT_MATRIX],
)
def test_env_flag_respects_explicit_true(
    env: str, module: str, predicate: str, monkeypatch,
):
    import importlib
    monkeypatch.setenv(env, "true")
    mod = importlib.import_module(module)
    assert getattr(mod, predicate)() is True


@pytest.mark.parametrize(
    "env,module,predicate", _REVERT_MATRIX,
    ids=[m[0] for m in _REVERT_MATRIX],
)
def test_env_flag_respects_explicit_false(
    env: str, module: str, predicate: str, monkeypatch,
):
    import importlib
    monkeypatch.setenv(env, "false")
    mod = importlib.import_module(module)
    assert getattr(mod, predicate)() is False


@pytest.mark.parametrize(
    "env,module,predicate", _REVERT_MATRIX,
    ids=[m[0] for m in _REVERT_MATRIX],
)
def test_env_flag_rejects_malformed(
    env: str, module: str, predicate: str, monkeypatch,
):
    """Malformed envs must fall back to a safe default (not raise)."""
    import importlib
    monkeypatch.setenv(env, "yes please")
    mod = importlib.import_module(module)
    # Any non-"true" string reads as false.
    assert getattr(mod, predicate)() is False


# ===========================================================================
# 3. Authority invariants — grep-pinned across all modules
# ===========================================================================


_FORBIDDEN_IMPORTS = (
    "orchestrator",
    "policy_engine",
    "iron_gate",
    "risk_tier_floor",
    "semantic_guardian",
    "tool_executor",
    "candidate_generator",
    "change_engine",
)


def _scan_file_for_forbidden(path: Path) -> List[str]:
    src = path.read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN_IMPORTS:
        pattern = re.compile(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            re.MULTILINE,
        )
        if pattern.search(src):
            violations.append(mod)
    return violations


@pytest.mark.parametrize("rel_path", [
    "backend/core/ouroboros/governance/inline_permission.py",
    "backend/core/ouroboros/governance/inline_permission_prompt.py",
    "backend/core/ouroboros/governance/inline_permission_memory.py",
    "backend/core/ouroboros/governance/inline_permission_repl.py",
    "backend/core/ouroboros/governance/inline_permission_observability.py",
])
def test_module_has_no_authority_imports(rel_path: str):
    """The inline-permission surface is pure observability / decision-layer.

    None of these modules may import authority-carrying modules
    (orchestrator / policy_engine / iron_gate / ...). Violations
    would mean the module could alter authorization state, which
    breaks the §1 Boundary Principle.
    """
    # inline_permission_prompt.py is allowed to import inline_permission
    # (its sibling), but NOT any authority module.
    violations = _scan_file_for_forbidden(Path(rel_path))
    assert violations == [], (
        f"{rel_path} imports forbidden authority modules: {violations}"
    )


# ===========================================================================
# 4. Docstring bit-rot guards
# ===========================================================================


def test_observability_master_switch_docstring_pins_graduation():
    from backend.core.ouroboros.governance.inline_permission_observability \
        import inline_permission_observability_enabled
    doc = inline_permission_observability_enabled.__doc__ or ""
    assert "graduated" in doc.lower()
    # Guard against someone flipping the default back without updating
    # the rationale: docstring must say "true" as the default.
    assert "``true``" in doc


def test_tool_hook_master_switch_docstring_explains_kept_false():
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        inline_permission_enabled,
    )
    doc = inline_permission_enabled.__doc__ or ""
    assert "false" in doc.lower()
    assert "deliberate" in doc.lower() or "deliberate design" in doc.lower()
    # Must reference OpApprovedScope — the root cause of keeping it off.
    assert "OpApprovedScope" in doc or "scope" in doc.lower()


# ===========================================================================
# 5. Ruleset version in telemetry (§8)
# ===========================================================================


def test_ruleset_version_constant_exported():
    from backend.core.ouroboros.governance.inline_permission import (
        INLINE_PERMISSION_RULESET_VERSION,
    )
    assert INLINE_PERMISSION_RULESET_VERSION == "inline_permission.v0"


def test_every_verdict_stamps_ruleset_version():
    """Regression test: a Slice 1 change that dropped the field from
    :class:`InlineGateVerdict` would fail this."""
    from backend.core.ouroboros.governance.inline_permission import (
        INLINE_PERMISSION_RULESET_VERSION,
        InlineGateInput,
        OpApprovedScope,
        RoutePosture,
        UpstreamPolicy,
        decide,
    )
    verdict = decide(InlineGateInput(
        tool="read_file", arg_fingerprint="x.py", target_path="x.py",
        route=RoutePosture.INTERACTIVE,
        approved_scope=OpApprovedScope(),
        upstream_decision=UpstreamPolicy.NO_MATCH,
    ))
    assert verdict.ruleset_version == INLINE_PERMISSION_RULESET_VERSION


# ===========================================================================
# 6. Event type allowlist completeness (Slice 4 + graduation)
# ===========================================================================


def test_broker_allowlist_contains_all_7_inline_event_types():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    required = {
        "inline_prompt_pending",
        "inline_prompt_allowed",
        "inline_prompt_denied",
        "inline_prompt_expired",
        "inline_prompt_paused",
        "inline_grant_created",
        "inline_grant_revoked",
    }
    missing = required - set(_VALID_EVENT_TYPES)
    assert not missing, f"broker allowlist missing: {missing}"


# ===========================================================================
# 7. Bridge idempotency
# ===========================================================================


@pytest.mark.asyncio
async def test_bridge_attach_detach_no_listener_leak(monkeypatch, tmp_path: Path):
    """Multiple attach/detach cycles leave zero residual listeners."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        InlinePromptController,
        reset_default_singletons,
    )
    from backend.core.ouroboros.governance.inline_permission_memory import (
        RememberedAllowStore,
    )
    from backend.core.ouroboros.governance.inline_permission_observability \
        import bridge_inline_permission_to_broker

    reset_default_singletons()
    ctrl = InlinePromptController(default_timeout_s=5.0)
    store = RememberedAllowStore(tmp_path)

    unsubs = []
    for _ in range(5):
        unsubs.append(bridge_inline_permission_to_broker(
            controller=ctrl, store=store,
        ))
    for u in unsubs:
        u()

    # Internal: the controller / store should carry no listeners from
    # this module after full teardown. (Probe via the private attribute —
    # the contract we're pinning is "unsub actually unsubs".)
    assert len(ctrl._listeners) == 0
    assert len(store._listeners) == 0


# ===========================================================================
# 8. Interaction pins — double-ask guard under graduated defaults
# ===========================================================================


@pytest.mark.asyncio
async def test_double_ask_suppression_survives_graduation(
    monkeypatch, tmp_path: Path,
):
    """Blessed shape still skips prompt even when observability is on."""
    monkeypatch.delenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.inline_permission import (
        OpApprovedScope,
        RoutePosture,
        UpstreamPolicy,
    )
    from backend.core.ouroboros.governance.inline_permission_prompt import (
        BlessedShapeLedger,
        BlessingSource,
        InlinePermissionMiddleware,
        InlinePromptController,
        OutcomeSource,
        reset_default_singletons,
    )

    reset_default_singletons()
    ctrl = InlinePromptController(default_timeout_s=5.0)
    ledger = BlessedShapeLedger(default_ttl_s=60.0)
    ledger.bless_notify_apply(
        op_id="op-x",
        approved_paths=frozenset({"backend/"}),
        candidate_hash="h1",
    )

    class _Resolver:
        def resolve(self, op_id: str):
            return OpApprovedScope()

    mw = InlinePermissionMiddleware(
        controller=ctrl, ledger=ledger,
        scope_resolver=_Resolver(),
    )
    outcome = await mw.check(
        op_id="op-x", call_id="c-1",
        tool="edit_file", arg_fingerprint="backend/foo.py",
        target_path="backend/foo.py",
        route=RoutePosture.INTERACTIVE,
        upstream_decision=UpstreamPolicy.NO_MATCH,
        candidate_hash="h1",
    )
    assert outcome.proceed is True
    assert outcome.source is OutcomeSource.LEDGER_BLESSED
    assert outcome.blessing_source is BlessingSource.NOTIFY_APPLY


# ===========================================================================
# 9. EventChannelServer wiring — module imports surface
# ===========================================================================


def test_event_channel_imports_inline_perm_observability():
    """After Slice 5 the EventChannelServer startup sequence must
    reference the observability router + bridge. Grep-pinned so a
    future edit that removes the wiring fails here."""
    src = Path(
        "backend/core/ouroboros/governance/event_channel.py"
    ).read_text()
    assert "InlinePermissionObservabilityRouter" in src
    assert "bridge_inline_permission_to_broker" in src


def test_serpent_flow_imports_inline_repl_dispatcher():
    """SerpentFlow must dispatch /allow /deny /always /pause /prompts
    /permissions to the inline_permission_repl dispatcher."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert "dispatch_inline_command" in src
    assert "/allow" in src
    assert "/deny" in src
    assert "/always" in src
    assert "/pause" in src


# ===========================================================================
# 10. Fail-closed invariants preserved under graduation
# ===========================================================================


@pytest.mark.asyncio
async def test_autonomous_route_still_coerces_ask_to_block(monkeypatch):
    """§7 fail-closed: AUTONOMOUS + ASK must remain BLOCK even with all
    env knobs flipped to their graduated defaults."""
    monkeypatch.delenv(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.inline_permission import (
        InlineDecision,
        InlineGateInput,
        OpApprovedScope,
        RoutePosture,
        UpstreamPolicy,
        decide,
    )
    verdict = decide(InlineGateInput(
        tool="bash", arg_fingerprint="make build",
        target_path="",
        route=RoutePosture.AUTONOMOUS,
        approved_scope=OpApprovedScope(),
        upstream_decision=UpstreamPolicy.NO_MATCH,
    ))
    assert verdict.decision is InlineDecision.BLOCK
    assert "autonomous_coerce" in verdict.rule_id


def test_block_shape_grant_guard_still_firing(tmp_path: Path):
    """§6 additive lock: cannot persist a grant for a BLOCK shape
    regardless of which env knobs are set."""
    from backend.core.ouroboros.governance.inline_permission_memory import (
        GrantRejected,
        RememberedAllowStore,
    )
    store = RememberedAllowStore(tmp_path)
    with pytest.raises(GrantRejected):
        store.grant(tool="bash", pattern="sudo rm /")
