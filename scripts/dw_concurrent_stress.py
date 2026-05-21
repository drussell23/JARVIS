#!/usr/bin/env python3
"""DW Concurrent Stress Harness — Hypothesis-#2 / #3 / #4 isolation rig.

What this is
============
A production-faithful asynchronous concurrent stress harness for the
DoubleWord streaming endpoint. Built to definitively narrow the
remaining hypothesis space from §25.2 of the Apr 16 benchmark report
after the May 21 single-request standalone test eliminated
"DW streaming is broken in general."

This script mirrors the production O+V client path *exactly* in the
parts that matter for the SSE stall signature:

  * Same ``aiohttp.ClientSession`` config shape as
    :class:`backend.core.ouroboros.governance.doubleword_provider.DoublewordProvider`
    (connector limit, ttl_dns_cache, persistent session).
  * Same ``await asyncio.wait_for(resp.content.readline(), timeout=...)``
    SSE-stall detection mechanism (see ``doubleword_provider.py:1812``).
  * Same per-chunk timeout default (30s, matching production constant
    ``_PER_CHUNK_TIMEOUT`` at ``doubleword_provider.py:1689``) — overridable
    via env for sweep testing.
  * Same ``StreamRuptureError`` failure mode (imported from production
    ``providers`` module so the failure class is identical).

What it tests
=============
Concurrent agent-scale streaming load against the DW endpoint, with
exhaustive per-stream telemetry:

  * Hypothesis #2 (sustained concurrent-load queueing) — directly
    stress-tested by ``burst`` mode firing N parallel streams.
  * Hypothesis #4 (O+V aiohttp client specifics under concurrent
    sessions) — covered because this script uses the same client shape
    as production.
  * Hypothesis #3 (multi-turn tool-loop SSE framing) — out of scope
    for this harness; needs an orchestrator-driven follow-up.

What it doesn't test
====================
  * The full O+V orchestrator (governance pipeline, gates, FSM).
  * Multi-turn tool-loop SSE framing — that's a separate harness.
  * Failback FSM behavior (cascade to Claude) — Claude isn't called
    here; this is DW-only isolation.

Configuration
=============
All knobs are environment-variable driven — no hardcoded values. The
defaults match the production constants where applicable.

  JARVIS_STRESS_CONCURRENCY              N parallel streams (default 10)
  JARVIS_STRESS_MODEL                    model id (default Qwen 3.5 397B MoE)
  JARVIS_STRESS_MAX_OUTPUT_TOKENS        per-stream max_tokens (default 1500)
  JARVIS_STRESS_PER_CHUNK_TIMEOUT_S      stall threshold (default 30.0, prod-faithful)
  JARVIS_STRESS_PER_REQUEST_TIMEOUT_S    per-stream wall-clock cap (default 180.0)
  JARVIS_STRESS_TEMPERATURE              sampling temperature (default 0.2)
  JARVIS_STRESS_MODE                     burst | ramp (default burst)
  JARVIS_STRESS_RAMP_LEVELS              comma-separated levels for ramp mode
                                          (default "1,2,5,10,15")
  JARVIS_STRESS_CONNECTOR_LIMIT          aiohttp pool size (default = concurrency + 5)
  JARVIS_STRESS_OUTPUT_DIR               JSON archive dir (default $TMPDIR)
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

# Make the repo root importable so we can pull from production modules
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import production StreamRuptureError so the failure class is identical
# to what candidate_generator.py:1097 catches in production.
class _LocalStreamRuptureError(Exception):
    """Fallback class used only when production import fails (diagnostic mode)."""

    def __init__(self, provider: str, elapsed_s: float, bytes_received: int,
                 rupture_timeout_s: float, phase: str) -> None:
        self.provider = provider
        self.elapsed_s = elapsed_s
        self.bytes_received = bytes_received
        self.rupture_timeout_s = rupture_timeout_s
        self.phase = phase
        super().__init__(
            f"stream_rupture provider={provider} phase={phase} "
            f"elapsed={elapsed_s:.1f}s bytes={bytes_received}"
        )


_ProdStreamRuptureError: type[_LocalStreamRuptureError]
try:
    from backend.core.ouroboros.governance.providers import StreamRuptureError as _imported_rupture_cls
    _ProdStreamRuptureError = _imported_rupture_cls  # type: ignore[assignment]  # shape-compatible duck-type
    _STREAM_RUPTURE_SOURCE = "production"
except Exception:  # pragma: no cover — diagnostic fallback only
    _ProdStreamRuptureError = _LocalStreamRuptureError
    _STREAM_RUPTURE_SOURCE = "local_fallback"

import aiohttp  # noqa: E402


# ---------------------------------------------------------------------------
# Env-driven configuration (no hardcoding)
# ---------------------------------------------------------------------------

def _env(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v if v is not None and v != "" else default


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except (ValueError, TypeError):
        return default


CONCURRENCY = _env_int("JARVIS_STRESS_CONCURRENCY", 10)
MODEL = _env("JARVIS_STRESS_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
MAX_OUTPUT_TOKENS = _env_int("JARVIS_STRESS_MAX_OUTPUT_TOKENS", 1500)
PER_CHUNK_TIMEOUT_S = _env_float("JARVIS_STRESS_PER_CHUNK_TIMEOUT_S", 30.0)
PER_REQUEST_TIMEOUT_S = _env_float("JARVIS_STRESS_PER_REQUEST_TIMEOUT_S", 180.0)
TEMPERATURE = _env_float("JARVIS_STRESS_TEMPERATURE", 0.2)
MODE = _env("JARVIS_STRESS_MODE", "burst").lower().strip()
RAMP_LEVELS_RAW = _env("JARVIS_STRESS_RAMP_LEVELS", "1,2,5,10,15")
CONNECTOR_LIMIT = _env_int("JARVIS_STRESS_CONNECTOR_LIMIT", CONCURRENCY + 5)
OUTPUT_DIR = _env("JARVIS_STRESS_OUTPUT_DIR", os.environ.get("TMPDIR", "/tmp"))

DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
if DW_BASE_URL.endswith("/v1/"):
    DW_BASE_URL = DW_BASE_URL[:-1]
elif not DW_BASE_URL.endswith("/v1"):
    DW_BASE_URL = DW_BASE_URL.rstrip("/") + "/v1"


# ---------------------------------------------------------------------------
# Agent-scale prompt fixture (matches Apr 14 isolation-test envelope)
# ---------------------------------------------------------------------------

AGENT_SCALE_PROMPT = """\
You are a senior systems architect. Think step by step through this problem
in detail before responding. Do NOT skip your reasoning — reason carefully.

Problem: Design a deterministic recursion-bounding mechanism for a self-
modifying AI system. The mechanism must guarantee that a sequence of
self-applied code mutations cannot escape a fixed safety envelope, even
under adversarial inputs. Your design must include:

1. A formal definition of the safety envelope and what "escape" means.
2. A bounded-iteration property with a concrete termination proof.
3. An adversarial input class against which the mechanism must hold.
4. A non-trivial example of an attempted escape and how the mechanism
   detects + rejects it.
5. The trade-offs vs. a learned classifier approach.

Take your time. Reason carefully. The answer should be ~800 tokens.
"""


# ---------------------------------------------------------------------------
# Per-stream result dataclass — full telemetry surface
# ---------------------------------------------------------------------------

@dataclass
class StreamResult:
    stream_id: int
    start_time: float
    end_time: float = 0.0
    duration_s: float = 0.0
    ttft_s: float = -1.0          # time to first *content* token (not reasoning)
    ttfr_s: float = -1.0          # time to first reasoning frame (if model emits)
    chunks: int = 0
    bytes_received: int = 0
    content_chunks: int = 0
    reasoning_chunks: int = 0
    inter_chunk_gaps_s: list[float] = field(default_factory=list)
    max_gap_s: float = 0.0
    finish_reason: str = ""
    error_class: str = ""
    error_msg: str = ""
    error_phase: str = ""
    stalled: bool = False
    completed: bool = False
    http_status: int = 0

    def summary(self) -> dict:
        d = asdict(self)
        d.pop("inter_chunk_gaps_s", None)  # large, save separately in archive
        return d


# ---------------------------------------------------------------------------
# Production-faithful streaming client
# ---------------------------------------------------------------------------

async def _stream_one_request(
    session: aiohttp.ClientSession,
    stream_id: int,
    prompt: str,
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
) -> StreamResult:
    """One concurrent stream — mirrors production SSE path in doubleword_provider.py."""
    result = StreamResult(stream_id=stream_id, start_time=time.monotonic())

    headers = {
        "Authorization": f"Bearer {DW_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body = {
        "model": MODEL,
        "stream": True,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    url = f"{DW_BASE_URL}/chat/completions"

    try:
        async with session.post(
            url,
            json=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=per_request_timeout_s, connect=15),
        ) as resp:
            result.http_status = resp.status
            if resp.status != 200:
                result.error_class = "HTTPError"
                result.error_msg = f"status={resp.status}"
                try:
                    err_body = await resp.text()
                    result.error_msg += f" body={err_body[:200]}"
                except Exception:
                    pass
                return result

            last_chunk_time = result.start_time

            while True:
                # PRODUCTION-FAITHFUL stall detection: same primitive as
                # doubleword_provider.py:1812 — asyncio.wait_for around
                # resp.content.readline() with per-chunk timeout.
                try:
                    line = await asyncio.wait_for(
                        resp.content.readline(),
                        timeout=per_chunk_timeout_s,
                    )
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - result.start_time
                    phase = "ttft" if result.chunks == 0 else "inter_chunk"
                    # Construct identical StreamRuptureError as production
                    rupture = _ProdStreamRuptureError(
                        provider="doubleword",
                        elapsed_s=elapsed,
                        bytes_received=result.bytes_received,
                        rupture_timeout_s=per_chunk_timeout_s,
                        phase=phase,
                    )
                    result.error_class = type(rupture).__name__
                    result.error_msg = str(rupture)
                    result.error_phase = phase
                    result.stalled = True
                    return result

                if not line:
                    # EOF reached cleanly
                    break

                now = time.monotonic()
                gap = now - last_chunk_time
                last_chunk_time = now

                line_str = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line_str:
                    # SSE event delimiter — don't count as a "chunk" gap
                    continue

                result.bytes_received += len(line)

                if line_str.startswith(":"):
                    # SSE comment line / keepalive — log as a chunk for gap analysis
                    if result.chunks > 0:
                        result.inter_chunk_gaps_s.append(gap)
                        if gap > result.max_gap_s:
                            result.max_gap_s = gap
                    continue

                if not line_str.startswith("data:"):
                    continue

                payload = line_str[5:].strip()
                if payload == "[DONE]":
                    result.completed = True
                    result.finish_reason = result.finish_reason or "[DONE]"
                    break

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                # Count this as a chunk for gap analysis
                if result.chunks > 0:
                    result.inter_chunk_gaps_s.append(gap)
                    if gap > result.max_gap_s:
                        result.max_gap_s = gap
                result.chunks += 1

                choices = chunk.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {}) or {}
                if delta.get("content"):
                    if result.ttft_s < 0:
                        result.ttft_s = now - result.start_time
                    result.content_chunks += 1
                if delta.get("reasoning"):
                    if result.ttfr_s < 0:
                        result.ttfr_s = now - result.start_time
                    result.reasoning_chunks += 1
                fr = choices[0].get("finish_reason")
                if fr:
                    result.finish_reason = fr

    except asyncio.TimeoutError:
        result.error_class = "TotalRequestTimeout"
        result.error_msg = f"request exceeded {per_request_timeout_s:.0f}s wall-clock"
    except aiohttp.ClientError as e:
        result.error_class = type(e).__name__
        result.error_msg = str(e)[:240]
    except Exception as e:  # noqa: BLE001 — diagnostic harness
        result.error_class = type(e).__name__
        result.error_msg = str(e)[:240]
    finally:
        result.end_time = time.monotonic()
        result.duration_s = result.end_time - result.start_time

    return result


# ---------------------------------------------------------------------------
# Concurrent runner — burst + ramp modes
# ---------------------------------------------------------------------------

async def _run_burst(
    concurrency: int,
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
    connector_limit: int,
) -> list[StreamResult]:
    """Launch N concurrent agent-scale streams in a single burst."""
    # Use ThreadedResolver explicitly so we go through libc getaddrinfo —
    # the same DNS path curl + the production aiohttp session use. The
    # default aiohttp resolver can fall back to aiodns (c-ares), which
    # takes a different code path that some restricted environments block.
    resolver = aiohttp.ThreadedResolver()
    connector = aiohttp.TCPConnector(
        limit=connector_limit,
        ttl_dns_cache=300,
        resolver=resolver,
    )
    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=None, connect=15),
    ) as session:
        tasks = [
            asyncio.create_task(
                _stream_one_request(
                    session, i, AGENT_SCALE_PROMPT,
                    per_chunk_timeout_s, per_request_timeout_s,
                ),
                name=f"stream-{i}",
            )
            for i in range(concurrency)
        ]
        return await asyncio.gather(*tasks)


async def _run_ramp(
    levels: list[int],
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
) -> dict[int, list[StreamResult]]:
    """Ramp mode: run successive bursts at increasing concurrency levels.

    Stops early if a level produces ANY stall — that's the breakpoint.
    """
    by_level: dict[int, list[StreamResult]] = {}
    for level in levels:
        print(f"\n── Ramp: launching {level} concurrent stream(s) ──")
        results = await _run_burst(
            level, per_chunk_timeout_s, per_request_timeout_s,
            connector_limit=level + 5,
        )
        by_level[level] = results
        n_stalled = sum(1 for r in results if r.stalled)
        n_other_err = sum(1 for r in results if r.error_class and not r.stalled)
        n_ok = sum(1 for r in results if r.completed)
        print(f"  → ok={n_ok}/{level}  stalled={n_stalled}  other_err={n_other_err}")
        if n_stalled > 0:
            print(f"  ✗ Stalls detected at concurrency={level} — stopping ramp.")
            break
    return by_level


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------

def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def _aggregate(results: list[StreamResult]) -> dict:
    n = len(results)
    completed = [r for r in results if r.completed]
    stalled = [r for r in results if r.stalled]
    errored = [r for r in results if r.error_class and not r.stalled]

    all_gaps: list[float] = []
    for r in results:
        all_gaps.extend(r.inter_chunk_gaps_s)

    ttft_values = [r.ttft_s for r in results if r.ttft_s > 0]
    ttfr_values = [r.ttfr_s for r in results if r.ttfr_s > 0]
    durations = [r.duration_s for r in results if r.duration_s > 0]

    return {
        "n_streams": n,
        "n_completed": len(completed),
        "n_stalled": len(stalled),
        "n_other_errors": len(errored),
        "success_rate": (len(completed) / n) if n else 0.0,
        "stall_rate": (len(stalled) / n) if n else 0.0,
        "gaps": {
            "count": len(all_gaps),
            "mean_ms": (statistics.mean(all_gaps) * 1000) if all_gaps else 0.0,
            "median_ms": (statistics.median(all_gaps) * 1000) if all_gaps else 0.0,
            "p95_ms": _percentile(all_gaps, 0.95) * 1000,
            "p99_ms": _percentile(all_gaps, 0.99) * 1000,
            "max_ms": (max(all_gaps) * 1000) if all_gaps else 0.0,
            "n_over_1s": sum(1 for g in all_gaps if g > 1),
            "n_over_5s": sum(1 for g in all_gaps if g > 5),
            "n_over_10s": sum(1 for g in all_gaps if g > 10),
            "n_over_30s": sum(1 for g in all_gaps if g > 30),
        },
        "ttft": {
            "count": len(ttft_values),
            "median_s": statistics.median(ttft_values) if ttft_values else 0.0,
            "p95_s": _percentile(ttft_values, 0.95),
            "max_s": max(ttft_values) if ttft_values else 0.0,
        },
        "ttfr": {
            "count": len(ttfr_values),
            "median_s": statistics.median(ttfr_values) if ttfr_values else 0.0,
        },
        "duration": {
            "median_s": statistics.median(durations) if durations else 0.0,
            "p95_s": _percentile(durations, 0.95),
            "max_s": max(durations) if durations else 0.0,
        },
    }


def _verdict(agg: dict) -> tuple[str, str]:
    """Map aggregated stats to a categorical verdict."""
    if agg["n_streams"] == 0:
        return "INDETERMINATE", "no streams ran"

    if agg["stall_rate"] > 0.5:
        return "FAIL", f"{agg['n_stalled']}/{agg['n_streams']} streams stalled — clear breakdown under concurrent load"
    if agg["stall_rate"] > 0:
        return "PARTIAL_FAIL", f"{agg['n_stalled']}/{agg['n_streams']} streams stalled — load-dependent stall reproduced"
    if agg["n_other_errors"] > 0:
        return "DEGRADED", f"{agg['n_other_errors']}/{agg['n_streams']} non-stall errors — see per-stream detail"
    if agg["gaps"]["n_over_10s"] > 0:
        return "CONCERNING", f"{agg['gaps']['n_over_10s']} inter-chunk gaps > 10s — stalls didn't fire, but margins were thin"
    if agg["success_rate"] == 1.0 and agg["gaps"]["max_ms"] < 5000:
        return "CLEAN", "all streams completed cleanly with max gap < 5s — sustained concurrent load survived"
    return "PASS", f"all streams completed; max gap {agg['gaps']['max_ms']:.0f} ms"


def _print_report(agg: dict, results: list[StreamResult], header: str) -> None:
    print()
    print("=" * 78)
    print(header)
    print("=" * 78)

    print(f"  Streams:           {agg['n_streams']}")
    print(f"  Completed:         {agg['n_completed']} ({100*agg['success_rate']:.1f}%)")
    print(f"  Stalled (rupture): {agg['n_stalled']} ({100*agg['stall_rate']:.1f}%)")
    print(f"  Other errors:      {agg['n_other_errors']}")

    print()
    print("  Inter-chunk gap distribution (across all streams):")
    g = agg["gaps"]
    print(f"    count={g['count']}  mean={g['mean_ms']:.1f}ms  median={g['median_ms']:.1f}ms")
    print(f"    p95={g['p95_ms']:.1f}ms  p99={g['p99_ms']:.1f}ms  max={g['max_ms']:.1f}ms")
    print(f"    gaps > 1s:  {g['n_over_1s']}")
    print(f"    gaps > 5s:  {g['n_over_5s']}")
    print(f"    gaps > 10s: {g['n_over_10s']}")
    print(f"    gaps > 30s: {g['n_over_30s']}    ← the production stall threshold")

    print()
    print("  TTFT (time to first content token):")
    t = agg["ttft"]
    print(f"    median={t['median_s']*1000:.0f}ms  p95={t['p95_s']*1000:.0f}ms  max={t['max_s']*1000:.0f}ms")

    print()
    print("  Per-stream duration:")
    d = agg["duration"]
    print(f"    median={d['median_s']:.2f}s  p95={d['p95_s']:.2f}s  max={d['max_s']:.2f}s")

    print()
    print("  Per-stream summary:")
    print(f"    {'#':>3}  {'status':>10}  {'dur':>7}  {'ttft':>7}  {'chunks':>6}  {'max_gap':>8}  finish")
    for r in sorted(results, key=lambda x: x.stream_id):
        status = "STALLED" if r.stalled else ("OK" if r.completed else (r.error_class or "PARTIAL"))
        ttft = f"{r.ttft_s*1000:.0f}ms" if r.ttft_s > 0 else "-"
        dur = f"{r.duration_s:.2f}s"
        gap = f"{r.max_gap_s*1000:.0f}ms"
        print(f"    {r.stream_id:>3}  {status:>10}  {dur:>7}  {ttft:>7}  {r.chunks:>6}  {gap:>8}  {r.finish_reason[:24]}")

    print()
    verdict, why = _verdict(agg)
    print(f"  VERDICT: {verdict}")
    print(f"  WHY:     {why}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _amain() -> int:
    if not DW_API_KEY:
        print("ERROR: DOUBLEWORD_API_KEY not set in environment", file=sys.stderr)
        return 2

    print("=" * 78)
    print("DW Concurrent Stress Harness")
    print("=" * 78)
    print(f"  Endpoint:                  {DW_BASE_URL}/chat/completions")
    print(f"  Model:                     {MODEL}")
    print(f"  Mode:                      {MODE}")
    print(f"  Concurrency:               {CONCURRENCY}")
    print(f"  Per-chunk timeout:         {PER_CHUNK_TIMEOUT_S}s   (prod default: 30.0s)")
    print(f"  Per-request wall-clock:    {PER_REQUEST_TIMEOUT_S}s")
    print(f"  Max output tokens:         {MAX_OUTPUT_TOKENS}")
    print(f"  Temperature:               {TEMPERATURE}")
    print(f"  Connector limit:           {CONNECTOR_LIMIT}")
    print(f"  StreamRuptureError source: {_STREAM_RUPTURE_SOURCE}")
    print()

    overall_start = time.monotonic()

    if MODE == "ramp":
        try:
            levels = sorted({int(x.strip()) for x in RAMP_LEVELS_RAW.split(",") if x.strip()})
        except ValueError:
            levels = [1, 2, 5, 10, 15]
        print(f"  Ramp levels: {levels}")
        by_level = await _run_ramp(levels, PER_CHUNK_TIMEOUT_S, PER_REQUEST_TIMEOUT_S)
        all_results: list[StreamResult] = []
        for lvl, rs in by_level.items():
            agg = _aggregate(rs)
            _print_report(agg, rs, f"Ramp level: concurrency = {lvl}")
            all_results.extend(rs)
        agg_all = _aggregate(all_results)
        _print_report(agg_all, all_results, "Ramp aggregate (all levels)")
        primary_results = all_results
        primary_agg = agg_all
        levels_summary = {lvl: _aggregate(rs) for lvl, rs in by_level.items()}
    else:
        results = await _run_burst(
            CONCURRENCY, PER_CHUNK_TIMEOUT_S, PER_REQUEST_TIMEOUT_S, CONNECTOR_LIMIT,
        )
        agg = _aggregate(results)
        _print_report(agg, results, f"Burst: concurrency = {CONCURRENCY}")
        primary_results = results
        primary_agg = agg
        levels_summary = {CONCURRENCY: agg}

    overall_dur = time.monotonic() - overall_start
    print()
    print(f"  Total harness wall-clock: {overall_dur:.2f}s")

    # JSON archive
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"dw_concurrent_stress_{ts}.json"
    archive = {
        "harness": "dw_concurrent_stress.py",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": MODE,
        "endpoint": f"{DW_BASE_URL}/chat/completions",
        "model": MODEL,
        "concurrency": CONCURRENCY,
        "per_chunk_timeout_s": PER_CHUNK_TIMEOUT_S,
        "per_request_timeout_s": PER_REQUEST_TIMEOUT_S,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "stream_rupture_source": _STREAM_RUPTURE_SOURCE,
        "overall_wall_clock_s": overall_dur,
        "aggregate": primary_agg,
        "levels_aggregate": levels_summary,
        "per_stream": [r.summary() for r in primary_results],
        "per_stream_gaps": {
            r.stream_id: r.inter_chunk_gaps_s for r in primary_results
        },
    }
    with open(out_path, "w") as f:
        json.dump(archive, f, indent=2, default=str)
    print(f"  JSON archive:             {out_path}")

    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\n[interrupted]", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
