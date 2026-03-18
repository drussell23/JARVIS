#!/usr/bin/env python3
"""
Voice.ai TTS Latency Benchmark
===============================
Compares Voice.ai API (HTTP + WebSocket) against macOS Daniel TTS (local baseline).

Metrics captured:
  - TTFB (time-to-first-byte): how quickly audio starts streaming
  - Total generation time: full synthesis + delivery
  - Audio size: bytes received
  - Statistical aggregates: mean, median, P95, P99, std dev

Usage:
  export VOICEAI_API_KEY="vk_..."
  python3 benchmarks/voiceai_tts_benchmark.py [--rounds 20] [--warmup 3]

Requirements:
  pip install websockets aiohttp
"""

import asyncio
import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VOICEAI_BASE_URL = "https://dev.voice.ai"
VOICEAI_HTTP_ENDPOINT = f"{VOICEAI_BASE_URL}/api/v1/tts/speech/stream"
VOICEAI_HTTP_SYNC_ENDPOINT = f"{VOICEAI_BASE_URL}/api/v1/tts/speech"
VOICEAI_WS_ENDPOINT = "wss://dev.voice.ai/api/v1/tts/multi-stream"

# Test sentences — varying lengths to capture behavior across input sizes
TEST_SENTENCES = {
    "short": "Hello Derek, welcome back.",
    "medium": "The voice authentication system is initializing. Please stand by for biometric verification.",
    "long": (
        "Good morning, Derek. I've completed the overnight analysis of the Trinity ecosystem. "
        "All systems are operating within normal parameters. JARVIS Prime responded in forty-three "
        "milliseconds, and the Reactive Core handled twelve thousand events without any failures. "
        "Would you like a detailed breakdown of the performance metrics?"
    ),
}

MACOS_VOICE = "Daniel"
MACOS_RATE = 175  # WPM — matches JARVIS safe_say() default


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LatencyResult:
    provider: str
    protocol: str
    sentence_class: str
    ttfb_ms: float
    total_ms: float
    audio_bytes: int
    text_length: int
    success: bool
    error: Optional[str] = None


@dataclass
class BenchmarkSummary:
    provider: str
    protocol: str
    sentence_class: str
    rounds: int
    ttfb_mean_ms: float
    ttfb_median_ms: float
    ttfb_p95_ms: float
    ttfb_p99_ms: float
    ttfb_std_ms: float
    ttfb_min_ms: float
    ttfb_max_ms: float
    total_mean_ms: float
    total_median_ms: float
    total_p95_ms: float
    avg_audio_bytes: float
    success_rate: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def percentile(data: list, p: float) -> float:
    """Calculate p-th percentile (0-100)."""
    if not data:
        return 0.0
    sorted_data = sorted(data)
    k = (len(sorted_data) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(sorted_data):
        return sorted_data[-1]
    return sorted_data[f] + (k - f) * (sorted_data[c] - sorted_data[f])


def summarize(results: list) -> Optional[BenchmarkSummary]:
    """Aggregate individual results into a statistical summary."""
    successes = [r for r in results if r.success]
    if not successes:
        return None

    ttfbs = [r.ttfb_ms for r in successes]
    totals = [r.total_ms for r in successes]
    sizes = [r.audio_bytes for r in successes]

    return BenchmarkSummary(
        provider=results[0].provider,
        protocol=results[0].protocol,
        sentence_class=results[0].sentence_class,
        rounds=len(results),
        ttfb_mean_ms=round(statistics.mean(ttfbs), 2),
        ttfb_median_ms=round(statistics.median(ttfbs), 2),
        ttfb_p95_ms=round(percentile(ttfbs, 95), 2),
        ttfb_p99_ms=round(percentile(ttfbs, 99), 2),
        ttfb_std_ms=round(statistics.stdev(ttfbs), 2) if len(ttfbs) > 1 else 0.0,
        ttfb_min_ms=round(min(ttfbs), 2),
        ttfb_max_ms=round(max(ttfbs), 2),
        total_mean_ms=round(statistics.mean(totals), 2),
        total_median_ms=round(statistics.median(totals), 2),
        total_p95_ms=round(percentile(totals, 95), 2),
        avg_audio_bytes=round(statistics.mean(sizes), 0),
        success_rate=round(len(successes) / len(results) * 100, 1),
    )


# ---------------------------------------------------------------------------
# Benchmark: macOS Daniel (local baseline)
# ---------------------------------------------------------------------------

async def bench_macos_daniel(text: str, sentence_class: str) -> LatencyResult:
    """Benchmark macOS say command — mirrors JARVIS safe_say() pipeline."""
    t_start = time.perf_counter()

    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        # Step 1: Synthesis — say -v Daniel -r 175 -o tempfile
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", MACOS_VOICE, "-r", str(MACOS_RATE), "-o", tmp_path, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        t_synth = time.perf_counter()

        audio_bytes = Path(tmp_path).stat().st_size

        # TTFB for local = synthesis time (audio file is ready)
        ttfb_ms = (t_synth - t_start) * 1000

        # Step 2: Playback time — afplay (what the user actually hears)
        proc2 = await asyncio.create_subprocess_exec(
            "afplay", tmp_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc2.wait()
        t_end = time.perf_counter()

        total_ms = (t_end - t_start) * 1000

        return LatencyResult(
            provider="macOS_Daniel",
            protocol="local",
            sentence_class=sentence_class,
            ttfb_ms=round(ttfb_ms, 2),
            total_ms=round(total_ms, 2),
            audio_bytes=audio_bytes,
            text_length=len(text),
            success=True,
        )
    except Exception as e:
        t_end = time.perf_counter()
        return LatencyResult(
            provider="macOS_Daniel",
            protocol="local",
            sentence_class=sentence_class,
            ttfb_ms=round((t_end - t_start) * 1000, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=0,
            text_length=len(text),
            success=False,
            error=str(e),
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Benchmark: Voice.ai HTTP Streaming
# ---------------------------------------------------------------------------

async def bench_voiceai_http(
    text: str, sentence_class: str, api_key: str
) -> LatencyResult:
    """Benchmark Voice.ai HTTP streaming endpoint."""
    import aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "audio_format": "mp3",
        "language": "en",
    }

    t_start = time.perf_counter()
    ttfb_ms = 0.0
    audio_chunks = []
    first_chunk = True

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                VOICEAI_HTTP_ENDPOINT, headers=headers, json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    t_end = time.perf_counter()
                    return LatencyResult(
                        provider="Voice.ai",
                        protocol="http_stream",
                        sentence_class=sentence_class,
                        ttfb_ms=round((t_end - t_start) * 1000, 2),
                        total_ms=round((t_end - t_start) * 1000, 2),
                        audio_bytes=0,
                        text_length=len(text),
                        success=False,
                        error=f"HTTP {resp.status}: {body[:200]}",
                    )

                async for chunk in resp.content.iter_any():
                    if first_chunk and chunk:
                        ttfb_ms = (time.perf_counter() - t_start) * 1000
                        first_chunk = False
                    audio_chunks.append(chunk)

        t_end = time.perf_counter()
        total_bytes = sum(len(c) for c in audio_chunks)

        return LatencyResult(
            provider="Voice.ai",
            protocol="http_stream",
            sentence_class=sentence_class,
            ttfb_ms=round(ttfb_ms, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=total_bytes,
            text_length=len(text),
            success=True,
        )
    except Exception as e:
        t_end = time.perf_counter()
        return LatencyResult(
            provider="Voice.ai",
            protocol="http_stream",
            sentence_class=sentence_class,
            ttfb_ms=round(ttfb_ms or (t_end - t_start) * 1000, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=0,
            text_length=len(text),
            success=False,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Benchmark: Voice.ai HTTP Sync (non-streaming)
# ---------------------------------------------------------------------------

async def bench_voiceai_http_sync(
    text: str, sentence_class: str, api_key: str
) -> LatencyResult:
    """Benchmark Voice.ai synchronous (non-streaming) HTTP endpoint."""
    import aiohttp

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "audio_format": "mp3",
        "language": "en",
    }

    t_start = time.perf_counter()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                VOICEAI_HTTP_SYNC_ENDPOINT, headers=headers, json=payload
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    t_end = time.perf_counter()
                    return LatencyResult(
                        provider="Voice.ai",
                        protocol="http_sync",
                        sentence_class=sentence_class,
                        ttfb_ms=round((t_end - t_start) * 1000, 2),
                        total_ms=round((t_end - t_start) * 1000, 2),
                        audio_bytes=0,
                        text_length=len(text),
                        success=False,
                        error=f"HTTP {resp.status}: {body[:200]}",
                    )

                body = await resp.read()
                t_end = time.perf_counter()
                elapsed_ms = (t_end - t_start) * 1000

                return LatencyResult(
                    provider="Voice.ai",
                    protocol="http_sync",
                    sentence_class=sentence_class,
                    ttfb_ms=round(elapsed_ms, 2),
                    total_ms=round(elapsed_ms, 2),
                    audio_bytes=len(body),
                    text_length=len(text),
                    success=True,
                )
    except Exception as e:
        t_end = time.perf_counter()
        return LatencyResult(
            provider="Voice.ai",
            protocol="http_sync",
            sentence_class=sentence_class,
            ttfb_ms=round((t_end - t_start) * 1000, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=0,
            text_length=len(text),
            success=False,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Benchmark: Voice.ai WebSocket
# ---------------------------------------------------------------------------

async def bench_voiceai_ws(
    text: str, sentence_class: str, api_key: str
) -> LatencyResult:
    """Benchmark Voice.ai WebSocket streaming endpoint (fastest path)."""
    import websockets
    import base64

    ws_url = VOICEAI_WS_ENDPOINT

    t_start = time.perf_counter()
    ttfb_ms = 0.0
    audio_chunks = []
    first_chunk = True

    try:
        async with websockets.connect(
            ws_url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as ws:
            # Send TTS request
            msg = json.dumps({
                "text": text,
                "language": "en",
                "flush": True,
            })
            await ws.send(msg)

            # Receive audio chunks
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
                    return LatencyResult(
                        provider="Voice.ai",
                        protocol="websocket",
                        sentence_class=sentence_class,
                        ttfb_ms=round((t_end - t_start) * 1000, 2),
                        total_ms=round((t_end - t_start) * 1000, 2),
                        audio_bytes=0,
                        text_length=len(text),
                        success=False,
                        error=data["error"],
                    )

                if "audio" in data:
                    audio_bytes_data = base64.b64decode(data["audio"])
                    if first_chunk and audio_bytes_data:
                        ttfb_ms = (time.perf_counter() - t_start) * 1000
                        first_chunk = False
                    audio_chunks.append(audio_bytes_data)

                if data.get("is_last", False):
                    break

            # Gracefully close
            try:
                await ws.send(json.dumps({"close_socket": True}))
            except Exception:
                pass

        t_end = time.perf_counter()
        total_bytes = sum(len(c) for c in audio_chunks)

        return LatencyResult(
            provider="Voice.ai",
            protocol="websocket",
            sentence_class=sentence_class,
            ttfb_ms=round(ttfb_ms, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=total_bytes,
            text_length=len(text),
            success=True,
        )
    except Exception as e:
        t_end = time.perf_counter()
        return LatencyResult(
            provider="Voice.ai",
            protocol="websocket",
            sentence_class=sentence_class,
            ttfb_ms=round(ttfb_ms or (t_end - t_start) * 1000, 2),
            total_ms=round((t_end - t_start) * 1000, 2),
            audio_bytes=0,
            text_length=len(text),
            success=False,
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_benchmark(rounds: int, warmup: int, api_key: str) -> dict:
    """Run the full benchmark suite."""
    all_results = []
    summaries = []

    providers = [
        ("macOS_Daniel", "local", bench_macos_daniel),
        ("Voice.ai", "http_stream", bench_voiceai_http),
        ("Voice.ai", "http_sync", bench_voiceai_http_sync),
        ("Voice.ai", "websocket", bench_voiceai_ws),
    ]

    for provider_name, protocol, bench_fn in providers:
        for sc, text in TEST_SENTENCES.items():
            print(f"\n{'='*60}")
            print(f"  {provider_name} ({protocol}) -- {sc} sentence")
            if len(text) > 60:
                print(f"  Text: \"{text[:60]}...\"")
            else:
                print(f"  Text: \"{text}\"")
            print(f"{'='*60}")

            # Warmup rounds (not counted)
            if warmup > 0:
                print(f"  Warming up ({warmup} rounds)...", end="", flush=True)
                for _ in range(warmup):
                    if protocol == "local":
                        await bench_fn(text, sc)
                    else:
                        await bench_fn(text, sc, api_key)
                    print(".", end="", flush=True)
                print(" done")

            # Benchmark rounds
            round_results = []
            for i in range(1, rounds + 1):
                if protocol == "local":
                    result = await bench_fn(text, sc)
                else:
                    result = await bench_fn(text, sc, api_key)

                round_results.append(result)
                all_results.append(result)

                status = "OK" if result.success else f"FAIL: {result.error}"
                print(
                    f"  Round {i:2d}/{rounds}: "
                    f"TTFB={result.ttfb_ms:7.1f}ms  "
                    f"Total={result.total_ms:7.1f}ms  "
                    f"Audio={result.audio_bytes:6d}B  "
                    f"[{status}]"
                )

                # Small delay between rounds to avoid rate limiting
                await asyncio.sleep(0.3)

            summary = summarize(round_results)
            if summary:
                summaries.append(summary)
                print(f"\n  --- Summary ---")
                print(
                    f"  TTFB:  mean={summary.ttfb_mean_ms}ms  "
                    f"median={summary.ttfb_median_ms}ms  "
                    f"P95={summary.ttfb_p95_ms}ms  "
                    f"std={summary.ttfb_std_ms}ms"
                )
                print(
                    f"  Total: mean={summary.total_mean_ms}ms  "
                    f"median={summary.total_median_ms}ms  "
                    f"P95={summary.total_p95_ms}ms"
                )
                print(f"  Success rate: {summary.success_rate}%")

    return {
        "results": [asdict(r) for r in all_results],
        "summaries": [asdict(s) for s in summaries],
    }


def print_comparison_table(summaries_data: list) -> str:
    """Print a comparison table across all providers/protocols."""
    lines = []
    lines.append("")
    lines.append("=" * 90)
    lines.append("  BENCHMARK COMPARISON -- Voice.ai vs macOS Daniel (JARVIS baseline)")
    lines.append("=" * 90)
    lines.append(
        f"  {'Provider':<15} {'Protocol':<12} {'Sentence':<8} "
        f"{'TTFB Mean':>10} {'TTFB P95':>10} {'Std Dev':>8} "
        f"{'Total':>10} {'Success':>8}"
    )
    lines.append("-" * 90)

    for s in summaries_data:
        lines.append(
            f"  {s['provider']:<15} {s['protocol']:<12} {s['sentence_class']:<8} "
            f"{s['ttfb_mean_ms']:>8.1f}ms {s['ttfb_p95_ms']:>8.1f}ms "
            f"{s['ttfb_std_ms']:>6.1f}ms "
            f"{s['total_mean_ms']:>8.1f}ms {s['success_rate']:>6.1f}%"
        )

    lines.append("-" * 90)

    # Calculate delta between Voice.ai WS and macOS Daniel for medium sentence
    ws_medium = next(
        (s for s in summaries_data
         if s["protocol"] == "websocket" and s["sentence_class"] == "medium"),
        None,
    )
    mac_medium = next(
        (s for s in summaries_data
         if s["protocol"] == "local" and s["sentence_class"] == "medium"),
        None,
    )

    if ws_medium and mac_medium:
        delta = ws_medium["ttfb_mean_ms"] - mac_medium["ttfb_mean_ms"]
        sign = "+" if delta > 0 else ""
        lines.append(
            f"\n  Voice.ai WS TTFB vs macOS Daniel TTFB (medium): "
            f"{sign}{delta:.1f}ms"
        )
        if delta < 200:
            lines.append(
                "  VERDICT: Voice.ai WebSocket is within 200ms "
                "conversational threshold"
            )
        else:
            lines.append(
                "  VERDICT: Voice.ai WebSocket exceeds 200ms "
                "conversational threshold"
            )

    output = "\n".join(lines)
    print(output)
    return output


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Voice.ai TTS Latency Benchmark"
    )
    parser.add_argument(
        "--rounds", type=int, default=10,
        help="Benchmark rounds per test (default: 10)",
    )
    parser.add_argument(
        "--warmup", type=int, default=2,
        help="Warmup rounds (default: 2)",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output JSON file path",
    )
    args = parser.parse_args()

    api_key = os.environ.get("VOICEAI_API_KEY")
    if not api_key:
        print("ERROR: Set VOICEAI_API_KEY environment variable")
        print("  export VOICEAI_API_KEY='vk_...'")
        sys.exit(1)

    print("=" * 60)
    print("  Voice.ai TTS Latency Benchmark")
    print(f"  Rounds: {args.rounds}  |  Warmup: {args.warmup}")
    print("  Providers: macOS Daniel (local), Voice.ai (HTTP + WS)")
    print(f"  Sentences: {', '.join(TEST_SENTENCES.keys())}")
    print("=" * 60)

    data = await run_benchmark(args.rounds, args.warmup, api_key)

    # Print comparison table
    table_output = print_comparison_table(data["summaries"])

    # Save results
    output_dir = Path(__file__).parent
    output_path = args.output or str(
        output_dir / f"voiceai_benchmark_{time.strftime('%Y%m%dT%H%M%S')}.json"
    )
    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)
    print(f"\n  Results saved to: {output_path}")

    # Also save a markdown summary
    md_path = output_path.replace(".json", ".md")
    with open(md_path, "w") as f:
        f.write("# Voice.ai TTS Benchmark Results\n\n")
        f.write(f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Rounds: {args.rounds} | Warmup: {args.warmup}\n\n")
        f.write("```\n")
        f.write(table_output)
        f.write("\n```\n")
    print(f"  Summary saved to: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
