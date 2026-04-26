# P0 PostmortemRecall Graduation Soak Ledger

Tracks the 3-session graduation soak for `PostmortemRecall` (PRD §11 Layer 4).
Merged commit: `d708c5d425` — PR #20976.
Required: 3 consecutive CLEAN sessions before graduation PR may be opened.

## Columns

| session_id | start_utc | duration_s | stop_reason | session_outcome | postmortem_recall_markers | clean_verdict | notes |
|---|---|---|---|---|---|---|---|
| N/A | 2026-04-26T02:04:00Z | ~15 | api_key_missing | not_started | 0 | BLOCKED | ANTHROPIC_API_KEY + DOUBLEWORD_API_KEY both unset in sandbox env. Sanity gate passed: 16/16 livefire + 57/57 pytest. Harness preflight exits before session creation. Must set ≥1 API key to run soak. |

---

> **Soak conductor:** agent.
> **HUMAN_REVIEW_WAIVED:** TTY-only affordances (Rich diff, REPL) not exercised in headless soak — accepted residual risk per feedback_agent_conducted_soak_delegation.md.
