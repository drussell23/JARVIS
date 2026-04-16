# DoubleWord benchmark scripts

Staging for the 2026-04-16 benchmark deliverable to Meryem Arik (DoubleWord CEO). Two bounded diagnostics, run in order A → B.

**Nothing in this directory ever executes network calls automatically. You run the scripts; the scripts read secrets from environment variables; secrets never touch disk or git.**

---

## Pre-flight checklist

Before running either script:

```bash
# Required for both A and B:
export DOUBLEWORD_API_KEY='...'       # never commit

# Required for B only (Claude cascade on non-overridden routes):
export ANTHROPIC_API_KEY='...'        # never commit

# Verify the keys are in your env but NOT in any tracked file:
env | grep -E '(DOUBLEWORD|ANTHROPIC)_API_KEY' | cut -d= -f1
git grep -i -E 'DOUBLEWORD_API_KEY[^S]|ANTHROPIC_API_KEY[^S]' || echo "No keys in repo — ok"
```

Python 3.9+ required. `httpx` is used by script A (`pip3 install httpx` if missing). The battle-test harness (script B) pulls its own dependencies from the repo's `requirements.txt`.

---

## A — Direct API smoke test (`dw_sse_smoke.py`)

**Purpose:** Isolate stream vs. non-stream on *identical payload*, same model, same generation params. Gives the clearest possible diagnostic signal for DoubleWord's gateway team.

**Run time:** ~2 minutes worst case (two back-to-back requests, each bounded by a 180s wall timeout and a 30s no-data stall detector).

**Cost:** ~$0.01–0.05 depending on output length.

### Qwen 3.5 397B (matches `bt-2026-04-14-203740` STANDARD isolation)

```bash
python3 scripts/benchmarks/dw_sse_smoke.py \
    --model "Qwen/Qwen3.5-397B-A17B-FP8" \
    --label qwen397b_standard
```

### Gemma 4 31B (matches `bt-2026-04-14-182446` BACKGROUND isolation)

```bash
python3 scripts/benchmarks/dw_sse_smoke.py \
    --model "google/gemma-4-31B-it" \
    --label gemma31b_background
```

### Outputs

Written to `.ouroboros/benchmarks/dw_sse_smoke_<label>_<UTC timestamp>.json`. Human summary printed to stdout.

### Exit codes (interpretable from CI / shell)

| Exit | Meaning | What it tells Meryem |
|---|---|---|
| 0 | Both stream + non-stream completed | Endpoint is healthy today. SSE issue may be intermittent. |
| **1** | **Stream stalled, non-stream succeeded** | **Isolates the blocker to SSE transport — matches Apr 14.** This is the gold-signal outcome for the benchmark report. |
| 2 | Both failed | Endpoint-level issue, broader than streaming |
| 3 | Non-stream failed, stream succeeded | Unusual — retry |
| 4 | Configuration error | Check env vars |

### Recommended sequence for the benchmark report

Run **both** labels (Qwen 397B first, Gemma 31B second) to reproduce both Apr 14 isolation tests on a single dated run. Copy both JSON outputs into the doc as evidence:

```bash
python3 scripts/benchmarks/dw_sse_smoke.py --model "Qwen/Qwen3.5-397B-A17B-FP8" --label qwen397b_standard
python3 scripts/benchmarks/dw_sse_smoke.py --model "google/gemma-4-31B-it" --label gemma31b_background
ls -la .ouroboros/benchmarks/dw_sse_smoke_*.json
```

---

## B — Full battle-test Gemma BG repro (`run_gemma_bg_repro.sh`)

**Purpose:** Dated full-harness reproduction of `bt-2026-04-14-182446`. Exercises the whole governance stack (router, cost governor, failback FSM, exhaustion watcher) under the exact conditions that triggered the topology seal on 2026-04-14.

**Run time:** Up to 5 minutes (default `IDLE_TIMEOUT=300`). Stops early on cost cap.

**Cost cap (default):** $0.30 — low enough to stop fast, high enough to generate a handful of op attempts.

### Run

```bash
bash scripts/benchmarks/run_gemma_bg_repro.sh
```

### Override knobs

```bash
COST_CAP=0.50 IDLE_TIMEOUT=600 bash scripts/benchmarks/run_gemma_bg_repro.sh
```

### Expected outcome

Session writes to `.ouroboros/sessions/bt-<timestamp>/`. Look for these lines in `debug.log`:

```
[DoublewordProvider] WARNING SSE stream stalled (no data for 30s)
[Orchestrator] BACKGROUND route: DW failed (background_dw_error:RuntimeError:...)
```

If you see these, the Apr 14 signature is reproduced today. The session's `summary.json` should show:
- `stop_reason: idle_timeout` or `budget_exhausted`
- `cost_breakdown.doubleword = $0` (no successful completions)
- `strategic_drift.total_ops > 0`, `drifted_ops = 0 or 1` (ok)

Copy the new session ID into `docs/benchmarks/DW_BENCHMARKS_2026-04-16.md` §3.1 / "Apr 16 addendum".

---

## What to do after both scripts have run

1. **Confirm the findings:** cat the `dw_sse_smoke_*.json` outputs and the new session's `debug.log`.
2. **Ask Claude to append the Apr 16 addendum** to `docs/benchmarks/DW_BENCHMARKS_2026-04-16.md`. Paste the two script outputs + new session ID. A one-paragraph addendum is enough.
3. **Regenerate HTML + PDF:**

   ```bash
   cd docs/benchmarks
   pandoc DW_BENCHMARKS_2026-04-16.md -o DW_BENCHMARKS_2026-04-16.html --from=gfm --to=html5 --standalone --metadata title="DoubleWord × Ouroboros + Venom — Battle-Test Benchmarks"
   TMPDIR_CHROME="${TMPDIR:-/tmp}/chrome-headless-$$"
   mkdir -p "$TMPDIR_CHROME"
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu --user-data-dir="$TMPDIR_CHROME" --no-pdf-header-footer --print-to-pdf=DW_BENCHMARKS_2026-04-16.pdf "file://$(pwd)/DW_BENCHMARKS_2026-04-16.html"
   rm -rf "$TMPDIR_CHROME"
   ```

4. **Review tone/numbers once more**, then send via the `DELIVERY_EMAIL_AND_LINKEDIN.md` drafts.

---

## Security invariants (read before every run)

1. **Never commit** `DOUBLEWORD_API_KEY` or `ANTHROPIC_API_KEY` to this repo. Both scripts read from env only.
2. **Never pass keys as CLI args.** They'd show up in shell history and `ps` output. Env vars are the only safe path.
3. **Never paste API responses** that include authentication data. DW responses contain only model output + usage counts — safe to share.
4. **Outputs go to `.ouroboros/benchmarks/`** which is diagnostic telemetry, not source. If you want a specific run's output to accompany the benchmark report, copy the JSON into `docs/benchmarks/artifacts/` (explicit move, not automatic).
5. **If you cancel a run mid-stream**, check that the session directory doesn't have any partial `debug.log` with stale credentials leaked via stack traces. (Unlikely — httpx doesn't log Authorization headers — but worth a grep.)

---

**Created:** 2026-04-16 as part of the DW benchmark deliverable.
**Scope:** Diagnostic only. Not part of the normal battle-test or governance pipeline.
