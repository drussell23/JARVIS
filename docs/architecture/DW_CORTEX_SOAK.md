# DW Predictive-Cortex Soak — Runbook (Slices 168–176)

A DW-only soak to validate the predictive cortex on **live DoubleWord traffic**. Claude is
disabled; the organism runs on DW alone and the cortex accumulates its intelligence from
real DW failures.

## What this validates (and what it doesn't)

| Validated | How |
|---|---|
| Per-model rupture rings fill from real DW failures (Slice 175) | cortex logs + monitor |
| Multi-signal fusion: transport / economic / upstream / cancel events weighted (176) | `[Cortex]` logs |
| Self-calibration: thresholds move toward the right boundary from observed FP/FN (174) | persisted `.jarvis` files |
| The organism survives **indefinitely on DW alone** (restart:always) | `docker ps` uptime |

> **Honest scope note.** With Claude disabled, Slice 36 already routes standard/complex ops
> to batch, so the cortex's *preemptive reroute* is moot (batch either way) — but its
> **learning** runs fully and is observable. The reroute's *economic* payoff (avoided Claude
> cascades, Slice 171's `💸` counter) is only meaningful with a **funded, available Claude**
> for the cortex to route *around* — deliberately out of scope for a DW-only test.

## Launch

```bash
# 1. Funded DW key in .env (DOUBLEWORD_API_KEY=...). NO Claude key needed (disabled).
# 2. One command — builds the oracle image, launches detached (survives reboot), follows the cortex:
./scripts/launch_dw_cortex_soak.sh
```

Detached + `restart: always` → survives this terminal, a crash, and a host reboot (if
`dockerd` starts on boot). Decoupled from any agent event loop.

## Observe

```bash
# the cortex reasoning, live:
docker compose -f docker-compose.dw-cortex-soak.yml logs -f | grep -E "Cortex|reroute|live_transport"

# what it has LEARNED (persisted per-model thresholds + recent activity):
./scripts/dw_cortex_monitor.sh
```

Log lines to watch for:
- `[Cortex] forecast preempt: model=… rupture-risk≥threshold → DW-batch` — the forecast fired.
- `[Cortex] calibrate model=… threshold→0.xx (FP=… FN=… brier=…)` — it tuned its boundary.
- `IMMEDIATE reroute → DW` / `live_transport` — the underlying DW failure signals it learns from.

**Live forecast %** (the `🔮 forecast` field) is best seen on the Discord spine: set
`JARVIS_DISCORD_GATEWAY_ENABLED=1` + `DISCORD_BOT_TOKEN` in `.env` and uncomment the gateway
lines in the compose. (The rupture rings are in-process, so the instantaneous % isn't on
disk; the *calibrated thresholds* are.)

## Success criteria

After the soak has seen enough DW traffic:
1. `dw_threshold_calibration_<model>.json` files appear in `.jarvis/` with per-model
   thresholds that have **moved off the 0.70 baseline** (the cortex learned).
2. `[Cortex] calibrate` lines show the Brier score and FP/FN counts evolving.
3. The container shows multi-hour/day uptime — the organism is self-sufficient on DW.

## Tuning knobs (env, override in the compose)

| Env | Default | Effect |
|---|---|---|
| `JARVIS_DW_RUPTURE_RISK_THRESHOLD` | `0.7` | initial baseline (174 self-tunes from here) |
| `JARVIS_DW_RUPTURE_HORIZON_S` | `300` | forecast window |
| `JARVIS_DW_CALIBRATION_STEP` | `0.02` | per-FP/FN threshold nudge (raise to accelerate learning) |
| `JARVIS_DW_SIGNAL_WEIGHT_ECONOMIC` | `2.0` | weight of 402/429 quota events |
| `JARVIS_DW_SIGNAL_WEIGHT_UPSTREAM` | `0.4` | weight of empty/5xx/parse events |

## Stop

```bash
docker compose -f docker-compose.dw-cortex-soak.yml down
```

The `.jarvis/` calibration state persists — the next launch resumes from the learned
thresholds (no amnesia).
