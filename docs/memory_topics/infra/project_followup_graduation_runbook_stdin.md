---
title: Graduation runbook: ban infinite-stdin trick, prefer explicit wall-clock cap
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_followup_graduation_runbook_stdin.md
---

# Graduation runbook: ban infinite-stdin trick, prefer explicit wall-clock cap

## Context

During #6 SLICE4B graduation, agent discovered that launching `python3 scripts/ouroboros_battle_test.py ...` with `</dev/null` or plain background (`&`) caused SerpentFlow's REPL `PromptSession.prompt_async()` to return EOF on the first loop iteration, hitting `except EOFError: break` and ending the harness in ~16 log lines. Workaround: feed the process stdin from an always-open pipe via `tail -f /dev/null | python3 ...`.

This works but is opaque. Operator directive 2026-04-23:

> Background command hygiene: Ensure long-running soak commands are documented (purpose, flag, cap, stop condition). Avoid opaque `tail -f /dev/null | …` unless that's an intentional stdin guard—if it is, one-line comment in the runbook.

The matrix runbook note (added 2026-04-23) documents the idiom but only as a current workaround. The right fix is to replace it with a proper headless mode + wall-clock guard.

## Proposed fix (spec, not implementation)

### Fix 1 — Native headless mode

Add a `--headless` flag to `scripts/ouroboros_battle_test.py` (and/or auto-detect via `not sys.stdin.isatty()`) that:

1. Skips starting the `SerpentReplInput` task entirely. The REPL is a no-op in headless runs — starting it just to immediately hit `except EOFError: break` is wasted infrastructure.
2. Keeps all other surfaces active (CommProtocol transports, LiveDashboard toolbar rendering for log-file consumers, status_line, etc.) — only the *input* half is disabled.
3. Logs `[Harness] Headless mode: REPL input disabled` at INFO so the session artifact shows which branch ran.

This removes the need for the `tail -f /dev/null` prefix entirely.

### Fix 2 — Mandatory wall-clock cap for graduation class

Per Ticket A, `--max-wall-seconds` is added as a separate knob. Runbook update: every graduation soak launches with BOTH `--headless` AND `--max-wall-seconds 2400` (or graduation-operator-chosen value). Future ledger rows document both.

### Fix 3 — Runbook doc cleanup

- Move the `tail -f /dev/null` idiom out of the matrix's "Background-command hygiene" section and into a deprecation note tagged "retained only until Fix 1 ships; do not copy to new runbooks."
- Add a new canonical launch recipe:

  ```bash
  # Canonical graduation-soak launch (after Tickets A+C ship):
  JARVIS_PHASE_RUNNER_<FLAG>=true python3 scripts/ouroboros_battle_test.py \
      --headless \
      --max-wall-seconds 2400 \
      --cost-cap 1.00 \
      --idle-timeout 600 \
      -v \
      > /tmp/claude/<flag>_<session_n>.log 2>&1
  ```

- Explicit comment block at the top of the recipe: `# purpose: <flag> graduation Session <N>; stop: first of idle_timeout | budget | wall_clock_cap`.

## Blast radius

Zero runtime semantics. The REPL is already skipped-by-behavior in the current workaround (stdin-silent pipe means prompt_async blocks forever). Fix 1 just makes the skip explicit and removes the workaround.

## Test plan

- Unit: launch harness with `--headless`, assert `SerpentReplInput.start()` is not called.
- Integration: launch harness with `--headless --max-wall-seconds 30`, assert it exits cleanly at ~30s with `stop_reason=wall_clock_cap` and a valid summary.json.
- Runbook smoke: agent-conducted soak launches a graduation session via the canonical recipe, asserts session completes within expected window with `session_outcome=complete`.

## Relation to graduation work

Not a graduation blocker for #7 (Ticket A is the real unblocker). But the runbook cleanup should land alongside A so the new "how to run a graduation soak" docs are coherent. Graduation cadence restart for #7 GENERATE (S2′, S3) should use the canonical recipe from this ticket + the wall-clock cap from Ticket A.

## Priority

Lower than A. Can be a separate PR or bundled with A if it keeps the diff coherent.
