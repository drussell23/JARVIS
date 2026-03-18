#!/usr/bin/env python3
"""
Voice.ai Benchmark v2 — Persistent session + audio playback
=============================================================
Tests Voice.ai HTTP streaming (persistent TCP session) and WebSocket
against macOS Daniel. Plays audio on first round so you can hear it.

Usage:
  export VOICEAI_API_KEY="vk_..."
  python3 benchmarks/voiceai_persistent_ws_benchmark.py [--rounds 10]
"""

import asyncio
import argparse
import base64
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import aiohttp

VOICEAI_STREAM_URL = "https://dev.voice.ai/api/v1/tts/speech/stream"
VOICEAI_WS_URL = "wss://dev.voice.ai/api/v1/tts/multi-stream"

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


@dataclass
class Result:
    provider: str
    protocol: str
    sentence_class: str
    round_num: int
    ttfb_ms: float
    total_ms: float
    playback_ms: float
    audio_bytes: int
    success: bool
    error: Optional[str] = None


def percentile(data, p):
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    return s[f] + (k - f) * (s[c] - s[f])


async def play_audio_file(path):
    """Play audio via afplay (macOS native, GIL-free)."""
    t0 = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        "afplay", path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return (time.perf_counter() - t0) * 1000


# ---- macOS Daniel ----

async def bench_daniel(text, sc, rnd, play):
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        t0 = time.perf_counter()
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", "Daniel", "-r", "175", "-o", tmp_path, text,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        ttfb = (time.perf_counter() - t0) * 1000
        audio_bytes = Path(tmp_path).stat().st_size

        playback_ms = 0.0
        if play:
            playback_ms = await play_audio_file(tmp_path)
        t_end = time.perf_counter()

        return Result("macOS_Daniel", "local", sc, rnd,
                       round(ttfb, 2), round((t_end - t0) * 1000, 2),
                       round(playback_ms, 2), audio_bytes, True)
    except Exception as e:
        return Result("macOS_Daniel", "local", sc, rnd, 0, 0, 0, 0, False, str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---- Voice.ai HTTP streaming (persistent session) ----

async def bench_voiceai_http(session, api_key, text, sc, rnd, play):
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    payload = {"text": text, "audio_format": "mp3", "language": "en"}

    t0 = time.perf_counter()
    ttfb = 0.0
    chunks = []
    first = True

    try:
        async with session.post(
            VOICEAI_STREAM_URL, headers=headers, json=payload
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                elapsed = (time.perf_counter() - t0) * 1000
                return Result("Voice.ai", "http_persistent", sc, rnd,
                               round(elapsed, 2), round(elapsed, 2),
                               0, 0, False,
                               "HTTP " + str(resp.status) + ": " + body[:200])

            async for chunk in resp.content.iter_any():
                if first and chunk:
                    ttfb = (time.perf_counter() - t0) * 1000
                    first = False
                chunks.append(chunk)

        t_recv = time.perf_counter()
        total_bytes = sum(len(c) for c in chunks)

        playback_ms = 0.0
        if play and chunks:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(b"".join(chunks))
            try:
                playback_ms = await play_audio_file(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return Result("Voice.ai", "http_persistent", sc, rnd,
                       round(ttfb, 2), round((t_recv - t0) * 1000, 2),
                       round(playback_ms, 2), total_bytes, True)
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return Result("Voice.ai", "http_persistent", sc, rnd,
                       round(ttfb or elapsed, 2), round(elapsed, 2),
                       0, 0, False, str(e))


# ---- Voice.ai WebSocket (fresh per request) ----

async def bench_voiceai_ws(api_key, text, sc, rnd, play):
    import websockets

    t0 = time.perf_counter()
    ttfb = 0.0
    chunks = []
    first = True

    try:
        async with websockets.connect(
            VOICEAI_WS_URL,
            additional_headers={"Authorization": "Bearer " + api_key},
            ping_interval=20, ping_timeout=10, close_timeout=5,
        ) as ws:
            await ws.send(json.dumps({
                "text": text, "language": "en", "flush": True,
            }))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                except asyncio.TimeoutError:
                    break
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())

                if "error" in data:
                    elapsed = (time.perf_counter() - t0) * 1000
                    return Result("Voice.ai", "websocket", sc, rnd,
                                   round(elapsed, 2), round(elapsed, 2),
                                   0, 0, False, str(data["error"]))

                if "audio" in data:
                    ad = base64.b64decode(data["audio"])
                    if first and ad:
                        ttfb = (time.perf_counter() - t0) * 1000
                        first = False
                    chunks.append(ad)
                if data.get("is_last", False):
                    break

            try:
                await ws.send(json.dumps({"close_socket": True}))
            except Exception:
                pass

        t_recv = time.perf_counter()
        total_bytes = sum(len(c) for c in chunks)

        playback_ms = 0.0
        if play and chunks:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                tmp_path = tmp.name
                for c in chunks:
                    tmp.write(c)
            try:
                playback_ms = await play_audio_file(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        return Result("Voice.ai", "websocket", sc, rnd,
                       round(ttfb, 2), round((t_recv - t0) * 1000, 2),
                       round(playback_ms, 2), total_bytes, True)
    except Exception as e:
        elapsed = (time.perf_counter() - t0) * 1000
        return Result("Voice.ai", "websocket", sc, rnd,
                       round(ttfb or elapsed, 2), round(elapsed, 2),
                       0, 0, False, str(e))


# ---- Main runner ----

async def run_all(rounds, warmup, api_key, play):
    all_results = []

    # Phase 1: Daniel
    print("\n" + "=" * 65)
    print("  PHASE 1: macOS Daniel (local baseline)")
    print("=" * 65)
    for sc, text in TEST_SENTENCES.items():
        label = text[:55] + "..." if len(text) > 55 else text
        print("\n  [" + sc + "] \"" + label + "\"")
        for i in range(1, rounds + 1):
            do_play = play and (i == 1)
            r = await bench_daniel(text, sc, i, do_play)
            all_results.append(r)
            tag = " >> PLAYING" if do_play else ""
            print("    " + str(i).rjust(2) + "/" + str(rounds) +
                  ": TTFB=" + str(r.ttfb_ms).rjust(7) + "ms  " +
                  "Total=" + str(r.total_ms).rjust(7) + "ms  " +
                  "Audio=" + str(r.audio_bytes).rjust(6) + "B" + tag)

    # Phase 2: Voice.ai HTTP persistent
    print("\n" + "=" * 65)
    print("  PHASE 2: Voice.ai HTTP Streaming (persistent TCP session)")
    print("=" * 65)

    conn = aiohttp.TCPConnector(limit=1, keepalive_timeout=60)
    async with aiohttp.ClientSession(connector=conn) as session:
        for sc, text in TEST_SENTENCES.items():
            label = text[:55] + "..." if len(text) > 55 else text
            print("\n  [" + sc + "] \"" + label + "\"")

            if warmup > 0:
                print("    Warming up (" + str(warmup) + ")...", end="", flush=True)
                for _ in range(warmup):
                    await bench_voiceai_http(session, api_key, text, sc, 0, False)
                    print(".", end="", flush=True)
                print(" done")

            for i in range(1, rounds + 1):
                do_play = play and (i == 1)
                r = await bench_voiceai_http(session, api_key, text, sc, i, do_play)
                all_results.append(r)
                ok = "OK" if r.success else "FAIL: " + str(r.error)
                tag = " >> PLAYING" if do_play and r.success else ""
                print("    " + str(i).rjust(2) + "/" + str(rounds) +
                      ": TTFB=" + str(r.ttfb_ms).rjust(7) + "ms  " +
                      "Total=" + str(r.total_ms).rjust(7) + "ms  " +
                      "Audio=" + str(r.audio_bytes).rjust(6) + "B  [" + ok + "]" + tag)
                await asyncio.sleep(0.2)

    # Phase 3: Voice.ai WebSocket
    print("\n" + "=" * 65)
    print("  PHASE 3: Voice.ai WebSocket (fresh connection per request)")
    print("=" * 65)
    for sc, text in TEST_SENTENCES.items():
        label = text[:55] + "..." if len(text) > 55 else text
        print("\n  [" + sc + "] \"" + label + "\"")
        for i in range(1, rounds + 1):
            do_play = play and (i == 1)
            r = await bench_voiceai_ws(api_key, text, sc, i, do_play)
            all_results.append(r)
            ok = "OK" if r.success else "FAIL: " + str(r.error)
            tag = " >> PLAYING" if do_play and r.success else ""
            print("    " + str(i).rjust(2) + "/" + str(rounds) +
                  ": TTFB=" + str(r.ttfb_ms).rjust(7) + "ms  " +
                  "Total=" + str(r.total_ms).rjust(7) + "ms  " +
                  "Audio=" + str(r.audio_bytes).rjust(6) + "B  [" + ok + "]" + tag)
            await asyncio.sleep(0.5)

    return {"results": [asdict(r) for r in all_results]}


def print_report(data):
    results = data["results"]
    lines = []
    lines.append("")
    lines.append("=" * 85)
    lines.append("  FINAL COMPARISON -- Voice.ai vs macOS Daniel")
    lines.append("=" * 85)
    lines.append(
        "  " + "Provider".ljust(15) + "Protocol".ljust(20) + "Sent".ljust(8) +
        "TTFB Mean".rjust(10) + "TTFB P95".rjust(10) + "Std".rjust(8) +
        "Total".rjust(10) + "N".rjust(4)
    )
    lines.append("-" * 85)

    for proto in ["local", "http_persistent", "websocket"]:
        for sc in ["short", "medium", "long"]:
            hits = [r for r in results
                    if r["protocol"] == proto
                    and r["sentence_class"] == sc
                    and r["success"]
                    and r["round_num"] > 0]
            if not hits:
                continue
            ttfbs = [r["ttfb_ms"] for r in hits]
            totals = [r["total_ms"] for r in hits]
            prov = hits[0]["provider"]
            std = statistics.stdev(ttfbs) if len(ttfbs) > 1 else 0.0
            mean_t = statistics.mean(ttfbs)
            p95 = percentile(ttfbs, 95)
            mean_total = statistics.mean(totals)
            lines.append(
                "  " + prov.ljust(15) + proto.ljust(20) + sc.ljust(8) +
                (str(round(mean_t, 1)) + "ms").rjust(10) +
                (str(round(p95, 1)) + "ms").rjust(10) +
                (str(round(std, 1)) + "ms").rjust(8) +
                (str(round(mean_total, 1)) + "ms").rjust(10) +
                str(len(hits)).rjust(4)
            )

    lines.append("-" * 85)

    http_med = [r["ttfb_ms"] for r in results
                if r["protocol"] == "http_persistent"
                and r["sentence_class"] == "medium"
                and r["success"] and r["round_num"] > 0]
    dan_med = [r["ttfb_ms"] for r in results
               if r["protocol"] == "local"
               and r["sentence_class"] == "medium"
               and r["success"] and r["round_num"] > 0]
    if http_med and dan_med:
        h = statistics.mean(http_med)
        d = statistics.mean(dan_med)
        sp = d / h if h > 0 else 0
        lines.append("")
        lines.append(
            "  Medium sentence: Voice.ai HTTP = " + str(round(h)) +
            "ms vs Daniel = " + str(round(d)) + "ms (" +
            str(round(sp, 1)) + "x faster)"
        )

    all_http = [r["ttfb_ms"] for r in results
                if r["protocol"] == "http_persistent"
                and r["success"] and r["round_num"] > 0]
    if all_http:
        u200 = sum(1 for t in all_http if t < 200)
        u400 = sum(1 for t in all_http if t < 400)
        lines.append("")
        lines.append("  Voice.ai HTTP persistent TTFB distribution:")
        lines.append(
            "    Under 200ms: " + str(u200) + "/" + str(len(all_http)) +
            " (" + str(round(u200/len(all_http)*100)) + "%)"
        )
        lines.append(
            "    Under 400ms: " + str(u400) + "/" + str(len(all_http)) +
            " (" + str(round(u400/len(all_http)*100)) + "%)"
        )

    output = "\n".join(lines)
    print(output)
    return output


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--no-play", action="store_true")
    args = parser.parse_args()

    api_key = os.environ.get("VOICEAI_API_KEY")
    if not api_key:
        print("ERROR: Set VOICEAI_API_KEY")
        sys.exit(1)

    play = not args.no_play

    print("=" * 65)
    print("  Voice.ai Benchmark v2 (persistent session + audio playback)")
    print("  Rounds: " + str(args.rounds) + "  |  Warmup: " + str(args.warmup))
    if play:
        print("  Audio: ON (plays first round of each sentence)")
    else:
        print("  Audio: OFF")
    print("=" * 65)

    data = await run_all(args.rounds, args.warmup, api_key, play)
    report = print_report(data)

    out_dir = Path(__file__).parent
    ts = time.strftime("%Y%m%dT%H%M%S")
    jp = str(out_dir / ("voiceai_v2_" + ts + ".json"))
    with open(jp, "w") as f:
        json.dump(data, f, indent=2)
    mp = jp.replace(".json", ".md")
    with open(mp, "w") as f:
        f.write("# Voice.ai Benchmark v2\n\n")
        f.write("Date: " + time.strftime("%Y-%m-%d %H:%M:%S") + "\n\n")
        f.write("```\n" + report + "\n```\n")
    print("\n  Results: " + jp)
    print("  Report:  " + mp)


if __name__ == "__main__":
    asyncio.run(main())
