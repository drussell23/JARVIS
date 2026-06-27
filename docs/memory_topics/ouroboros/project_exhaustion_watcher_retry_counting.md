---
title: Project Exhaustion Watcher Retry Counting
modules: []
status: historical
source: project_exhaustion_watcher_retry_counting.md
---

**Status:** FIXED in commit `37a371e65d` (2026-04-15). Per-op dedup lands via
`record_exhaustion(op_id=...)`. Session P reproduction test confirms 2 distinct
ops producing 3 raw events keeps consecutive at 2 and does NOT hibernate.

**Why:** A single chronic unresolvable op (or one transient API flake spanning
2 ops × 2 attempts each) could trip the process-global consecutive_exhaustion
threshold in ~4 minutes, hibernating the organism even when the reflex path
was healthy.

**How to apply:** When investigating unexpected hibernation, check the snapshot
for `deduped_events` and `unique_ops_counted`. If `consecutive_exhaustion=N`
reports with `unique_ops_counted<N` something is wrong with the callers passing
op_id. If `unique_ops_counted==N` the ops are genuinely distinct and hibernation
is correctly firing.

---

### Original diagnosis (kept for history)

Discovered in bt-2026-04-15-024041 while verifying the github_issue cooldown fix.
The fix worked correctly (write hook fired, disk persistence verified via cold-load
probe against the real file), but the session still hibernated with
`consecutive_exhaustion=3`. Root cause was not the cooldown — it was a separate
structural issue in how `ProviderExhaustionWatcher.record_exhaustion` counted.

**The pitfall:** `ProviderExhaustionWatcher` had a single global counter
(`self._consecutive: int`, `self._threshold: int = 3` by default). Every call
to `record_exhaustion` incremented the counter regardless of whether the event
came from:
  * A fresh sensor emission → fresh op
  * A CandidateGenerator internal retry of an existing op (IMMEDIATE → IMMEDIATE)
  * An IMMEDIATE → STANDARD route demotion of an existing op
  * A different op from a different source entirely

In bt-2026-04-15-024041, `op-019d8f05-c17d-...-cau` (github_issue #16501) hit
`_raise_exhausted` three times within ~4 minutes:

```
event_n=1  19:46:42  op-019d8f05  IMMEDIATE   CancelledError (first try)
event_n=2  19:48:39  op-019d8f05  IMMEDIATE   TimeoutError   (IMMEDIATE retry)
event_n=3  19:50:24  op-019d8f05  STANDARD    CancelledError (IMMEDIATE→STANDARD demoted retry)
```

Three events, one op, one source — counter went 1 → 2 → 3 → hibernation.

Session P (bt-2026-04-15-192504) was a second incarnation of the same structural
bug: transient Claude flake, probe op's 1 retry plus runtime_health op's 2
attempts = 3 events / 2 distinct ops → hibernation.

**The fix (shipped):** `record_exhaustion(op_id=...)` dedupes within the current
consecutive run. Same op never counts twice. Different ops still count
independently. Dedup set cleared on `record_success()` and `reset()`.
`snapshot()` exposes `deduped_events` and `unique_ops_counted` for
observability. FIFO-ish eviction at `_MAX_COUNTED_OPS=256` prevents
unbounded growth.

Callers without op_id (legacy wire) preserve old behavior.
