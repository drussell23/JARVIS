---
title: Project Github Issue Cooldown
modules: []
status: historical
source: project_github_issue_cooldown.md
---

Landed 2026-04-14 as three sequential commits in response to bt-2026-04-15
exhaustion-by-chronic-noise pattern.

**What it does:** When a `CandidateGenerator.generate()` op exhausts providers
and its `signal_source == "github_issue"`, the hook parses the op's
`context.description` to recover the sensor's own dedup_key
(`"{short_repo}:{issue_number}"`), then calls
`register_issue_exhaustion(key, reason)` to stamp a wall-clock deadline
into `.jarvis/github_issue_cooldowns.json`. On the next scan cycle (same or
future process), `GitHubIssueSensor.scan_once` consults
`_issue_cooldown_active(dedup_key)` BEFORE the session `_seen_issues` check
and suppresses emission if the key is still in cooldown.

**Env gates:**
- `JARVIS_GITHUB_ISSUE_EXHAUSTION_COOLDOWN_S` — default 900 (15 min). Set to
  0/negative to disable the registry entirely.
- `JARVIS_GITHUB_ISSUE_COOLDOWN_PATH` — override the on-disk path. Default is
  `{JARVIS_REPO_PATH or '.'}/.jarvis/github_issue_cooldowns.json`.

**Commits:**
- `3a9fcc1aa9` — scaffold (sensor-side registry + env gate + emit-loop skip)
- `83b210210f` — CandidateGenerator hook + `issue_key_from_description` parser
  + 19 pytest cases
- `163d2e7bee` — disk persistence via atomic tempfile→fsync→rename, wall-clock
  expiries (not monotonic), 8 more pytest cases (TestDiskPersistence) for 27
  total

**Why wall-clock:** Monotonic timers reset across process restart, making
persisted values garbage after reboot. Wall-clock (`time.time()`) is
susceptible to NTP skew within a process but for a 15-min cooldown the worst
case is one extra suppression or one extra emission — bounded and
non-destructive.

**What it does NOT fix:** The `ExhaustionWatcher` retry-counting pitfall
(see `project_exhaustion_watcher_retry_counting.md`). The cooldown prevents
the SENSOR from re-emitting a chronic issue in a future scan/session, but it
does nothing to cap retries of a single op within one `CandidateGenerator.generate()`
call. bt-2026-04-15-024041 verified the cooldown write hook fires and the
disk persistence works (cold-load probe against real file), but still
hibernated because `op-019d8f05` retried 3 times within 4 minutes and
`ProviderExhaustionWatcher._consecutive` incremented blindly.

**How to apply:** If a future battle test hibernates on a github_issue op,
check `.jarvis/github_issue_cooldowns.json` first — if the key is present
with a future expires_at, the cooldown did its job and the hibernation
came from a different path (most likely the retry-counting pitfall). Also
check that the env gate is not set to 0.
