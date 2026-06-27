---
title: Project Rubric Soak Profile
modules: []
status: historical
source: project_rubric_soak_profile.md
---

**Phase R2 controlled rubric soak** — run a SWE-bench discriminator
instance to a real `ScoreOutcome` (RESOLVED/UNRESOLVED) without the
noise that masked prior runs. All knobs are pre-existing (no new
framework, no hardcoding). One instance at a time.

Env bundle (prefix the ratified battle-test command):
```
JARVIS_SWE_BENCH_PRO_ENABLED=true
JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED=true
JARVIS_SWE_BENCH_PRO_LOCAL_DATASET_PATH=.jarvis/swe_bench_pro/discriminator.jsonl
JARVIS_SWE_BENCH_PRO_INJECT_INSTANCE_IDS=<one id>      # psf first, then django
JARVIS_SWE_BENCH_PRO_ENVELOPE_URGENCY=high             # Trace-2 knob: dequeue ahead of sensors
JARVIS_SWE_BENCH_PRO_AUTOSCORE_ENABLED=true            # §33.1 default-FALSE — REQUIRED for ANY ScoreOutcome. Was OFF in every prior soak → that (not just the wall) is why 0 rubric lines ever appeared. harness_inject.py:78/147; `if autoscore_enabled():` gate at :440.
JARVIS_GOVERNED_MAX_CONCURRENT_OPS=1                    # default 2 → serial
JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED=true                # scales the R1-floored _gen_timeout
JARVIS_SENSOR_GOVERNOR_ENABLED=true
JARVIS_SENSOR_GOVERNOR_GLOBAL_CAP_PER_HOUR=5           # throttle the ~46-op sensor flood
```
Ratified caps: `--cost-cap 2.00 --idle-timeout 600 --max-wall-seconds 7200 --headless -v`.
(`2400` is calibrated for the ~850–1300s synthetic happy-path, NOT a
real SWE-bench GENERATE(~4min)+VALIDATE+RETRY×2+L2+autoscore cycle —
use ≥5400, 7200 preferred. Known risk: `--cost-cap 2.00` may become
the limiter on a ~2h run before autoscore; if `stop_reason=op_cost_*`
/ budget before a ScoreOutcome, that is the documented new limiter —
do NOT silently raise cost; report it.)
**No kill-wrapper** — run to natural conclusion (we WANT ScoreOutcome).

**Verdict source:** the session `debug.log` under
`.ouroboros/sessions/bt-<ts>/` — NOT the battle-test stdout (stdout is
Rich UI; `logger.info` ScoreOutcome/RESOLVED lines only land in
debug.log; this cost a wasted forensic cycle once). Grep the swe_bench
op's causal_id for `ScoreOutcome` / `RESOLVED` / `UNRESOLVED` /
`autoscore`.

**Pipeline-feed prerequisites (all merged + soak-validated):**
op-isolation (distinct ops), Trace-2 (urgency=normal default),
Trace-1 (classify_runner parity → COMPLEX route — origin/main
fc11cb44dc), R1 (outer/inner timeout coherence — origin/main
ea52b72569: shared `gen_call_likely_thinking()`/
`fallback_thinking_cap_s()` so outer `_gen_timeout` >= inner 360s
thinking cap by construction). The rubric gate for Tasks #7/#8.
See [[no-pre-result-euphoria]].
