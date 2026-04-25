# Battle-Test Operator Runbook

Last updated: 2026-04-25 (Harness Epic Slice 3).

This runbook is the canonical operator reference for launching, monitoring,
and recovering battle-test sessions of the Ouroboros pipeline. It supersedes
ad-hoc runbook fragments scattered across older session-graduation docs.

If you are an LLM agent reading this to figure out how to run a battle-test,
follow it literally. Do **not** invent variations.

## TL;DR — standard launch

```bash
python3 scripts/ouroboros_battle_test.py \
  --cost-cap 2.00 \
  --idle-timeout 600 \
  --max-wall-seconds 2400 \
  --headless -v
```

This recipe is the only supported invocation pattern. Operators and agent-
conducted soaks both use this exact form (vary the numeric flags as
appropriate).

## What changed in the harness epic (Slices 1–4, 2026-04-25)

| Slice | What it added |
|---|---|
| 1 | `BoundedShutdownWatchdog` — daemon thread + `os._exit(75)` after 30s if asyncio shutdown wedges. |
| 2 | `intake_router.lock` schema upgrade + wedged-but-alive TTL detection + single-flight launcher preflight. |
| 3 | Process hygiene canonicalization (this runbook + CI grep guard for banned patterns). |
| 4 | Graduation pins. |

**You no longer need to manually clean up zombies, stale locks, or wedged
sessions in normal operation.** Slices 1+2 cure the 14-incident class
structurally.

## Canonical process probe

Use this exact form everywhere — runbooks, agent prompts, debugging notes,
ad-hoc shell sessions:

```bash
pgrep -f "python3? scripts/ouroboros_battle_test\.py"
```

The `python3?` (regex `?` quantifier) accepts both `python` and `python3`.
The path-anchored `scripts/ouroboros_battle_test\.py` avoids matching:

* `zsh` wrapper processes that contain the script path inside their `eval`
  text (the ones with `/bin/zsh -c source ... && eval '...python3 scripts/ouroboros_battle_test.py ...'`).
* IDE search tabs that happen to mention the path.
* `grep` invocations searching for the pattern.

Anti-patterns (do **not** use any of these — they false-positive or
false-negative):

```bash
# Too loose — matches zsh wrappers + grep itself
pgrep -f ouroboros_battle_test
# Too loose — matches any python process running anything ouroboros
pgrep -f "python.*ouroboros"
# Wrong tool — pgrep without -f matches process name (always "python3"),
# never the script
pgrep ouroboros_battle_test
```

## Banned pattern: `tail -f /dev/null | python`

The deprecated `tail -f /dev/null | python3 scripts/ouroboros_battle_test.py ...`
stdin-guard idiom is **banned** in `docs/` and `scripts/`. CI enforces
this via `scripts/check_no_stdin_guard.sh`.

Why it's banned (per S5/S6 incidents 2026-04-24):

* When the parent shell dies, `tail -f /dev/null` keeps stdout open, so
  the Python child never receives SIGPIPE. The Python child becomes an
  orphan but stays alive — leading to the Py_FinalizeEx zombie class
  (14 documented incidents).
* The 7 orphans observed in the S4 mass-cleanup all had this pattern.

The replacement is built-in:

```bash
python3 scripts/ouroboros_battle_test.py --headless ...
```

The `--headless` flag (or env `OUROBOROS_BATTLE_HEADLESS=true`) skips the
`SerpentREPL` input task entirely — no stdin needed, no parent-shell
dependency. Auto-detected via `not sys.stdin.isatty()` when omitted.

## Recovery procedures

### Symptom: launcher exits with code 75

```
[single-flight] REJECTED — concurrent battle-test detected
  • pgrep: PID 12345
  • lock: PID 12345
  exit code 75 (EX_TEMPFAIL) — try again after the other run completes
```

This is expected behavior — Slice 2 single-flight rejected your launch
because another battle-test is already running. Two valid responses:

1. **Wait** for the other run to complete naturally.
2. **Take over** if you're sure the other run is yours and you want to
   replace it. Kill the other PID:
   ```bash
   pgrep -f "python3? scripts/ouroboros_battle_test\.py" | xargs kill -TERM
   sleep 30  # bounded-shutdown deadline
   ```
   Then re-launch.

**Override** (operator escape hatch): `JARVIS_BATTLE_SINGLE_FLIGHT_ENABLED=false`
disables the check. Only use for diagnostics or recovery — running two
battle-tests in parallel will contaminate each other's WAL state.

### Symptom: lock file exists but no process holds it

```
[IntakeRouter] Removed stale lock (dead PID 12345)
```

This is automatic recovery — the dead-PID staleness check (pre-existing,
preserved through Slice 2) reclaims locks held by crashed sessions. No
operator action needed.

### Symptom: lock file exists, PID is alive, but battle-test is wedged

```
[IntakeRouter] Removed wedged-but-alive stale lock (PID=12345 alive,
age=8000s > TTL=7200s — treating as Py_FinalizeEx-class zombie)
```

This is automatic recovery — Slice 2's wedged-TTL check reclaims locks
held by Py_FinalizeEx-deadlocked zombies. No operator action needed in
the new session — but you may want to investigate the wedged process:

```bash
sample <PID> 30 10 > .jarvis/forensics/pid<PID>_sample_$(date +%s).txt
kill -KILL <PID>
```

Slice 1's `BoundedShutdownWatchdog` should have prevented the wedge in
the first place; if you see this in production it's worth filing
forensics for follow-up.

### Symptom: session ran past `--max-wall-seconds` without terminating

This was the S6 pattern (51min on a 40min cap). Slice 1 fixed it via the
thread-side bounded-shutdown watchdog.

If you still see this post-Slice-1, the watchdog itself failed. Check:

* `JARVIS_BATTLE_BOUNDED_SHUTDOWN_ENABLED` (default `true`) — was it
  explicitly set to `false`?
* `JARVIS_BATTLE_SHUTDOWN_DEADLINE_S` (default `30`) — was it set to a
  large value?

If neither was tampered with and the session still hung, capture
forensics and treat as a regression.

### Symptom: session dir has `debug.log` but no `summary.json`

S5/S6 pattern — the SIGTERM-during-steady-state regression. Slice 1's
bounded-shutdown watchdog + the existing `_atexit_fallback_write` path
in `harness.py` should have written a partial summary before `os._exit`
fires.

If this happens post-Slice-1, capture the debug.log tail to forensics
and treat as a regression.

## Forensics directory

When capturing process state for a wedged session, write to
`.jarvis/forensics/`. The directory is gitignored. Standard naming:

```
.jarvis/forensics/pid<PID>_sample_<unix_ts>.txt
.jarvis/forensics/pid<PID>_lsof_<unix_ts>.txt
```

## Live-fire validation pattern

For arc-graduation or post-deferral validation:

1. Sync to main (`git pull --ff-only`).
2. Run preflight (`pgrep` clean, `intake_router.lock` absent or stale).
3. Launch with the standard recipe above.
4. Watch `tail -f .ouroboros/sessions/<session-id>/debug.log` for the
   first signal you're looking for (e.g. `[CancelOrigin]`,
   `[ParallelDispatch]`, etc.).
5. When the signal lands (or fails to land within reasonable time),
   trigger graceful shutdown:
   ```bash
   pgrep -f "python3? scripts/ouroboros_battle_test\.py" | xargs kill -TERM
   ```
   Slice 1's bounded-shutdown watchdog will guarantee `summary.json`
   lands within 30s.
6. Inspect `.ouroboros/sessions/<session-id>/summary.json` for the
   final classification.

## Cross-references

- `memory/project_followup_battle_test_post_summary_hang.md` — 14-incident
  forensic record (Py_FinalizeEx class), 5-item original epic.
- `memory/project_harness_epic_scope.md` — current 4-slice epic scope.
- `memory/project_f1_w3_slice5b_s1_s6_checkpoint.md` — S5/S6 incidents
  that added items 6 + 7 to the epic.
- `CLAUDE.md` — top-level harness behavior summary; references this
  runbook as the operational source of truth.
