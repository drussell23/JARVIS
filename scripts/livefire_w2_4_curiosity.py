#!/usr/bin/env python3
"""W2(4) Slice 4 — formal live-fire smoke for the curiosity engine.

Boots the production primitive + Rule 14 + JSONL ledger + SSE bridge
end-to-end with **default (master-on) env** — no overrides. Asserts the
full chain in-process so it can run on any developer machine without
needing a live Anthropic API key or a battle-test harness.

Coverage (10 checks):

1. Default master flag is True (post-graduation).
2. Default sub-flags compose correctly (3 questions / $0.05 / EXPLORE+CONSOLIDATE).
3. SSE sub-flag default-off (operator opt-in preserved).
4. CuriosityBudget construction + ContextVar binding.
5. Rule 14: SAFE_AUTO ask_human ALLOWED with full chain (master+budget+posture).
6. JSONL ledger record persisted (schema curiosity.1).
7. Counter incremented after Allow.
8. Quota exhaustion: 4th charge denies with QUESTIONS_EXHAUSTED.
9. SSE bridge fires with all 3 flags on (curiosity master + sub + IDE stream).
10. Hot-revert: master-off → SAFE_AUTO ask_human reverts to legacy reject.

Exit code 0 on PASS; non-zero on FAIL with a summary of the failed check(s).

Usage::

    python3 scripts/livefire_w2_4_curiosity.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Repo root on sys.path
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class Journal:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  [PASS] {name}")
        else:
            self.failed.append((name, detail))
            print(f"  [FAIL] {name}  ({detail})")

    def summary(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 64}")
        print(f"Result: {len(self.passed)}/{total} checks passed")
        if self.failed:
            print("\nFailures:")
            for n, d in self.failed:
                print(f"  - {n}: {d}")
            return 1
        print("All checks passed — W2(4) graduation live-fire smoke OK.")
        return 0


# ---------------------------------------------------------------------------
# Live-fire body
# ---------------------------------------------------------------------------


def main() -> int:
    j = Journal()
    print("=" * 64)
    print("W2(4) Slice 4 — Curiosity engine live-fire smoke")
    print("=" * 64)

    # Reset all curiosity envs to default — proves the production
    # post-graduation default chain works end-to-end with zero overrides.
    for key in (
        "JARVIS_CURIOSITY_ENABLED",
        "JARVIS_CURIOSITY_QUESTIONS_PER_SESSION",
        "JARVIS_CURIOSITY_COST_CAP_USD",
        "JARVIS_CURIOSITY_POSTURE_ALLOWLIST",
        "JARVIS_CURIOSITY_LEDGER_PERSIST_ENABLED",
        "JARVIS_CURIOSITY_SSE_ENABLED",
    ):
        os.environ.pop(key, None)

    from backend.core.ouroboros.governance.curiosity_engine import (
        CuriosityBudget,
        DenyReason,
        cost_cap_usd,
        curiosity_budget_var,
        curiosity_enabled,
        ledger_persist_enabled,
        posture_allowlist,
        questions_per_session,
        sse_enabled,
    )

    # --- (1) Default master is True ---------------------------------------
    j.check(
        "1. Master flag defaults True (post-graduation)",
        curiosity_enabled() is True,
        f"got {curiosity_enabled()}",
    )

    # --- (2) Default sub-flags compose ------------------------------------
    j.check(
        "2a. questions_per_session defaults 3",
        questions_per_session() == 3,
        f"got {questions_per_session()}",
    )
    j.check(
        "2b. cost_cap_usd defaults $0.05",
        cost_cap_usd() == 0.05,
        f"got {cost_cap_usd()}",
    )
    j.check(
        "2c. posture_allowlist defaults {EXPLORE, CONSOLIDATE}",
        posture_allowlist() == frozenset({"EXPLORE", "CONSOLIDATE"}),
        f"got {sorted(posture_allowlist())}",
    )
    j.check(
        "2d. ledger_persist_enabled defaults True (master-on)",
        ledger_persist_enabled() is True,
    )

    # --- (3) SSE default-off (operator opt-in) ---------------------------
    j.check(
        "3. sse_enabled defaults False (operator opt-in preserved)",
        sse_enabled() is False,
    )

    # --- (4) Budget + ContextVar ------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td)
        bud = CuriosityBudget(
            op_id="op-livefire-w24",
            posture_at_arm="EXPLORE",
            session_dir=session_dir,
        )
        curiosity_budget_var.set(bud)
        j.check(
            "4. CuriosityBudget binds to ContextVar",
            curiosity_budget_var.get() is bud,
        )

        # --- (5) Rule 14 SAFE_AUTO ALLOWED with full chain ---------------
        from backend.core.ouroboros.governance.risk_engine import RiskTier
        from backend.core.ouroboros.governance.tool_executor import (
            GoverningToolPolicy,
            PolicyContext,
            PolicyDecision,
            ToolCall,
        )
        ctx = PolicyContext(
            repo="jarvis", repo_root=session_dir,
            op_id="op-livefire-w24",
            call_id="op-livefire-w24:r0:ask_human",
            round_index=0, risk_tier=RiskTier.SAFE_AUTO, is_read_only=False,
        )
        gate = GoverningToolPolicy(repo_roots={"jarvis": session_dir})
        result = gate.evaluate(
            ToolCall(
                name="ask_human",
                arguments={"question": "Should I refactor X?"},
            ),
            ctx,
        )
        j.check(
            "5. Rule 14 ALLOWS SAFE_AUTO ask_human via curiosity widening",
            result.decision is PolicyDecision.ALLOW,
            f"decision={result.decision} reason={result.reason_code}",
        )

        # --- (6) JSONL ledger persisted ----------------------------------
        ledger_path = session_dir / "curiosity_ledger.jsonl"
        j.check(
            "6a. curiosity_ledger.jsonl was written",
            ledger_path.exists(),
            f"missing {ledger_path}",
        )
        if ledger_path.exists():
            import json as _json
            lines = [
                ln for ln in ledger_path.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
            j.check(
                "6b. ledger has >= 1 record",
                len(lines) >= 1,
                f"got {len(lines)} lines",
            )
            if lines:
                rec = _json.loads(lines[0])
                j.check(
                    "6c. record schema_version is curiosity.1",
                    rec.get("schema_version") == "curiosity.1",
                    f"got {rec.get('schema_version')}",
                )
                j.check(
                    "6d. record result is 'allowed'",
                    rec.get("result") == "allowed",
                    f"got {rec.get('result')}",
                )

        # --- (7) Counter incremented -------------------------------------
        j.check(
            "7. Counter incremented after Allow",
            bud.questions_used == 1,
            f"got {bud.questions_used}",
        )

        # --- (8) Quota exhaustion ----------------------------------------
        # Charge 2 more (uses 2/3) then 4th must deny
        for i in range(2):
            r = bud.try_charge(question_text=f"Q{i}", est_cost_usd=0.01)
            if not r.allowed:
                j.check(
                    f"8a.{i} pre-quota charge succeeded",
                    False,
                    f"unexpected deny: {r.deny_reason}",
                )
        r = bud.try_charge(question_text="Q4-overflow", est_cost_usd=0.01)
        j.check(
            "8. 4th charge denied with QUESTIONS_EXHAUSTED",
            r.allowed is False and r.deny_reason is DenyReason.QUESTIONS_EXHAUSTED,
            f"allowed={r.allowed} deny_reason={r.deny_reason}",
        )

        # --- (9) SSE bridge fires when all 3 flags on --------------------
        os.environ["JARVIS_CURIOSITY_SSE_ENABLED"] = "true"
        os.environ["JARVIS_IDE_STREAM_ENABLED"] = "true"
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
            get_default_broker,
            reset_default_broker,
        )
        reset_default_broker()
        broker = get_default_broker()
        pre = broker.published_count
        # Fresh budget (counter is exhausted on the previous bud) so SSE fires
        bud2 = CuriosityBudget(
            op_id="op-livefire-sse",
            posture_at_arm="EXPLORE",
            session_dir=session_dir,
        )
        curiosity_budget_var.set(bud2)
        bud2.try_charge(question_text="SSE smoke?", est_cost_usd=0.01)
        post = broker.published_count
        j.check(
            "9a. SSE broker received an event (count++)",
            post == pre + 1,
            f"pre={pre} post={post}",
        )
        if post == pre + 1:
            last = list(broker._history)[-1]  # noqa: SLF001
            j.check(
                "9b. SSE event_type is curiosity_question_emitted",
                last.event_type == EVENT_TYPE_CURIOSITY_QUESTION_EMITTED,
                f"got {last.event_type}",
            )
            j.check(
                "9c. SSE op_id matches charging op",
                last.op_id == "op-livefire-sse",
                f"got {last.op_id}",
            )
        reset_default_broker()
        del os.environ["JARVIS_CURIOSITY_SSE_ENABLED"]
        del os.environ["JARVIS_IDE_STREAM_ENABLED"]

        # --- (10) Hot-revert: master-off ---------------------------------
        os.environ["JARVIS_CURIOSITY_ENABLED"] = "false"
        bud3 = CuriosityBudget(
            op_id="op-livefire-revert",
            posture_at_arm="EXPLORE",
        )
        curiosity_budget_var.set(bud3)
        ctx2 = PolicyContext(
            repo="jarvis", repo_root=session_dir,
            op_id="op-livefire-revert",
            call_id="op-livefire-revert:r0:ask_human",
            round_index=0, risk_tier=RiskTier.SAFE_AUTO, is_read_only=False,
        )
        result_revert = gate.evaluate(
            ToolCall(name="ask_human", arguments={"question": "X?"}),
            ctx2,
        )
        j.check(
            "10a. Hot-revert: SAFE_AUTO ask_human reverts to legacy DENY",
            result_revert.decision is PolicyDecision.DENY,
            f"got {result_revert.decision}",
        )
        j.check(
            "10b. Legacy reason code is tool.denied.ask_human_low_risk",
            result_revert.reason_code == "tool.denied.ask_human_low_risk",
            f"got {result_revert.reason_code}",
        )
        j.check(
            "10c. Hot-revert: counter unchanged (master-off → no increment)",
            bud3.questions_used == 0,
            f"got {bud3.questions_used}",
        )
        del os.environ["JARVIS_CURIOSITY_ENABLED"]

    return j.summary()


if __name__ == "__main__":
    sys.exit(main())
