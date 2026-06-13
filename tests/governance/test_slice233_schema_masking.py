"""Slice 233 (RC2) — parse-gate enforcement of convergence.

The Slice 231 live soak proved GOAL-001 reaches the DW agentic path and the
3-axis convergence machinery (Slice 85 cumulative axis + Slice 3E nudge + grace
round) FIRES — yet the op still exhausted on exploration. Root: the final-write
nudge is ADVISORY. The model is told "tool calls will be IGNORED" but nothing
stops it from emitting read-only navigation calls on the grace round, after
which the loop returns the raw (patch-less) response and the op fails.

This loop advertises tools as PROSE in the prompt and parses tool_use from the
model's TEXT (``generate_fn: (prompt) -> str`` + ``parse_fn``) — there is no
structured ``tools=`` array at this boundary to strip. So the absolute invariant
is enforced at the PARSE/EXECUTE gate: once convergence is forced
(``_final_nudge_issued``), PURE read-only navigation rounds are structurally
REJECTED (reusing ``_READONLY_EXPLORATION_TOOLS`` + the Slice 85 trigger), the
model gets a hard denial directing it to emit the patch, bounded by an
enforcement cap AND remaining budget. A valid patch (non-tool response) is
honored at any point; a model that keeps refusing fails cleanly.
"""
from __future__ import annotations

from pathlib import Path

from backend.core.ouroboros.governance.tool_executor import (
    _READONLY_EXPLORATION_TOOLS,
    _ConvergenceEnforcement,
    _enforcement_rounds_cap,
    _slice233_enforcement_action,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_EXECUTOR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance" / "tool_executor.py"
)

_READONLY = ["read_file", "search_code", "glob_files", "list_dir"]
_MUTATION = ["edit_file"]


def _act(**kw):
    base = dict(
        convergence_forced=True,
        tool_call_names=_READONLY,
        readonly_set=_READONLY_EXPLORATION_TOOLS,
        enforcement_rounds_used=0,
        enforcement_cap=2,
        remaining_s=120.0,
        final_write_reserve_s=10.0,
    )
    base.update(kw)
    return _slice233_enforcement_action(**base)


# ── pure decision helper ────────────────────────────────────────────────
class TestEnforcementDecision:
    def test_pure_readonly_after_convergence_is_enforced(self):
        # (a) read-only calls rejected after convergence is forced.
        assert _act() is _ConvergenceEnforcement.ENFORCE

    def test_not_forced_is_normal(self):
        assert _act(convergence_forced=False) is _ConvergenceEnforcement.NORMAL

    def test_cap_reached_finalizes_cleanly(self):
        # (c) bounded-round exit — a model that keeps refusing fails cleanly.
        assert _act(enforcement_rounds_used=2, enforcement_cap=2) is \
            _ConvergenceEnforcement.FINALIZE

    def test_no_budget_for_write_finalizes(self):
        # If enforcing would leave no room for the final write, stop.
        assert _act(remaining_s=8.0, final_write_reserve_s=10.0) is \
            _ConvergenceEnforcement.FINALIZE

    def test_progress_tool_is_not_enforced_here(self):
        # A round touching a mutation/progress tool is not pure exploration —
        # the gate exists to stop exploration, not suppress progress.
        assert _act(tool_call_names=_READONLY + _MUTATION) is \
            _ConvergenceEnforcement.FINALIZE

    def test_mixed_only_readonly_variants_still_enforced(self):
        assert _act(tool_call_names=["git_log", "list_symbols", "get_callers"]) is \
            _ConvergenceEnforcement.ENFORCE

    def test_enforcement_cap_default_and_env(self, monkeypatch):
        monkeypatch.delenv("JARVIS_TOOL_LOOP_ENFORCEMENT_ROUNDS", raising=False)
        assert _enforcement_rounds_cap() == 2
        monkeypatch.setenv("JARVIS_TOOL_LOOP_ENFORCEMENT_ROUNDS", "3")
        assert _enforcement_rounds_cap() == 3
        monkeypatch.setenv("JARVIS_TOOL_LOOP_ENFORCEMENT_ROUNDS", "-1")
        assert _enforcement_rounds_cap() == 2  # invalid → default

    def test_zero_cap_finalizes_immediately(self):
        # Opt-out: cap 0 → never enforce (legacy advisory behavior).
        assert _act(enforcement_rounds_used=0, enforcement_cap=0) is \
            _ConvergenceEnforcement.FINALIZE


# ── AST pins: the enforcement is WIRED into the loop correctly ───────────
def _src() -> str:
    return TOOL_EXECUTOR_FILE.read_text()


class TestWiring:
    def test_enforcement_reuses_existing_readonly_taxonomy(self):
        src = _src()
        # No new tool list — the gate must reuse the Slice 85 evasion-proof set.
        assert "_slice233_enforcement_action" in src
        assert "_READONLY_EXPLORATION_TOOLS" in src

    def test_enforcement_wired_into_final_nudge_block(self):
        src = _src()
        assert "_enforcement_rounds_used" in src
        # The ENFORCE branch must feed a denial back via the proven
        # current_prompt-append + continue pattern (same as the nudge).
        assert "_ConvergenceEnforcement.ENFORCE" in src

    def test_patch_honored_before_enforcement(self):
        # Resilience (b): a non-tool response (valid patch) must return BEFORE
        # the _final_nudge_issued enforcement block is consulted, so a patch
        # emitted mid-enforcement is always honored.
        src = _src()
        none_return = src.index("Final non-tool response")
        # Anchor on the enforcement BLOCK (`if _final_nudge_issued:`), not the
        # `_final_nudge_issued: bool = False` init declaration.
        enforce_block = src.index("if _final_nudge_issued:")
        assert none_return < enforce_block, (
            "the parse_fn-None patch-return must precede the enforcement gate"
        )
