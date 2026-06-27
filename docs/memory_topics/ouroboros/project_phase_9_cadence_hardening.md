---
title: Each soak takes ~40min wall-clock. Run repeatedly:
modules: [backend/core/ouroboros/governance/graduation/live_fire_soak.py, scripts/live_fire_graduation_soak.py, tests/governance/test_phase_9_cadence_hardening.py, scripts/run_live_fire_graduation_soak.sh]
status: historical
source: project_phase_9_cadence_hardening.md
---

Phase 9 substrate is operator-paced (3+ clean soaks × 24 flags = days of wall-clock). Pre-flight audit (Explore agent) found one CRITICAL bug + 3 hardening gaps that would surface ONLY when the operator actually runs cadence — discovering them after committing wall-clock would waste days.

**Why this exists:** the operator asked to "resolve Phase 9 cadence first" — interpreted correctly as "harden the substrate so when I commit wall-clock time, it works first-try." Latent blockers found post-soak are catastrophic.

**How to apply:** when an operator-paced cadence is about to start, audit the substrate end-to-end via an Explore agent FIRST. Look for: (1) wall-clock dependencies that fail under NTP skew / DST; (2) missing operator-facing aggregation surfaces that would force tedious manual scanning; (3) graceful-degradation paths that silently swallow critical errors.

## Critical bug fixed: wall-clock session-detection race

`backend/core/ouroboros/governance/graduation/live_fire_soak.py:_run_battle_test_subprocess` previously called:

```python
proc = subprocess.run(...)
summary, debug_tail = _read_most_recent_session(
    sessions_root, after_epoch=time.time() - timeout_s - 60,
)
```

The `after_epoch` was derived from `time.time()` AFTER subprocess returned. **Forward NTP skew** (NTP correction, manual clock adjust, DST forward-jump) during the subprocess execution moves `time.time()` such that `after_epoch` becomes a future timestamp greater than the session mtime that subprocess wrote. Filter drops the session, summary returns empty dict, harness classifies SUMMARY_PARSE_FAILED, ledger never updates that soak's evidence. Soak wasted.

**Fix:** capture `start_wall_anchor = time.time()` BEFORE `subprocess.run`. The captured anchor is an immutable reference point — forward skew during execution cannot move it. Backward skew is absorbed by `_SESSION_DETECTION_GRACE_S = 60.0` (module-level constant). Pinned via AST regression that bans the `time.time() - timeout_s` post-derivation pattern from reappearing.

## Defense-in-depth: mtime-sort instead of lexicographic-name-sort

`_read_most_recent_session` previously did `sorted(sessions_root.iterdir(), reverse=True)` — relies on naming convention `bt-YYYY-MM-DD-HHMMSS` happening to sort the same as mtime. Robust by accident, not by construction. Fixed to sort by `st_mtime` descending — matches the actual semantic and survives any future naming-convention drift.

## Operator-UX: `ready` CLI subcommand

Added `python3 scripts/live_fire_graduation_soak.py ready` — composes the existing `GraduationLedger.eligible_flags()` primitive. Answers "which flags are ready to flip RIGHT NOW?" in one command. Without it, operator scans `queue` output × 24 flags × ~36 soaks of cadence runs (~30s/glance × 36 = ~18min wasted cognitive load). Empty-eligible case prints accumulate-more-evidence guidance with the wrapper script command — no confusing silence.

## Test surface

`tests/governance/test_phase_9_cadence_hardening.py` — 16 tests:
- 5 source-AST regression pins (banned-pattern detector for the wall-clock derivation; `_SESSION_DETECTION_GRACE_S` constant present; anchor captured BEFORE subprocess.run; `after_epoch = start_wall_anchor - _SESSION_DETECTION_GRACE_S` shape; mtime-sort uses `st_mtime`)
- 6 behavioral pins on mtime-sort robustness (lexicographic-vs-mtime divergence test proves the fix isn't a no-op; missing-root + empty-root + non-dir-entries + crashed-session-no-summary defensive paths)
- 5 CLI shape pins (handler exists + wired into handlers dict + subparser registered + empty-ledger smoke + populated-ledger smoke)

236/236 across full Phase 9 + Move 7 + Move 8 + closure + Wave 3 hygiene sweep.

## Operator runbook (post-hardening)

```bash
# Each soak takes ~40min wall-clock. Run repeatedly:
bash scripts/run_live_fire_graduation_soak.sh

# After 3+ soaks, check what's ready:
python3 scripts/live_fire_graduation_soak.py ready

# When a flag shows up: flip it default-true in flag_registry_seed.py + helper, commit, repeat.
```

The substrate is now graded production-ready for cadence. Operator can commit wall-clock confidently.
