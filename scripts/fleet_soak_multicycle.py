"""Deterministic multi-cycle EWMA-stability soak for the FleetEvaluator.

Runs the REAL FleetEvaluator + FleetCalibrationStore + fleet_rerank across N
mock operational cycles. Only the model_caller is mocked — with a controlled
schedule that injects a transient latency spike (one cycle) and a transient
502 (one cycle) so we can *prove* the EWMA absorbs them without the routing
rank flapping. No network, no secrets, no cost — reproducible in CI.

Profiles (steady-state valid_tok_per_s = tok/s x ast_pass_rate):
  fast-valid (gpt-oss-120b-like)  tps 120, valid code, clean label  -> vtps ~120
  mid-valid  (deepseek-like)      tps  80, valid code, clean label  -> vtps  ~80
  slow-valid (gemma-like)         tps  16, valid code, clean label  -> vtps  ~16
  reasoner   (qwen-397b-like)     tps  66, NO code block, prose lbl -> vtps   ~0

Injected perturbations:
  cycle 6: fast-valid latency SPIKE -> raw tps 40 for that cycle only.
           (Without EWMA: 40 < mid's 80 => rank flip. With EWMA(0.4): the
            blended value stays above mid => NO flip. This is the proof.)
  cycle 8: mid-valid transient 502 (probe ok=False) -> ast dips one cycle.

Asserts, over the steady-state window (cycle >= WARMUP):
  * the #1 code-route slot never flaps (always fast-valid),
  * the last slot is always the reasoner,
  * no top-slot flip on the spike/502 cycles.
Exit 1 (CI red) on any flap. Writes a markdown report to argv[1] (or stdout).

Run: PYTHONPATH=. python3 scripts/fleet_soak_multicycle.py [report.md]
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

# Force the master switch on for the soak (the evaluator gates probes on it).
os.environ["JARVIS_FLEET_EVALUATOR_ENABLED"] = "true"
os.environ.setdefault(
    "JARVIS_FLEET_CALIBRATION_PATH",
    os.path.join(tempfile.gettempdir(), "fleet_soak_store.json"),
)
os.environ.setdefault("JARVIS_FLEET_EWMA_ALPHA", "0.4")

from backend.core.ouroboros.governance.fleet_evaluator import (  # noqa: E402
    FleetEvaluator,
    ProbeResult,
)
from backend.core.ouroboros.governance import fleet_calibration_store as s  # noqa: E402

CYCLES = 12
WARMUP = 4  # ranks asserted stable from this cycle onward
SPIKE_CYCLE = 6
FIVEOHTWO_CYCLE = 8

VALID_CODE = (
    "```python\n"
    "def merge_intervals(intervals):\n"
    "    '''Merge overlapping intervals.'''\n"
    "    if not intervals:\n"
    "        return []\n"
    "    out = [intervals[0]]\n"
    "    for a, b in intervals[1:]:\n"
    "        if a <= out[-1][1]:\n"
    "            out[-1][1] = max(out[-1][1], b)\n"
    "        else:\n"
    "            out.append([a, b])\n"
    "    return out\n"
    "```"
)
PROSE = "Let me reason about intervals. First, consider the sorting step..."

# model_id -> (base_tps, emits_valid_code, clean_label)
PROFILES = {
    "gpt-oss-120b": (120.0, True, True),
    "DeepSeek-V4-Flash": (80.0, True, True),
    "gemma-4-31B": (16.0, True, True),
    "Qwen3.5-397B": (66.0, False, False),
}
MODELS = list(PROFILES)
_state = {"cycle": 0}


def _make_caller():
    async def caller(model_id, messages, *, max_tokens):
        cycle = _state["cycle"]
        base_tps, valid_code, clean_label = PROFILES[model_id]
        is_code = "code block" in messages[-1]["content"].lower()

        # Transient 502 on mid-valid (affects whichever probe lands this cycle).
        if model_id == "DeepSeek-V4-Flash" and cycle == FIVEOHTWO_CYCLE:
            return ProbeResult("", 0.0, 0.0, 0, False, "http_502")

        tps = base_tps
        # Latency spike on the fast coder's CODEGEN probe for one cycle: the
        # raw value (40) is BELOW the mid coder's 80 — without smoothing this
        # would flip the #1 rank. EWMA must absorb it (proves the property).
        if model_id == "gpt-oss-120b" and cycle == SPIKE_CYCLE and is_code:
            tps = 40.0

        # Encode target tps as completion_tokens over a 1000ms window so the
        # evaluator's tok_per_s = completion_tokens / (total_ms/1000) == tps.
        completion_tokens = int(tps)
        total_ms = 1000.0
        if is_code:
            text = VALID_CODE if valid_code else PROSE
        else:
            text = "ENRICH" if clean_label else PROSE
        return ProbeResult(
            text=text, ttft_ms=total_ms, total_ms=total_ms,
            completion_tokens=completion_tokens, ok=True, error="",
        )

    return caller


_SPARK = "▁▂▃▄▅▆▇█"


def _sparkline(vals):
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return _SPARK[len(_SPARK) // 2] * len(vals)
    out = []
    for v in vals:
        idx = int((v - lo) / (hi - lo) * (len(_SPARK) - 1))
        out.append(_SPARK[idx])
    return "".join(out)


async def main() -> int:
    # Fresh store each run (deterministic).
    try:
        os.unlink(os.environ["JARVIS_FLEET_CALIBRATION_PATH"])
    except OSError:
        pass
    store = s.FleetCalibrationStore()
    ev = FleetEvaluator(
        model_caller=_make_caller(), store=store,
        idle_check=lambda: True, clock=lambda: float(_state["cycle"]),
        default_model="Qwen3.5-397B",
    )

    rank_history = []     # per-cycle code-route order
    vtps_history = {m: [] for m in MODELS}
    top_slot = []

    for c in range(1, CYCLES + 1):
        _state["cycle"] = c
        await ev.calibrate_models(MODELS)
        scores = store.all_scores()
        order = list(s.fleet_rerank("standard", tuple(MODELS), scores, route_kind="code"))
        rank_history.append((c, order))
        top_slot.append(order[0])
        for m in MODELS:
            sc = scores.get(m)
            vtps_history[m].append(s.valid_tok_per_s(sc) if sc else 0.0)

    # ---- assertions (steady-state stability) ----
    steady = [(c, o) for (c, o) in rank_history if c >= WARMUP]
    flaps = []
    expected_top = "gpt-oss-120b"
    expected_last = "Qwen3.5-397B"
    for c, o in steady:
        if o[0] != expected_top:
            flaps.append(f"cycle {c}: top slot was {o[0]} (expected {expected_top})")
        if o[-1] != expected_last:
            flaps.append(f"cycle {c}: last slot was {o[-1]} (expected {expected_last})")

    # ---- report ----
    lines = []
    lines.append("## 🧬 Fleet Evaluator — multi-cycle EWMA-stability soak")
    lines.append("")
    lines.append(
        f"Deterministic clean-room soak on `ubuntu-latest` — **{CYCLES} cycles**, "
        f"EWMA α={s._alpha()}, real `FleetEvaluator`/store/`fleet_rerank`, mocked "
        "caller with an injected codegen-probe latency **spike** (cycle "
        f"{SPIKE_CYCLE}: fast coder raw throughput → 40, *below* the mid coder's "
        f"80 — would flip the rank unsmoothed) and a transient **502** "
        f"(cycle {FIVEOHTWO_CYCLE}, mid coder)."
    )
    lines.append("")
    lines.append("### valid_tok_per_s convergence (EWMA-smoothed)")
    lines.append("")
    lines.append("| model | trace | final |")
    lines.append("|---|---|---|")
    for m in MODELS:
        vh = vtps_history[m]
        lines.append(f"| `{m}` | `{_sparkline(vh)}` | {vh[-1]:.1f} |")
    lines.append("")
    lines.append("### per-cycle code-route rank order")
    lines.append("")
    lines.append("| cycle | #1 | #2 | #3 | #4 | note |")
    lines.append("|---|---|---|---|---|---|")
    for c, o in rank_history:
        note = ""
        if c == SPIKE_CYCLE:
            note = "⚡ fast-coder latency spike"
        elif c == FIVEOHTWO_CYCLE:
            note = "🔌 mid-coder 502"
        lines.append(f"| {c} | {o[0]} | {o[1]} | {o[2]} | {o[3]} | {note} |")
    lines.append("")
    settled = rank_history[-1][1]
    lines.append(f"**Settled routing array (code route):** `{settled}`")
    lines.append("")
    if flaps:
        lines.append("### ❌ VERDICT: RANK FLAP DETECTED")
        for f in flaps:
            lines.append(f"- {f}")
    else:
        lines.append(
            "### ✅ VERDICT: STABLE — no top/last-slot flap across the "
            f"steady-state window (cycles {WARMUP}–{CYCLES}), including the "
            "spike and 502 cycles. EWMA absorbed both transients; the routing "
            "rank did not flap."
        )

    report = "\n".join(lines)
    if len(sys.argv) > 1:
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
    print(report)
    return 1 if flaps else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
