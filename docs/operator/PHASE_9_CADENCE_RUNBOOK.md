# Phase 9 Cadence Runbook

**Purpose**: Install + verify the Live-Fire Graduation Soak schedule on macOS without falling into the silent-cadence-failure trap that bit cron #1 on 2026-05-06.

**Audience**: Operator running JARVIS on macOS (Darwin). Linux/cron operators can use the legacy cron path; the structural caveat below still applies.

---

## TL;DR — recommended path

```bash
bash scripts/install_live_fire_soak_cron.sh --launchd
```

Then verify:

```bash
launchctl list | grep com.jarvis.live-fire-soak
python3 scripts/live_fire_graduation_soak.py status
```

The `status` output now includes a **Cadence status** block (Slice 3) showing verdict + interval + ages of last preflight/history rows + next expected fire. If anything is off you see it immediately.

---

## Why launchd, not cron, on macOS

Cron is legacy on macOS and runs in a more restricted **TCC (Transparency, Consent, Control)** posture than user LaunchAgents. On 2026-05-06, cron #1 fired exactly on schedule — the log file mtime proves it — but macOS denied execution before the harness's Python interpreter even started:

```
$ cat .jarvis/live_fire_soak_logs/20260506-000001.log
/Applications/Xcode.app/.../python3: can't open file '...': [Errno 1] Operation not permitted
```

This is the canonical macOS gotcha. Two important details:

1. **The denied subject is often `python3` or the script path, not `/usr/sbin/cron` itself.** Adding cron to Full Disk Access is a global policy relaxation for a system daemon and **may not even fix it** — TCC's sandbox is per-process, and the chain of execve()'d processes inherits the parent's sandbox. The right fix targets the actual subject.

2. **User LaunchAgents run in a different (less restricted) execution context.** They inherit the user's logged-in session capabilities, including `~/Documents` access without explicit grants. This is the supported macOS path for recurring user-owned work.

The launchd installer we ship reuses the **same `run_live_fire_graduation_soak.sh` wrapper** as the cron path, so the env block is single-source-of-truth — there's no duplication or drift between the two paths.

---

## What the cadence substrate actually does (Slices 1–4)

Phase 9's cadence is now observable end-to-end:

| Slice | Surface | Purpose |
|---|---|---|
| **1** | `.jarvis/cadence_manifest.json` | Single source of truth for schedule kind + interval. Installer writes it at `--install` / `--launchd` time; detector reads it. **No magic numbers** in detection modules (AST-pinned). |
| **2** | `.jarvis/cadence_health.jsonl` + `scripts/cadence_preflight.py` | Pre-invocation capability probe. Records a structured row (`preflight_ok` or `preflight_failure` with `errno`, `errno_name`, `subject`) **before** the harness imports anything. Closes the EPERM-before-Python gap. |
| **3** | `cadence_status.evaluate_cadence_status()` | Pure-function overdue detector. 5-value verdict: `HEALTHY` / `OVERDUE` / `RECENTLY_FAILED` / `NEVER_RAN` / `UNKNOWN`. Composes manifest + health + history. Wires into existing `live_fire_graduation_soak.py status` subcommand. |
| **4** | `~/Library/LaunchAgents/com.jarvis.live-fire-soak.plist` | macOS-canonical scheduler. Plist invokes the same wrapper as cron; `StartInterval` derived from `CRON_SCHEDULE` via the manifest parser. |

Both schedulers funnel through the same `run_live_fire_graduation_soak.sh` wrapper, which:

1. Invokes `cadence_preflight.py` (records ok-or-failure row to `cadence_health.jsonl`)
2. Aborts if preflight fails (no harness invocation on EPERM)
3. Otherwise execs the harness with the canonical Phase 9 env block

---

## Choosing a cadence

`CRON_SCHEDULE` is the env knob — same source for both cron and launchd:

```bash
# 12-hour cadence (recommended for first 6-9 weeks of P9 graduation)
CRON_SCHEDULE='0 */12 * * *' bash scripts/install_live_fire_soak_cron.sh --launchd

# 8-hour cadence (default; faster graduation but ~3× the API spend)
bash scripts/install_live_fire_soak_cron.sh --launchd

# 3 fixed times per day
CRON_SCHEDULE='0 6,14,22 * * *' bash scripts/install_live_fire_soak_cron.sh --launchd
```

The manifest's `interval_hint_s` is derived from the **worst-case consecutive gap** in 24h (so `0 6,14,22 * * *` resolves to 8h, not 24h). The overdue detector uses `interval_hint_s × grace_factor` (default 1.5) as the OVERDUE threshold.

---

## Verifying it's actually firing

```bash
# Shows graduation queue + cadence status in one go
python3 scripts/live_fire_graduation_soak.py status
```

The Cadence status block tells you in one read:

- `HEALTHY` — last successful fire within `grace_window_s`. Cadence is observable + working.
- `OVERDUE` — no successful fire in the grace window. Either the schedule is paused, the daemon was unloaded, or the wrapper aborted.
- `RECENTLY_FAILED` — preflight rows show recent failures newer than any success. Typical TCC EPERM pattern. Inspect `.jarvis/cadence_health.jsonl` for the `errno_name` and `subject`.
- `NEVER_RAN` — manifest exists but no fires recorded yet. Just-installed state; should flip after the next interval.
- `UNKNOWN` — manifest missing or schedule unparseable. Re-run `--launchd` or `--install`.

```bash
# Tail recent preflight rows directly
tail -20 .jarvis/cadence_health.jsonl | python3 -m json.tool

# See the manifest
cat .jarvis/cadence_manifest.json | python3 -m json.tool
```

---

## When TCC denies anyway

If after `--launchd` install you still see `preflight_failure` rows with `failure_class=os_policy` and `errno_name=EPERM`:

1. **Check the launchd plist's path access**: `launchctl print gui/$(id -u)/com.jarvis.live-fire-soak` (newer macOS) or `launchctl list com.jarvis.live-fire-soak`. The "ExitStatus" field tells you the last exit code.

2. **If launchd-loaded but still denied**: macOS may need explicit Full Disk Access for the user's shell or for `bash`/`python3` specifically. The path is **System Settings → Privacy & Security → Full Disk Access**, but **the actual binary you grant matters**:
   - Grant `/bin/bash` (the wrapper's interpreter) — most likely
   - OR grant the system `python3` your installer uses (less common; user-installed Pythons are usually fine)
   - **Granting `/usr/sbin/cron`** only helps if you stayed on the cron path; it's not relevant to launchd.

3. **Hot-revert and retry**:
   ```bash
   bash scripts/install_live_fire_soak_cron.sh --remove-launchd
   # ... fix TCC ...
   bash scripts/install_live_fire_soak_cron.sh --launchd
   ```

The `cadence_health.jsonl` rows survive across install/uninstall, so you can grep for what was failing pre-fix:

```bash
jq -c 'select(.kind=="preflight_failure")' .jarvis/cadence_health.jsonl
```

---

## Cron path (legacy / Linux operators)

```bash
bash scripts/install_live_fire_soak_cron.sh --install
bash scripts/install_live_fire_soak_cron.sh --status
bash scripts/install_live_fire_soak_cron.sh --remove
```

The cron entry now `&&`-chains the preflight invocation before the harness, so a TCC denial on macOS still records a structured row instead of failing silently:

```
0 */8 * * * cd <repo> && JARVIS_CADENCE_KIND=cron python3 .../cadence_preflight.py --cadence-kind cron && <env block> python3 .../live_fire_graduation_soak.py run ...
```

If the preflight fails, the `&&` chain short-circuits — the harness never runs, and the failure row in `cadence_health.jsonl` tells you exactly which subject was denied.

---

## First proof — manual one-shot

Both paths support a manual one-shot soak that bypasses the schedule:

```bash
bash scripts/install_live_fire_soak_cron.sh --once
```

This invokes the wrapper directly (preflight + harness), useful as a first-proof run after install. The output goes to your terminal, not to a log file.

---

## Hot-revert checklist

Anything goes wrong:

```bash
# Stop the schedule
bash scripts/install_live_fire_soak_cron.sh --remove-launchd
bash scripts/install_live_fire_soak_cron.sh --remove

# Pause without uninstalling
export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true

# Inspect the manifest + health ledger
cat .jarvis/cadence_manifest.json | python3 -m json.tool
tail -50 .jarvis/cadence_health.jsonl

# Verify the substrate still parses
python3 -c "from backend.core.ouroboros.governance.graduation.cadence_status import evaluate_cadence_status, render_cadence_status_block; print(render_cadence_status_block(evaluate_cadence_status()))"
```

---

## Architectural locks (AST-pinned)

For reviewers / auditors:

- **Single source of cadence-string truth** — `CRON_SCHEDULE` env knob drives both cron + launchd installers. Manifest is the only on-disk knower of the derived `interval_hint_s`.
- **No magic seconds in detection** — `cadence_status.py` is AST-pinned to forbid hardcoded cadence-second integer literals (28800, 43200, 86400, etc.) outside the rendering helper. The detector reads from the manifest exclusively.
- **Wrapper is the env-block carrier** — both cron entry and launchd plist invoke `run_live_fire_graduation_soak.sh`; the 4 Phase 9 env vars (`JARVIS_GRADUATION_LEDGER_ENABLED` / `JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED` / `JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT` / `JARVIS_DW_TOPOLOGY_EARLY_REJECT_ENABLED`) export from the wrapper, not from the schedule entry. AST-pinned in `test_install_live_fire_soak_cron.py`.
- **Substrate authority asymmetry** — `cadence_manifest.py`, `cadence_health.py`, `cadence_status.py` all forbid imports of orchestrator / iron_gate / policy / providers / candidate_generator / urgency_router / change_engine / semantic_guardian. AST-pinned per module.
- **§33.4 canonical flock** — `cadence_health.jsonl` appends compose `cross_process_jsonl.flock_append_line`; no parallel locking impl (AST-pinned).
- **§33.5 versioned-artifact contracts** — `CadenceManifest`, `CadenceHealthRow`, `CadenceStatusReport` all carry `schema_version` + symmetric `to_dict`/`from_dict` (AST-pinned per artifact).

---

## File map

```
scripts/
├── install_live_fire_soak_cron.sh       # cron + launchd installer (this doc)
├── run_live_fire_graduation_soak.sh     # canonical wrapper (env block)
├── cadence_preflight.py                 # Slice 2 capability probe
└── live_fire_graduation_soak.py         # CLI surface

backend/core/ouroboros/governance/graduation/
├── cadence_manifest.py                  # Slice 1 — manifest substrate
├── cadence_health.py                    # Slice 2 — health ledger
└── cadence_status.py                    # Slice 3 — overdue detector

.jarvis/
├── cadence_manifest.json                # written at install time
├── cadence_health.jsonl                 # append-only preflight rows
├── live_fire_graduation_history.jsonl   # append-only soak rows
├── graduation_ledger.jsonl              # append-only flag-graduation rows
└── live_fire_soak_logs/                 # per-fire stdout/stderr
    ├── launchd.stdout.log
    ├── launchd.stderr.log
    └── YYYYMMDD-HHMMSS.log              # cron path per-fire logs
```
