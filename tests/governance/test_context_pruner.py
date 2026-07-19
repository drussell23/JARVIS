"""Cognitive Context Pruning — semantic GC + Ballistic Interceptor spine.

Operator mandates:
  Test A — a GRADUAL breach of the 80% threshold triggers semantic GC:
    resolved trajectories compact into working memory, the North Star +
    active frontier survive byte-identical, and the estimate lands back
    under threshold with the active task intact.
  Test B — the BALLISTIC SPIKE: a single ~150k-token tool payload is
    intercepted BEFORE merging into FSM state — head+tail survive with
    a forensic digest and the window is never breached.
Plus: policy-card limit resolution, TokenLedger calibration, master-off
byte-parity, protected-set immunity under adversarial pressure, and the
seam wiring pins (tool chokepoint + the ONE twin-path prompt gate).
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import context_pruner as cp


@pytest.fixture(autouse=True)
def _armed(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_PRUNE_ENABLED", "true")
    cp.reset_default_ledger()
    yield
    cp.reset_default_ledger()


# ---------------------------------------------------------------------------
# Limit resolution — policy cards + family fallbacks
# ---------------------------------------------------------------------------


def test_policy_card_limit_resolves():
    # LongCat cards declare context_window: 131072 in the REAL yaml.
    assert cp.resolve_context_limit(model="LongCat-Flash-Chat") == 131072


def test_family_fallbacks_env_tunable(monkeypatch):
    assert cp.resolve_context_limit(provider="claude") == 200_000
    assert cp.resolve_context_limit(provider="doubleword") == 262_144
    assert cp.resolve_context_limit(provider="mystery") == 131_072
    monkeypatch.setenv("JARVIS_CONTEXT_LIMIT_CLAUDE", "123456")
    assert cp.resolve_context_limit(provider="claude") == 123_456


def test_limit_never_raises():
    assert cp.resolve_context_limit(provider=None, model=None) > 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TokenLedger — self-calibration
# ---------------------------------------------------------------------------


def test_ledger_estimates_and_calibrates():
    led = cp.TokenLedger()
    assert led.estimate_tokens("a" * 400) == 100          # 4.0 default
    led.observe_actual(chars=1000, tokens=500)            # density 2.0
    assert led.chars_per_token < 4.0
    led.observe_actual(chars=1000, tokens=500)
    assert led.estimate_tokens("a" * 400) > 100           # denser now


def test_ledger_rejects_implausible_observations():
    led = cp.TokenLedger()
    led.observe_actual(chars=1000, tokens=1)              # 1000 c/t — absurd
    led.observe_actual(chars=0, tokens=100)
    assert led.chars_per_token == 4.0


# ---------------------------------------------------------------------------
# Test A — gradual breach → semantic GC, protected set immune
# ---------------------------------------------------------------------------


NORTH_STAR = "## North Star Directive\nA-level autonomy: sense, decide, act.\n"
FRONTIER = "## Current Frontier\nAPPLY-boundary: land the first live commit.\n"
TASK = "## Task\nOp-ID: op-test-1\nGoal: fix the flaky retry in intake.\n"


def _bloated_prompt(filler_sections: int = 6, section_chars: int = 40_000) -> str:
    parts = [NORTH_STAR, FRONTIER, TASK]
    for i in range(filler_sections):
        parts.append(
            f"## Previous Session {i} Log\n" + (f"resolved trace {i} · " * (section_chars // 20))
        )
    return "\n".join(parts)


async def test_gradual_breach_triggers_semantic_gc():
    text = _bloated_prompt()
    led = cp.TokenLedger()
    limit = 50_000                                        # 80% => 40k budget
    assert led.estimate_tokens(text) > int(limit * 0.8)   # genuinely breached

    pruned, tel = await cp.prune_prompt_text(
        text, limit_tokens=limit, ledger=led,
    )
    assert tel["fired"] is True
    assert tel["sections_compacted"] >= 1
    # Reduced back under the threshold budget.
    assert tel["tokens_after"] <= int(limit * cp.prune_threshold())
    # PROTECTED SET — byte-identical survival.
    assert NORTH_STAR.strip() in pruned
    assert FRONTIER.strip() in pruned
    assert TASK.strip() in pruned
    # Resolved trajectories migrated into working memory.
    assert "## Working Memory (compressed trajectories)" in pruned
    assert "working-memory digest" in pruned or "Previous Session" in pruned


async def test_under_threshold_is_byte_identical():
    text = NORTH_STAR + FRONTIER + TASK
    pruned, tel = await cp.prune_prompt_text(
        text, limit_tokens=200_000,
    )
    assert pruned == text
    assert tel["fired"] is False


async def test_master_off_is_byte_identical(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_PRUNE_ENABLED", "false")
    text = _bloated_prompt()
    pruned, tel = await cp.prune_prompt_text(text, limit_tokens=10_000)
    assert pruned == text
    assert tel["fired"] is False


async def test_protected_sections_immune_under_adversarial_pressure():
    """Even when ONLY protected sections exist and the budget is
    impossible, the gate refuses to touch them — over-budget beats
    identity loss."""
    text = "\n".join([NORTH_STAR, FRONTIER, TASK]) * 50
    pruned, tel = await cp.prune_prompt_text(text, limit_tokens=1_000)
    assert NORTH_STAR.strip() in pruned
    assert tel["sections_compacted"] == 0                 # nothing eligible


async def test_unstructured_text_keeps_head_and_tail():
    text = "IDENTITY HEAD " + ("x" * 300_000) + " LIVE TASK TAIL"
    pruned, tel = await cp.prune_prompt_text(text, limit_tokens=20_000)
    assert tel["fired"] is True
    assert pruned.startswith("IDENTITY HEAD")
    assert pruned.rstrip().endswith("LIVE TASK TAIL")
    assert "Working Memory" in pruned


# ---------------------------------------------------------------------------
# Test B — the Ballistic Spike
# ---------------------------------------------------------------------------


def test_ballistic_spike_150k_tokens_intercepted():
    """A single tool payload of ~150k tokens (600k chars at the default
    density) against a 200k window: the interceptor must catch it,
    chunk it, and keep it under the per-payload fraction — the window
    is never threatened by one payload."""
    led = cp.TokenLedger()
    limit = 200_000
    spike = "log line: retry storm detected\n" * 20_000    # ~600k chars
    assert led.estimate_tokens(spike) >= 150_000

    safe, tel = cp.ballistic_intercept(
        spike, limit_tokens=limit, label="tool:read_file", ledger=led,
    )
    assert tel["intercepted"] is True
    budget_chars = cp.ballistic_char_budget(limit, led)
    assert len(safe) <= budget_chars + 400                 # digest margin
    assert led.estimate_tokens(safe) < int(limit * 0.30)
    # Forensics + surgical-re-read affordance survive.
    assert "ballistic-intercept" in safe
    assert "sha256:" in safe
    assert "re-read narrower ranges" in safe
    # Head AND tail context both survive.
    assert safe.startswith("log line:")
    assert safe.rstrip().endswith("retry storm detected")


def test_ballistic_small_payload_untouched():
    safe, tel = cp.ballistic_intercept(
        "small result", limit_tokens=200_000,
    )
    assert safe == "small result"
    assert tel["intercepted"] is False


def test_ballistic_master_off_untouched(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTEXT_PRUNE_ENABLED", "false")
    spike = "y" * 1_000_000
    safe, tel = cp.ballistic_intercept(spike, limit_tokens=100_000)
    assert safe == spike
    assert tel["intercepted"] is False


def test_ballistic_fraction_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_BALLISTIC_MAX_FRACTION", "0.05")
    led = cp.TokenLedger()
    assert cp.ballistic_char_budget(100_000, led) == int(100_000 * 0.05 * 4.0)


def test_ballistic_never_raises():
    safe, tel = cp.ballistic_intercept(None, limit_tokens=0)  # type: ignore[arg-type]
    assert tel["intercepted"] is False


# ---------------------------------------------------------------------------
# Seam wiring pins
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / rel).read_text()


def test_tool_chokepoint_intercepts_before_static_cap():
    src = _read("backend/core/ouroboros/governance/tool_executor.py")
    body = src[src.index("def _format_tool_result"):][:3000]
    assert "ballistic_intercept" in body
    # Token-aware interception fires BEFORE the static byte-cap slice.
    assert body.index("ballistic_intercept") < body.index("len(raw_output) > cap")


def test_prompt_gate_is_single_sourced_across_twin_paths():
    src = _read("backend/core/ouroboros/governance/providers.py")
    assert src.count("async def _context_prune_gate") == 1
    calls = src.count("await _context_prune_gate(")
    assert calls == 2                                      # Claude + DW twins
    assert '_context_prune_gate(prompt, "claude")' in src
    assert '_context_prune_gate(prompt_text, "doubleword")' in src


def test_gc_composes_canonical_compactor():
    src = _read("backend/core/ouroboros/governance/context_pruner.py")
    assert "from backend.core.ouroboros.governance.context_compaction import" in src
    assert "brain_selection_policy" in src                 # limit authority
