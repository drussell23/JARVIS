#!/usr/bin/env python3
"""
DoubleWord SSE smoke test — isolate streaming vs non-streaming on identical payload.

Purpose
-------
One bounded diagnostic for the benchmark report to DoubleWord (Meryem Arik).
Two back-to-back requests, same model, same prompt, same generation params —
only the `stream` flag differs. Captures timing, bytes received, chunk count,
time-to-first-byte, stall detection, and the DW-returned request_id (if any).

Intent
------
Reproduce the SSE stream-stall signature observed on 2026-04-14 in sessions
`bt-2026-04-14-182446` (Gemma 4 31B) and `bt-2026-04-14-203740` (Qwen 3.5 397B)
on today's date, with the non-streaming variant as the control. If non-stream
succeeds while stream stalls on the same payload, the blocker is isolated to
the SSE transport layer, not model capability or prompt size.

Security
--------
Reads DOUBLEWORD_API_KEY and DOUBLEWORD_BASE_URL from environment only.
No keys are persisted to disk or committed to the repo. Output artifacts
go to .ouroboros/benchmarks/ which is diagnostic telemetry, not source code.

Usage
-----
    # Qwen 397B STANDARD-shape payload
    python3 scripts/benchmarks/dw_sse_smoke.py \
        --model "Qwen/Qwen3.5-397B-A17B-FP8" \
        --label qwen397b_standard

    # Gemma 31B BACKGROUND-shape payload
    python3 scripts/benchmarks/dw_sse_smoke.py \
        --model "google/gemma-4-31B-it" \
        --label gemma31b_background

    # Custom prompt from file
    python3 scripts/benchmarks/dw_sse_smoke.py \
        --model "google/gemma-4-31B-it" \
        --prompt-file /tmp/myprompt.txt \
        --max-tokens 2048

Defaults
--------
- base URL:       https://api.doubleword.ai/v1 (env override: DOUBLEWORD_BASE_URL)
- stall timeout:  30s no-data (matches DW client default; env DW_STALL_TIMEOUT_S)
- wall timeout:   180s (matches BACKGROUND route budget; env DW_WALL_TIMEOUT_S)
- max tokens:     4096 output (bounded for diagnostic, configurable via --max-tokens)
- temperature:    0.2 (matches DOUBLEWORD_TEMPERATURE default)

Output
------
Writes a JSON report to .ouroboros/benchmarks/dw_sse_smoke_<label>_<ts>.json
with per-variant fields:
    - ts_start_utc, ts_end_utc, wall_s
    - ttfb_s                    (time to first byte)
    - ttft_s                    (time to first token — SSE only)
    - stall_detected            (bool; true if >= stall_timeout_s with no data)
    - stall_at_s                (elapsed seconds when stall detected)
    - bytes_received
    - chunks_received           (SSE only)
    - completed_normally        (bool)
    - error_class               (str or null)
    - error_detail              (str or null)
    - http_status               (int or null)
    - request_id                (str or null — DW may expose via X-Request-Id header)
    - usage                     ({prompt_tokens, completion_tokens, total_tokens} or null)
    - raw_completion_preview    (first 200 chars of generated content if available)

Exit codes
----------
0 — both variants completed normally (diagnostic: stream WORKS today)
1 — stream stalled, non-stream succeeded (diagnostic: SSE-specific issue, matches Apr 14)
2 — both stalled (diagnostic: endpoint-level issue, not streaming-specific)
3 — non-stream stalled/failed, stream succeeded (unusual — investigate)
4 — configuration error (missing API key, bad model, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import httpx
except ImportError:
    print("ERROR: httpx is required. Install with: pip3 install httpx", file=sys.stderr)
    sys.exit(4)


# ---------------------------------------------------------------------------
# Payload templates — deliberately modeled after what O+V sends in production
# ---------------------------------------------------------------------------

DEFAULT_PROMPT = """You are an engineering diagnostic assistant. Please write a JSON object
with the following schema, and nothing else:

{
  "diagnostic_id": "<a fresh uuid4 string>",
  "endpoint_tested": "DoubleWord /v1/chat/completions",
  "stream_mode": "<one of: streaming, non-streaming>",
  "timestamp_iso": "<current ISO-8601 timestamp>",
  "observations": [
    "Observation 1 about the request",
    "Observation 2 about the request",
    "Observation 3 about the request"
  ],
  "reasoning_trace": "<2-3 sentences of reasoning about what was just generated>"
}

Fill in the fields. Return ONLY the JSON object, no prose, no markdown fences.
"""


def _iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_request_body(model: str, prompt: str, max_tokens: int, temperature: float, stream: bool) -> Dict[str, Any]:
    """OpenAI-compatible chat completions body. Mirrors what DoublewordProvider emits."""
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": stream,
    }


def _fingerprint_payload(body: Dict[str, Any]) -> Dict[str, Any]:
    """Extract a tamper-evident fingerprint of the payload for the report."""
    messages = body.get("messages") or []
    total_chars = sum(len(m.get("content", "")) for m in messages if isinstance(m.get("content"), str))
    return {
        "model": body.get("model"),
        "message_count": len(messages),
        "total_prompt_chars": total_chars,
        "est_prompt_tokens": int(total_chars / 3.5),  # rough, matches _DW_CHARS_PER_TOKEN heuristic
        "max_tokens": body.get("max_tokens"),
        "temperature": body.get("temperature"),
        "stream": body.get("stream"),
    }


# ---------------------------------------------------------------------------
# Per-variant probes
# ---------------------------------------------------------------------------

def probe_non_streaming(
    base_url: str,
    api_key: str,
    body: Dict[str, Any],
    stall_timeout_s: float,
    wall_timeout_s: float,
) -> Dict[str, Any]:
    """Single blocking POST with stream=false. Measures end-to-end latency."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    result: Dict[str, Any] = {
        "variant": "non_streaming",
        "ts_start_utc": _iso_utc(),
        "ttfb_s": None,
        "ttft_s": None,
        "stall_detected": False,
        "stall_at_s": None,
        "bytes_received": 0,
        "chunks_received": None,
        "completed_normally": False,
        "error_class": None,
        "error_detail": None,
        "http_status": None,
        "request_id": None,
        "usage": None,
        "raw_completion_preview": None,
    }

    t0 = time.monotonic()
    try:
        # httpx stream=False by default; read_timeout governs no-data stall for blocking reads
        with httpx.Client(timeout=httpx.Timeout(wall_timeout_s, read=stall_timeout_s)) as client:
            resp = client.post(url, headers=headers, json=body)
            ttfb = time.monotonic() - t0
            result["ttfb_s"] = round(ttfb, 3)
            result["http_status"] = resp.status_code
            result["request_id"] = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id")
            body_bytes = resp.content
            result["bytes_received"] = len(body_bytes)
            resp.raise_for_status()
            data = resp.json()
            result["usage"] = data.get("usage")
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content") or ""
                result["raw_completion_preview"] = content[:200]
            result["completed_normally"] = True
    except httpx.ReadTimeout as e:
        elapsed = time.monotonic() - t0
        result["stall_detected"] = True
        result["stall_at_s"] = round(elapsed, 3)
        result["error_class"] = "ReadTimeout"
        result["error_detail"] = str(e)
    except httpx.ConnectTimeout as e:
        result["error_class"] = "ConnectTimeout"
        result["error_detail"] = str(e)
    except httpx.HTTPStatusError as e:
        result["error_class"] = "HTTPStatusError"
        result["error_detail"] = f"{e.response.status_code}: {e.response.text[:500]}"
    except Exception as e:
        result["error_class"] = type(e).__name__
        result["error_detail"] = str(e)[:500]

    result["ts_end_utc"] = _iso_utc()
    result["wall_s"] = round(time.monotonic() - t0, 3)
    return result


def probe_streaming(
    base_url: str,
    api_key: str,
    body: Dict[str, Any],
    stall_timeout_s: float,
    wall_timeout_s: float,
) -> Dict[str, Any]:
    """SSE streaming POST with stream=true. Measures time-to-first-byte, time-to-first-token,
    stall detection (no data for stall_timeout_s), chunk count, and total bytes received.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    result: Dict[str, Any] = {
        "variant": "streaming",
        "ts_start_utc": _iso_utc(),
        "ttfb_s": None,
        "ttft_s": None,
        "stall_detected": False,
        "stall_at_s": None,
        "bytes_received": 0,
        "chunks_received": 0,
        "completed_normally": False,
        "error_class": None,
        "error_detail": None,
        "http_status": None,
        "request_id": None,
        "usage": None,
        "raw_completion_preview": None,
    }

    t0 = time.monotonic()
    first_byte_at: Optional[float] = None
    first_token_at: Optional[float] = None
    last_data_at = t0
    accumulated_text = []
    chunk_count = 0
    total_bytes = 0

    try:
        with httpx.Client(timeout=httpx.Timeout(wall_timeout_s, read=stall_timeout_s)) as client:
            with client.stream("POST", url, headers=headers, json=body) as resp:
                ttfb = time.monotonic() - t0
                result["ttfb_s"] = round(ttfb, 3)
                result["http_status"] = resp.status_code
                result["request_id"] = resp.headers.get("x-request-id") or resp.headers.get("X-Request-Id")
                if resp.status_code >= 400:
                    error_body = resp.read()
                    result["error_class"] = "HTTPStatusError"
                    result["error_detail"] = f"{resp.status_code}: {error_body.decode('utf-8', errors='replace')[:500]}"
                    result["bytes_received"] = len(error_body)
                    result["ts_end_utc"] = _iso_utc()
                    result["wall_s"] = round(time.monotonic() - t0, 3)
                    return result

                for line in resp.iter_lines():
                    now = time.monotonic()
                    # Detect stall: client-side gap between chunks
                    gap = now - last_data_at
                    if gap >= stall_timeout_s:
                        result["stall_detected"] = True
                        result["stall_at_s"] = round(now - t0, 3)
                        result["error_class"] = "StreamStalled"
                        result["error_detail"] = f"No data for {gap:.1f}s (threshold: {stall_timeout_s}s)"
                        break
                    if not line:
                        # SSE comment or heartbeat — skip
                        continue
                    last_data_at = now
                    if first_byte_at is None:
                        first_byte_at = now
                    chunk_count += 1
                    total_bytes += len(line.encode("utf-8"))
                    # SSE format: "data: {...}"
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            result["completed_normally"] = True
                            break
                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        choices = evt.get("choices") or []
                        if choices:
                            delta = choices[0].get("delta") or {}
                            piece = delta.get("content")
                            if piece:
                                if first_token_at is None:
                                    first_token_at = now
                                accumulated_text.append(piece)
                        if "usage" in evt and evt["usage"]:
                            result["usage"] = evt["usage"]

                # If we exited the loop without [DONE] but also without stalling, mark as early-close
                if not result["completed_normally"] and not result["stall_detected"]:
                    result["error_class"] = "StreamEndedWithoutDone"
                    result["error_detail"] = f"Stream closed after {chunk_count} chunks, no [DONE] marker"

    except httpx.ReadTimeout as e:
        elapsed = time.monotonic() - t0
        result["stall_detected"] = True
        result["stall_at_s"] = round(elapsed, 3)
        result["error_class"] = "ReadTimeout"
        result["error_detail"] = str(e)
    except httpx.ConnectTimeout as e:
        result["error_class"] = "ConnectTimeout"
        result["error_detail"] = str(e)
    except Exception as e:
        result["error_class"] = type(e).__name__
        result["error_detail"] = str(e)[:500]

    result["chunks_received"] = chunk_count
    result["bytes_received"] = total_bytes
    if first_token_at is not None:
        result["ttft_s"] = round(first_token_at - t0, 3)
    completion_text = "".join(accumulated_text)
    result["raw_completion_preview"] = completion_text[:200] if completion_text else None
    result["ts_end_utc"] = _iso_utc()
    result["wall_s"] = round(time.monotonic() - t0, 3)
    return result


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def classify_exit_code(stream_res: Dict[str, Any], nonstream_res: Dict[str, Any]) -> int:
    stream_ok = stream_res.get("completed_normally", False)
    nonstream_ok = nonstream_res.get("completed_normally", False)
    if stream_ok and nonstream_ok:
        return 0
    if (not stream_ok) and nonstream_ok:
        return 1
    if (not stream_ok) and (not nonstream_ok):
        return 2
    if stream_ok and (not nonstream_ok):
        return 3
    return 4


def exit_code_narrative(code: int) -> str:
    return {
        0: "Both stream AND non-stream COMPLETED. Endpoint is healthy today.",
        1: "Stream STALLED, non-stream SUCCEEDED. This is the SSE-specific signature — matches 2026-04-14 observation. Isolates the blocker to the streaming transport layer.",
        2: "Both stream AND non-stream FAILED. This is endpoint-level, not streaming-specific — warrants deeper investigation.",
        3: "Non-stream FAILED but stream SUCCEEDED. Unusual — check DW status page and retry.",
        4: "Configuration error. Check environment variables.",
    }.get(code, "Unknown outcome.")


def write_report(report: Dict[str, Any], out_dir: Path, label: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"dw_sse_smoke_{label}_{ts}.json"
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True, default=str)
    return out_path


def print_summary(report: Dict[str, Any]) -> None:
    stream = report["streaming"]
    nonstream = report["non_streaming"]
    fp = report["payload_fingerprint"]
    print("=" * 78)
    print("DoubleWord SSE Smoke Test — Summary")
    print("=" * 78)
    print(f"Label:              {report['label']}")
    print(f"Run ID:             {report['run_id']}")
    print(f"Timestamp:          {report['ts_utc']}")
    print(f"Base URL:           {report['base_url']}")
    print(f"Model:              {fp['model']}")
    print(f"Prompt chars:       {fp['total_prompt_chars']:,} (~{fp['est_prompt_tokens']:,} tokens)")
    print(f"max_tokens (out):   {fp['max_tokens']:,}")
    print(f"temperature:        {fp['temperature']}")
    print(f"stall_timeout:      {report['stall_timeout_s']}s")
    print(f"wall_timeout:       {report['wall_timeout_s']}s")
    print()
    print("-" * 78)
    print(f"{'':20} {'STREAM':>14} {'NON-STREAM':>14}")
    print("-" * 78)
    rows = [
        ("completed_normally", stream["completed_normally"], nonstream["completed_normally"]),
        ("stall_detected", stream["stall_detected"], nonstream["stall_detected"]),
        ("stall_at_s", stream["stall_at_s"], nonstream["stall_at_s"]),
        ("wall_s", stream["wall_s"], nonstream["wall_s"]),
        ("ttfb_s", stream["ttfb_s"], nonstream["ttfb_s"]),
        ("ttft_s", stream["ttft_s"], nonstream["ttft_s"]),
        ("bytes_received", stream["bytes_received"], nonstream["bytes_received"]),
        ("chunks_received", stream["chunks_received"], nonstream["chunks_received"]),
        ("http_status", stream["http_status"], nonstream["http_status"]),
        ("request_id", stream["request_id"] or "-", nonstream["request_id"] or "-"),
        ("error_class", stream["error_class"] or "-", nonstream["error_class"] or "-"),
    ]
    for label, s, n in rows:
        print(f"{label:20} {str(s):>14} {str(n):>14}")
    print("-" * 78)
    if stream["error_detail"]:
        print(f"STREAM error detail: {stream['error_detail'][:200]}")
    if nonstream["error_detail"]:
        print(f"NON-STREAM error detail: {nonstream['error_detail'][:200]}")
    if stream.get("usage"):
        u = stream["usage"]
        print(f"STREAM usage:  prompt={u.get('prompt_tokens','?')}, completion={u.get('completion_tokens','?')}, total={u.get('total_tokens','?')}")
    if nonstream.get("usage"):
        u = nonstream["usage"]
        print(f"NON-STREAM usage:  prompt={u.get('prompt_tokens','?')}, completion={u.get('completion_tokens','?')}, total={u.get('total_tokens','?')}")
    print("=" * 78)
    print(f"VERDICT (exit {report['exit_code']}): {report['verdict']}")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="DoubleWord SSE vs non-stream smoke test. Diagnostic only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Usage")[1].split("Output")[0] if __doc__ else "",
    )
    parser.add_argument("--model", default=None,
                        help="DW model ID. Default: DOUBLEWORD_MODEL env or Qwen/Qwen3.5-397B-A17B-FP8")
    parser.add_argument("--base-url", default=None,
                        help="DW base URL. Default: DOUBLEWORD_BASE_URL env or https://api.doubleword.ai/v1")
    parser.add_argument("--prompt-file", default=None,
                        help="File containing the prompt. Default: built-in structured-JSON diagnostic prompt.")
    parser.add_argument("--max-tokens", type=int, default=4096,
                        help="max_tokens output bound. Default: 4096.")
    parser.add_argument("--temperature", type=float, default=0.2,
                        help="Sampling temperature. Default: 0.2 (matches DOUBLEWORD_TEMPERATURE default).")
    parser.add_argument("--stall-timeout", type=float, default=None,
                        help="No-data stall timeout in seconds. Default: 30 (matches DW client). Env: DW_STALL_TIMEOUT_S")
    parser.add_argument("--wall-timeout", type=float, default=None,
                        help="Overall request wall timeout in seconds. Default: 180 (matches BG route). Env: DW_WALL_TIMEOUT_S")
    parser.add_argument("--label", default="diag",
                        help="Label used in output filename. E.g., 'qwen397b_standard' or 'gemma31b_background'.")
    parser.add_argument("--out-dir", default=None,
                        help="Output directory. Default: .ouroboros/benchmarks/")
    parser.add_argument("--order", default="stream_first", choices=["stream_first", "nonstream_first"],
                        help="Which variant to run first. Default: stream_first.")

    args = parser.parse_args()

    # Resolve config
    api_key = os.environ.get("DOUBLEWORD_API_KEY")
    if not api_key:
        print("ERROR: DOUBLEWORD_API_KEY environment variable is required.", file=sys.stderr)
        print("       Set it with: export DOUBLEWORD_API_KEY='...'", file=sys.stderr)
        print("       NEVER commit this key to the repo.", file=sys.stderr)
        return 4

    base_url = args.base_url or os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
    model = args.model or os.environ.get("DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
    stall_timeout_s = args.stall_timeout if args.stall_timeout is not None else float(os.environ.get("DW_STALL_TIMEOUT_S", "30"))
    wall_timeout_s = args.wall_timeout if args.wall_timeout is not None else float(os.environ.get("DW_WALL_TIMEOUT_S", "180"))

    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            print(f"ERROR: --prompt-file not found: {prompt_path}", file=sys.stderr)
            return 4
        prompt = prompt_path.read_text(encoding="utf-8")
    else:
        prompt = DEFAULT_PROMPT

    body_template = _build_request_body(model, prompt, args.max_tokens, args.temperature, stream=False)
    fingerprint = _fingerprint_payload(body_template)

    run_id = str(uuid.uuid4())
    print(f"[dw_sse_smoke] run_id={run_id} model={model} label={args.label}")
    print(f"[dw_sse_smoke] prompt_chars={fingerprint['total_prompt_chars']} est_tokens={fingerprint['est_prompt_tokens']} max_out={args.max_tokens}")
    print(f"[dw_sse_smoke] stall_timeout={stall_timeout_s}s wall_timeout={wall_timeout_s}s")
    print(f"[dw_sse_smoke] order={args.order}")
    print()

    # Build both bodies (only `stream` flag differs — otherwise identical)
    stream_body = dict(body_template)
    stream_body["stream"] = True
    nonstream_body = dict(body_template)
    nonstream_body["stream"] = False

    if args.order == "stream_first":
        print("[dw_sse_smoke] probing STREAM variant...")
        stream_res = probe_streaming(base_url, api_key, stream_body, stall_timeout_s, wall_timeout_s)
        print(f"[dw_sse_smoke] STREAM done: completed={stream_res['completed_normally']} stalled={stream_res['stall_detected']} wall_s={stream_res['wall_s']}")
        print()
        print("[dw_sse_smoke] probing NON-STREAM variant...")
        nonstream_res = probe_non_streaming(base_url, api_key, nonstream_body, stall_timeout_s, wall_timeout_s)
        print(f"[dw_sse_smoke] NON-STREAM done: completed={nonstream_res['completed_normally']} stalled={nonstream_res['stall_detected']} wall_s={nonstream_res['wall_s']}")
    else:
        print("[dw_sse_smoke] probing NON-STREAM variant...")
        nonstream_res = probe_non_streaming(base_url, api_key, nonstream_body, stall_timeout_s, wall_timeout_s)
        print(f"[dw_sse_smoke] NON-STREAM done: completed={nonstream_res['completed_normally']} stalled={nonstream_res['stall_detected']} wall_s={nonstream_res['wall_s']}")
        print()
        print("[dw_sse_smoke] probing STREAM variant...")
        stream_res = probe_streaming(base_url, api_key, stream_body, stall_timeout_s, wall_timeout_s)
        print(f"[dw_sse_smoke] STREAM done: completed={stream_res['completed_normally']} stalled={stream_res['stall_detected']} wall_s={stream_res['wall_s']}")

    exit_code = classify_exit_code(stream_res, nonstream_res)
    verdict = exit_code_narrative(exit_code)

    report = {
        "run_id": run_id,
        "ts_utc": _iso_utc(),
        "label": args.label,
        "base_url": base_url,
        "payload_fingerprint": fingerprint,
        "stall_timeout_s": stall_timeout_s,
        "wall_timeout_s": wall_timeout_s,
        "streaming": stream_res,
        "non_streaming": nonstream_res,
        "exit_code": exit_code,
        "verdict": verdict,
    }

    out_dir = Path(args.out_dir) if args.out_dir else Path(".ouroboros/benchmarks")
    out_path = write_report(report, out_dir, args.label)
    print()
    print_summary(report)
    print(f"\nFull report written to: {out_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
