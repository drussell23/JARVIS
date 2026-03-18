#!/usr/bin/env python3
"""
Voice.ai Persistent WebSocket Benchmark
=========================================
Tests TTFB with a SINGLE persistent WebSocket connection, sending multiple
requests without reconnecting. This isolates true generation latency from
connection setup overhead — matching how Trinity would use it in production.

Usage:
  export VOICEAI_API_KEY="vk_..."
  python3 benchmarks/voiceai_persistent_ws_benchmark.py [--rounds 20]
"""

import asyncio
import argparse
import base64
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import websockets


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VOICEAI_WS_ENDPOINT = "wss://dev.voice.ai/api/v1/tts/multi-stream"

TEST_SENTENCES = {
    "short": "Hello Derek, welcome back.",
    "medium": (
        "The voice authentication system is initializing. "
        "Please stand by for biometric verification."
    ),
    "long": (
        "Good morning, Derek. I've completed the overnight analysis of "
        "the Trinity ecosystem. All systems are operating within normal "
        "parameters. JARVIS Prime responded in forty-three milliseconds, "
        "and the Reactive Core handled twelve thousand events without any "
        "failures. Would you like a detailed breakdown of the performance "
        "metrics?"
    ),
}


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class WsResult:
    sentence_class: str
    round_num: int
    ttfb_ms: float
    total_ms: float
    audio_bytes: int
    chunks_received: int
    text_length: int
    success: bool
    error: Optional[str] = None


def percentile(data: list, p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


# ---------------------------------------------------------------------------
# Persistent WebSocket benchmark
# ---------------------------------------------------------------------------

async def run_persistent_ws_benchmark(
    api_key: str,
    rounds: int,
    warmup: int,
) -> dict:
    """
    Open ONE WebSocket connection, then send multiple TTS requests through it.
    This eliminates connection setup overhead and measures pure generation TTFB.
    """
    all_results: list[WsResult] = []
    ws_url = VOICEAI_WS_ENDPOINT

    print(f"\n  Connecting to {ws_url} ...")
    t_connect_start = time.perf_counter()

    async with websockets.connect(
        ws_url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ping_interval=30,
        ping_timeout=15,
        close_timeout=10,
        max_size=10 * 1024 * 1024,  # 10MB max message
    ) as ws:
        t_connect_end = time.perf_counter()
        connect_ms = (t_connect_end - t_connect_start) * 1000
        print(f"  Connected! (handshake: {connect_ms:.1f}ms)")
        print(f"  Connection will be reused for ALL {rounds} rounds x "
              f"{len(TEST_SENTENCES)} sentences\n")

        for sc, text in TEST_SENTENCES.items():
            print(f"{'='*65}")
            if len(text) > 60:
                print(f"  [{sc}] \"{text[:60]}...\"")
            else:
                print(f"  [{sc}] \"{text}\"")
            print(f"{'='*65}")

            # Warmup — generous delay to let server release contexts
            if warmup > 0:
                print(f"  Warming up ({warmup} rounds)...", end="", flush=True)
                for _ in range(warmup):
                    await _send_and_receive(ws, text)
                    await asyncio.sleep(1.5)  # let server release context
                    print(".", end="", flush=True)
                print(" done")

            # Benchmark rounds (with retry on concurrency error)
            for i in range(1, rounds + 1):
                max_retries = 3
                for attempt in range(max_retries):
                    result = await _timed_send_receive(ws, text, sc, i)
                    if result.success:
                        break
                    if "concurrent" in (result.error or "").lower():
                        wait = 2.0 * (attempt + 1)
                        print(f"  Round {i:2d}/{rounds}: "
                              f"concurrency limit — retrying in {wait}s...")
                        await asyncio.sleep(wait)
                    else:
                        break  # non-retryable error

                all_results.append(result)

                status = "OK" if result.success else f"FAIL: {result.error}"
                print(
                    f"  Round {i:2d}/{rounds}: "
                    f"TTFB={result.ttfb_ms:7.1f}ms  "
                    f"Total={result.total_ms:7.1f}ms  "
                    f"Chunks={result.chunks_received:3d}  "
                    f"Audio={result.audio_bytes:6d}B  "
                    f"[{status}]"
                )

                # Wait for server to release context before next request
                await asyncio.sleep(1.0)

            # Print per-sentence summary
            sc_results = [r for r in all_results
                          if r.sentence_class == sc and r.success]
            if sc_results:
                ttfbs = [r.ttfb_ms for r in sc_results]
                totals = [r.total_ms for r in sc_results]
                print(f"\n  --- {sc} Summary (persistent WS) ---")
                print(
                    f"  TTFB:  mean={statistics.mean(ttfbs):.1f}ms  "
                    f"median={statistics.median(ttfbs):.1f}ms  "
                    f"P95={percentile(ttfbs, 95):.1f}ms  "
                    f"min={min(ttfbs):.1f}ms  "
                    f"max={max(ttfbs):.1f}ms  "
                    f"std={statistics.stdev(ttfbs):.1f}ms"
                    if len(ttfbs) > 1 else
                    f"  TTFB:  {ttfbs[0]:.1f}ms"
                )
                print(
                    f"  Total: mean={statistics.mean(totals):.1f}ms  "
                    f"median={statistics.median(totals):.1f}ms  "
                    f"P95={percentile(totals, 95):.1f}ms"
                )
            print()
            # Extra pause between sentence classes
            await asyncio.sleep(2.0)

        # Graceful close
        try:
            await ws.send(json.dumps({"close_socket": True}))
        except Exception:
            pass

    return {
        "connection_ms": round(connect_ms, 2),
        "results": [asdict(r) for r in all_results],
    }


async def _send_and_receive(ws, text: str) -> list:
    """Send text and collect all audio chunks (no timing)."""
    await ws.send(json.dumps({
        "text": text,
        "language": "en",
        "flush": True,
    }))
    chunks = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
        except asyncio.TimeoutError:
            break
        data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
        if "audio" in data:
            chunks.append(base64.b64decode(data["audio"]))
        if data.get("is_last", False):
            break
    return chunks


async def _timed_send_receive(
    ws, text: str, sentence_class: str, round_num: int
) -> WsResult:
    """Send text, time TTFB and total, collect audio."""
    t_start = time.perf_counter()
    ttfb_ms = 0.0
    audio_chunks = []
    first_chunk = True
    chunk_count = 0

    try:
        await ws.send(json.dumps({
            "text": text,
            "language": "en",
            "flush": True,
        }))

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
            except asyncio.TimeoutError:
                break

            if isinstance(raw, str):
                data = json.loads(raw)
            else:
                data = json.loads(raw.decode())

            if "error" in data:
                t_end = time.perf_counter()
                return WsResult(
                    sentence_class=sentence_class,
                    round_num=round_num,
                    ttfb_ms=round((t_end - t_start) * 1000, 2),
                    total_ms=round((t_end - t_start) * 1000, 2),
                    audio_bytes=0,
                    chunks_received=chunk_count,
                    text_length=len(text),
                    success=False,
                    error=str(data["error"]),
                )

            if "audio" in data:
                audio_data = base64.b64decode(data["audio"])
                chunk_count += 1
                if first_chunk and audio_data:
                    ttfb_ms = (time.perf_counter() - t_start) * 1000
                    first_chunk = False
                audio_chunks.append(audio_data)

            if data.get("is_last", False):
                break

        t_end = time.perf_counter()
        total_bytes = sum(len(c) for c in audio_chunks)

        return WsResult(
            sentence_class=sentence_class,
            round_num=round_num,
            ttfb_ms=round(ttfb_ms, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=total_bytes,
            chunks_received=chunk_count,
            text_length=len(text),
            success=True,
        )
    except Exception as e:
        t_end = time.perf_counter()
        return WsResult(
            sentence_class=sentence_class,
            round_num=round_num,
            ttfb_ms=round(ttfb_ms or (t_end - t_start) * 1000, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=0,
            chunks_received=chunk_count,
            text_length=len(text),
            success=False,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Comparison with previous results
# ---------------------------------------------------------------------------

def print_final_report(data: dict) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 75)
    lines.append("  PERSISTENT WEBSOCKET BENCHMARK RESULTS")
    lines.append(f"  Connection handshake: {data['connection_ms']:.1f}ms (one-time)")
    lines.append("=" * 75)

    results = data["results"]
    successes = [r for r in results if r["success"]]

    lines.append(
        f"\n  {'Sentence':<10} {'TTFB Mean':>10} {'TTFB Med':>10} "
        f"{'TTFB P95':>10} {'TTFB Min':>10} {'Std Dev':>8} "
        f"{'Total':>10}"
    )
    lines.append("-" * 75)

    for sc in ["short", "medium", "long"]:
        sc_res = [r for r in successes if r["sentence_class"] == sc]
        if not sc_res:
            continue
        ttfbs = [r["ttfb_ms"] for r in sc_res]
        totals = [r["total_ms"] for r in sc_res]
        std = statistics.stdev(ttfbs) if len(ttfbs) > 1 else 0.0
        lines.append(
            f"  {sc:<10} {statistics.mean(ttfbs):>8.1f}ms "
            f"{statistics.median(ttfbs):>8.1f}ms "
            f"{percentile(ttfbs, 95):>8.1f}ms "
            f"{min(ttfbs):>8.1f}ms "
            f"{std:>6.1f}ms "
            f"{statistics.mean(totals):>8.1f}ms"
        )

    lines.append("-" * 75)

    # Compare to macOS Daniel from previous run
    lines.append("\n  Comparison to macOS Daniel (from previous benchmark):")
    lines.append("  macOS Daniel TTFB:  short=2451ms  medium=6656ms  long=3953ms")

    med_results = [r for r in successes if r["sentence_class"] == "medium"]
    if med_results:
        med_ttfb = statistics.mean([r["ttfb_ms"] for r in med_results])
        speedup = 6656 / med_ttfb if med_ttfb > 0 else 0
        lines.append(
            f"  Voice.ai persistent WS (medium): {med_ttfb:.1f}ms "
            f"({speedup:.1f}x faster)"
        )

    short_results = [r for r in successes if r["sentence_class"] == "short"]
    if short_results:
        short_ttfb = statistics.mean([r["ttfb_ms"] for r in short_results])
        speedup = 2451 / short_ttfb if short_ttfb > 0 else 0
        lines.append(
            f"  Voice.ai persistent WS (short):  {short_ttfb:.1f}ms "
            f"({speedup:.1f}x faster)"
        )

    lines.append("")
    lines.append("  200ms threshold for conversational AI: ", )

    all_ttfbs = [r["ttfb_ms"] for r in successes]
    if all_ttfbs:
        under_200 = sum(1 for t in all_ttfbs if t < 200)
        under_400 = sum(1 for t in all_ttfbs if t < 400)
        lines.append(
            f"    Under 200ms: {under_200}/{len(all_ttfbs)} "
            f"({under_200/len(all_ttfbs)*100:.0f}%)"
        )
        lines.append(
            f"    Under 400ms: {under_400}/{len(all_ttfbs)} "
            f"({under_400/len(all_ttfbs)*100:.0f}%)"
        )
    else:
        lines.append("    No successful results to analyze.")

    output = "\n".join(lines)
    print(output)
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Voice.ai Persistent WebSocket Benchmark"
    )
    parser.add_argument(
        "--rounds", type=int, default=15,
        help="Rounds per sentence class (default: 15)",
    )
    parser.add_argument(
        "--warmup", type=int, default=3,
        help="Warmup rounds per sentence (default: 3)",
    )
    args = parser.parse_args()

    api_key = os.environ.get("VOICEAI_API_KEY")
    if not api_key:
        print("ERROR: Set VOICEAI_API_KEY environment variable")
        sys.exit(1)

    print("=" * 65)
    print("  Voice.ai PERSISTENT WebSocket Benchmark")
    print(f"  Rounds: {args.rounds} per sentence  |  Warmup: {args.warmup}")
    print("  Single connection reused for all requests")
    print("  This measures TRUE generation TTFB (no connection overhead)")
    print("=" * 65)

    data = await run_persistent_ws_benchmark(api_key, args.rounds, args.warmup)

    report = print_final_report(data)

    # Save
    output_dir = Path(__file__).parent
    ts = time.strftime("%Y%m%dT%H%M%S")
    json_path = str(output_dir / f"voiceai_persistent_ws_{ts}.json")
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2)

    md_path = json_path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write("# Voice.ai Persistent WebSocket Benchmark\n\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Rounds: {args.rounds} | Warmup: {args.warmup}\n\n")
        f.write("```\n")
        f.write(report)
        f.write("\n```\n")

    print(f"\n  Results: {json_path}")
    print(f"  Report:  {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
