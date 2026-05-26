"""Slice 22 — Dynamic Tier Degradation Engine.

Closes the structural routing gap surfaced by v16 soak
(bt-2026-05-26-220930). When ``JARVIS_PROVIDER_CLAUDE_DISABLED=true``
is set, ClaudeProvider is not constructed (Slice 19a). But the
UrgencyRouter still routed ops to ``IMMEDIATE`` per §5 ("Claude direct,
skip DW"). With Claude absent, the cascade exhausted at the dispatcher
with ``fallback_skipped:no_fallback_configured`` (Slice 19b) — ops
died before any provider call landed. v16 burned 22 minutes and
$0 of useful spend exactly this way.

Slice 22 fixes this STRUCTURALLY at the router rather than tagging
individual envelopes: when the router resolves to IMMEDIATE AND the
Claude tier is structurally absent, demote to STANDARD (DW-primary).
The healing matrix (Slices 20B/20C/20D + Phase 3) is now actually
reachable for the demoted op.

# Test surface (3 AST pins + 8 spine)
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "urgency_router.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice22_helpers_present() -> None:
    """Slice 22 substrate (the 3 module-level helpers) MUST be in place,
    using the canonical env-var names that match Slice 19a's contract."""
    src = UR_FILE.read_text()
    assert "Slice 22" in src, (
        "urgency_router missing Slice 22 attribution — refactor reverted"
    )
    # The 3 required env-var symbols
    assert "TIER_DECAY_ENABLED_ENV_VAR" in src
    assert "JARVIS_TIER_DECAY_ENABLED" in src
    assert "CLAUDE_DISABLED_ENV_VAR" in src
    assert "JARVIS_PROVIDER_CLAUDE_DISABLED" in src, (
        "Slice 22 not aligned with Slice 19a's env-var contract — "
        "decay won't fire when ClaudeProvider construction was skipped"
    )
    # The 3 required helper functions
    for name in (
        "_tier_decay_enabled",
        "_claude_tier_structurally_absent",
        "_apply_immediate_tier_decay",
    ):
        assert f"def {name}(" in src, (
            f"Slice 22 helper {name!r} missing — wiring broken"
        )


def test_ast_pin_immediate_returns_flow_through_decay_helper() -> None:
    """All 4 IMMEDIATE return sites in classify() MUST flow through
    ``_apply_immediate_tier_decay``. Without this, the decay is dead
    code and IMMEDIATE ops still vanish when Claude is absent.

    AST-walk approach: locate classify(), confirm the Priority-1
    IMMEDIATE block has NO direct ``return ProviderRoute.IMMEDIATE``
    statements (all of them must be `return _apply_immediate_tier_decay(...)`)
    """
    src = UR_FILE.read_text()
    tree = ast.parse(src, filename=str(UR_FILE))
    classify_body_src = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "classify"
        ):
            classify_body_src = ast.unparse(node)
            break
    assert classify_body_src, "classify() not found in urgency_router"

    # The decay helper must be invoked at least 4 times (once per
    # IMMEDIATE return site in the Priority-1 block).
    helper_calls = classify_body_src.count("_apply_immediate_tier_decay(")
    assert helper_calls >= 4, (
        f"Expected >= 4 _apply_immediate_tier_decay() calls in classify(), "
        f"got {helper_calls} — IMMEDIATE return sites not all wired"
    )
    # The phrase ``return ProviderRoute.IMMEDIATE, reason`` from the
    # legacy direct-return pattern must NOT appear in the Priority-1
    # block. (The phrase CAN appear in helper text / comments; we
    # search for the executable form specifically.)
    # We use the AST-unparsed normalized form: `return (ProviderRoute.IMMEDIATE, reason)`.
    legacy_returns = [
        n for n in ast.walk(ast.parse(classify_body_src))
        if isinstance(n, ast.Return)
        and isinstance(n.value, ast.Tuple)
        and len(n.value.elts) == 2
        and isinstance(n.value.elts[0], ast.Attribute)
        and isinstance(n.value.elts[0].value, ast.Name)
        and n.value.elts[0].value.id == "ProviderRoute"
        and n.value.elts[0].attr == "IMMEDIATE"
    ]
    assert len(legacy_returns) == 0, (
        f"Found {len(legacy_returns)} direct `return ProviderRoute.IMMEDIATE, ...` "
        "in classify() — Slice 22 wiring incomplete; some sites bypass decay"
    )


def test_ast_pin_attested_log_message_verbatim() -> None:
    """The operator-attested §5 transparency message MUST be present
    verbatim. Any rewording silently weakens the audit trail.

    We AST-walk to find the WARNING call inside
    ``_apply_immediate_tier_decay`` and join its adjacent string
    literals into the concatenated message — that's what Python's
    compiler does, and it's what actually reaches the log handler.
    """
    src = UR_FILE.read_text()
    tree = ast.parse(src, filename=str(UR_FILE))
    concatenated_message = ""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_apply_immediate_tier_decay"
        ):
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "warning"
                    and sub.args
                ):
                    arg0 = sub.args[0]
                    if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                        concatenated_message = arg0.value
                        break
            break
    assert concatenated_message, (
        "_apply_immediate_tier_decay missing logger.warning(...) call "
        "with a string-literal first arg"
    )
    # The operator-attested verbatim clauses (post-concat)
    expected_clauses = (
        "Adaptive tier decay activated: IMMEDIATE → STANDARD",
        "Claude infrastructure tier structurally absent",
    )
    for clause in expected_clauses:
        assert clause in concatenated_message, (
            f"Slice 22 §5-attested clause missing or reworded: {clause!r} "
            f"(actual concatenated message: {concatenated_message!r})"
        )


# ──────────────────────────────────────────────────────────────────────
# Spine — 8
# ──────────────────────────────────────────────────────────────────────


def test_spine_claude_present_immediate_preserved(monkeypatch) -> None:
    """When Claude is NOT structurally absent (default), the decay
    helper passes IMMEDIATE through unchanged. Legacy contract."""
    monkeypatch.delenv("JARVIS_PROVIDER_CLAUDE_DISABLED", raising=False)
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        _apply_immediate_tier_decay, ProviderRoute,
    )
    route, reason = _apply_immediate_tier_decay("voice_command:human_waiting")
    assert route is ProviderRoute.IMMEDIATE, (
        f"Claude present → IMMEDIATE should be preserved, got {route!r}"
    )
    assert reason == "voice_command:human_waiting", (
        f"Reason mutated unexpectedly: {reason!r}"
    )


def test_spine_claude_absent_immediate_demoted_to_standard(monkeypatch) -> None:
    """The core Slice 22 contract: with Claude absent + decay enabled
    (default), IMMEDIATE demotes to STANDARD."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        _apply_immediate_tier_decay, ProviderRoute,
    )
    route, reason = _apply_immediate_tier_decay("voice_command:human_waiting")
    assert route is ProviderRoute.STANDARD, (
        f"Slice 22 demotion did not fire: got {route!r}, expected STANDARD"
    )
    assert "tier_decay:immediate_to_standard:claude_absent" in reason, (
        f"Demotion reason missing forensic trail: {reason!r}"
    )
    # The original reason MUST be preserved at the tail for postmortem
    assert reason.endswith(":voice_command:human_waiting"), (
        f"Original IMMEDIATE rationale lost: {reason!r}"
    )


def test_spine_master_off_preserves_legacy_even_with_claude_absent(
    monkeypatch,
) -> None:
    """Master flag rollback contract: with JARVIS_TIER_DECAY_ENABLED=false,
    legacy behavior is restored even when Claude is absent (forensic
    comparison mode)."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.setenv("JARVIS_TIER_DECAY_ENABLED", "false")
    from backend.core.ouroboros.governance.urgency_router import (
        _apply_immediate_tier_decay, ProviderRoute,
    )
    route, reason = _apply_immediate_tier_decay("cross_repo:5_files")
    assert route is ProviderRoute.IMMEDIATE, (
        "Master-off should preserve legacy IMMEDIATE even with Claude absent"
    )
    assert reason == "cross_repo:5_files", "Reason mutated under master-off"


def test_spine_master_default_true(monkeypatch) -> None:
    """Slice 22 master flag defaults TRUE — there's no graduation
    period because the only failure mode of the demotion is the SAME
    failure mode we have today (cascade exhausts at dispatcher).
    The demotion only fires when JARVIS_PROVIDER_CLAUDE_DISABLED is
    itself opt-in, so this is a load-bearing safety net by default."""
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        _tier_decay_enabled,
    )
    assert _tier_decay_enabled() is True, (
        "JARVIS_TIER_DECAY_ENABLED default flipped — Slice 22 became opt-in"
    )


def test_spine_claude_disabled_env_truthy_variants(monkeypatch) -> None:
    """Slice 22's Claude-absent detector MUST honor the same truthy
    parsing rules as Slice 19a's GovernedLoopService check (true/1/
    yes/on, case-insensitive)."""
    from backend.core.ouroboros.governance.urgency_router import (
        _claude_tier_structurally_absent,
    )
    for variant in ("true", "True", "TRUE", "1", "yes", "YES", "on", "On"):
        monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", variant)
        assert _claude_tier_structurally_absent() is True, (
            f"Variant {variant!r} not recognized as Claude-absent — "
            "Slice 22 detector diverges from Slice 19a contract"
        )
    for variant in ("", "false", "0", "no", "off", "random"):
        monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", variant)
        assert _claude_tier_structurally_absent() is False, (
            f"Variant {variant!r} mis-classified as Claude-absent — "
            "Slice 22 false-positive demotion risk"
        )


def test_spine_attested_message_fires_on_demotion(monkeypatch, caplog) -> None:
    """Verifies the §5 attestation log message is emitted EXACTLY
    when a demotion fires — operator's transparency contract."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        _apply_immediate_tier_decay,
    )
    with caplog.at_level(logging.WARNING):
        _apply_immediate_tier_decay("test")
    # Find the attestation line
    matches = [
        r for r in caplog.records
        if "Adaptive tier decay activated" in r.getMessage()
    ]
    assert len(matches) == 1, (
        f"Attestation message fired {len(matches)} times, expected exactly 1; "
        f"all messages: {[r.getMessage() for r in caplog.records]!r}"
    )
    assert matches[0].levelno == logging.WARNING, (
        "Attestation must be WARNING level (operator-visible without grep)"
    )


def test_spine_end_to_end_classify_with_voice_command(monkeypatch) -> None:
    """End-to-end: build a voice_human ctx (which classify normally
    routes IMMEDIATE), Claude disabled, expect STANDARD demotion."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", raising=False)
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_WIRING_VALIDATION_ROUTE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )

    ctx = mock.MagicMock()
    ctx.signal_urgency = "normal"
    ctx.signal_source = "voice_human"
    ctx.task_complexity = "moderate"
    ctx.target_files = ()
    ctx.cross_repo = False
    ctx.provider_route = ""
    ctx.provider_route_reason = ""

    router = UrgencyRouter()
    route, reason = router.classify(ctx)

    assert route is ProviderRoute.STANDARD, (
        f"End-to-end voice_human + Claude disabled → expected STANDARD demotion, "
        f"got {route!r} reason={reason!r}"
    )
    assert "tier_decay:immediate_to_standard:claude_absent" in reason
    # Original "voice_command:human_waiting" preserved at the tail
    assert "voice_command:human_waiting" in reason


def test_spine_non_immediate_routes_untouched(monkeypatch) -> None:
    """STANDARD/COMPLEX/BACKGROUND/SPECULATIVE routes must NOT flow
    through the decay helper — only IMMEDIATE returns are wrapped.
    Verified by classifying a clear-cut background op + asserting
    no decay-tagged reason."""
    monkeypatch.setenv("JARVIS_PROVIDER_CLAUDE_DISABLED", "true")
    monkeypatch.delenv("JARVIS_TIER_DECAY_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", raising=False)
    monkeypatch.delenv("JARVIS_BACKLOG_URGENCY_HINT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_WIRING_VALIDATION_ROUTE_ENABLED", raising=False)
    from backend.core.ouroboros.governance.urgency_router import (
        UrgencyRouter, ProviderRoute,
    )

    ctx = mock.MagicMock()
    ctx.signal_urgency = "low"
    ctx.signal_source = "opportunity_miner"  # background source
    ctx.task_complexity = "trivial"
    ctx.target_files = ()
    ctx.cross_repo = False
    ctx.provider_route = ""
    ctx.provider_route_reason = ""

    router = UrgencyRouter()
    route, reason = router.classify(ctx)

    # Whatever route it lands on, it MUST NOT be tagged with the
    # tier_decay forensic trail (only IMMEDIATE demotions get that).
    assert "tier_decay:immediate_to_standard" not in reason, (
        f"Non-IMMEDIATE route picked up Slice 22 decay tag — wiring leaked: "
        f"route={route!r} reason={reason!r}"
    )
    # Must NOT be IMMEDIATE either (background source, low urgency)
    assert route is not ProviderRoute.IMMEDIATE
