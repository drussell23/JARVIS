---
title: §8 partial summary.json on interrupt
modules: [scripts/ouroboros_battle_test.py]
status: historical
source: project_followup_partial_summary_on_interrupt.md
---

# §8 partial summary.json on interrupt

## Observed behavior

Session `bt-2026-04-23-070317` was killed by operator directive after it entered a Claude API retry purgatory (see Ticket A). The kill was issued via Claude Code's `TaskStop` on the background Bash-tool task, which in this architecture corresponds to `SIGHUP` on the parent `bash` process that owned the `tail -f /dev/null | python3 scripts/ouroboros_battle_test.py ...` pipeline.

The Python child process apparently exited without running the existing atexit fallback (which is documented in CLAUDE.md as partial-shutdown insurance):

> Partial-shutdown insurance: the harness registers an `atexit` fallback **and** a sync signal-handler write so every session dir ends up with a v1.1a-parseable `summary.json` — even when SIGTERM arrives mid-cleanup or the async finally can't complete. `SIGKILL` remains unrecoverable by design (OS-level, uncatchable in Python).

Post-kill state: `.ouroboros/sessions/bt-2026-04-23-070317/` contains only `debug.log`. No `summary.json`, no `cost_tracker.json`, no `replay.html`. The session cannot be auto-parsed by `LastSessionSummary` (`v1.1a`). Graduation ledger has to manually reconstruct the session row from debug.log alone.

## Root-cause hypothesis

The SIGHUP from the parent bash is likely what killed the Python child. Two candidate explanations for why the signal handler didn't fire:

1. **The harness's signal handler is only installed for SIGTERM/SIGINT**, not SIGHUP. `trap` behavior on pipeline death may send SIGHUP rather than SIGTERM to the pipeline members. The harness would exit abruptly without its handler firing.

2. **The `tail -f /dev/null | python3 ...` wrapper**. When bash dies, stdin is closed; the Python process may exit via a broken-pipe chain rather than a signal. `atexit` handlers run on normal Python exit, but if the exit is via C-level process-group cleanup, `atexit` may be skipped.

Investigation needed — don't guess the fix.

## Proposed fix (spec, not implementation)

1. **Install signal handlers for SIGHUP + SIGTERM + SIGINT** (and SIGPIPE with explicit ignore to prevent broken-pipe crashes in the `tail -f` idiom from killing the Python process).
2. **The handler writes a partial summary.json with `session_outcome=incomplete_kill`** before the process exits. Fields: session_id, start_ts, last_activity_ts, stop_reason=operator_interrupt|sighup|sigterm, partial stats (ops seen, markers fired per phase, any PM), traceback-signature-frequencies. Schema extension to `v1.1b` (already parked per session memory).
3. **Harness-side test**: spawn a child harness, SIGHUP it mid-run, assert `summary.json` exists with the partial schema.

## Adjacent hardening (consider grouping into same PR)

- The existing `atexit` fallback path should be audited — does it actually write on non-SIGKILL exits? Add a unit test that runs the harness main in a subprocess, kills with SIGTERM, asserts summary.json lands.
- The `tail -f /dev/null` stdin idiom documented in the graduation matrix runbook is already flagged for replacement by `--max-wall-seconds` + proper headless mode (see Ticket C). Fixing B makes C's migration safer.

## Blast radius

Zero happy-path impact (signal handlers don't fire on normal exit). Edge-case impact only: external-kill sessions gain an audit trail they currently lack.

## Relation to graduation work

Not a graduation blocker per se, but the S2 kill in #7 cadence is the proof case. Once Ticket A's `--max-wall-seconds` lands, wall-clock-cap shutdowns will go through the normal `finally:` path and write summary.json natively — so B becomes a "defense in depth" ticket rather than a primary unblocker. Still worth shipping for the genuinely-externally-killed case (operator panics, CI timeout, cloud instance reclamation).

## Not in this ticket

- Ticket A's wall-clock cap (separate unblocker).
- Ticket C's runbook update (separate docs).
- Retroactive summary.json for the S2 session (pointless — just keep the debug.log + ledger row as "negative evidence" per operator directive).
