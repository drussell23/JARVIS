#!/usr/bin/env python3
"""
Trinity AI — Doubleword Batch API Benchmark
============================================
Runs the two standard Trinity demo tasks against Doubleword's batch API
and compares against recorded J-Prime (NVIDIA L4) baseline numbers.

Usage:
    export DOUBLEWORD_API_KEY=sk-...
    python3 benchmarks/doubleword/benchmark_doubleword.py

    # Choose a specific model (default: Qwen/Qwen3.5-35B-A3B-FP8)
    DOUBLEWORD_MODEL=Qwen/Qwen3.5-397B-A17B-FP8 python3 ...

    # Use 1h SLA (default) or 24h batch
    DOUBLEWORD_WINDOW=24h python3 ...

Outputs:
    benchmarks/doubleword/results/<timestamp>-UTC.json

Doubleword API docs: https://docs.doubleword.ai
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuration (all env-driven, no hardcoding) ────────────────────────────

DOUBLEWORD_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
DOUBLEWORD_BASE    = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
DOUBLEWORD_MODEL   = os.environ.get("DOUBLEWORD_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8")
DOUBLEWORD_WINDOW  = os.environ.get("DOUBLEWORD_WINDOW", "1h")
POLL_INTERVAL_S    = int(os.environ.get("DOUBLEWORD_POLL_INTERVAL_S", "10"))
MAX_WAIT_S         = int(os.environ.get("DOUBLEWORD_MAX_WAIT_S", "3600"))

# Pricing (March 2026 — update from https://www.doubleword.ai/calculator)
DW_INPUT_COST_PER_M  = float(os.environ.get("DOUBLEWORD_INPUT_COST_PER_M",  "0.10"))
DW_OUTPUT_COST_PER_M = float(os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"))

# J-Prime baseline (from benchmarks/LATEST.md — run 2026-03-16T08-05-18)
JPRIME_BASELINE = {
    "model":           "Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf",
    "compute":         "NVIDIA L4 (g2-standard-4, 24GB VRAM)",
    "vm_cost_per_hour": 1.20,
    "tasks": {
        "infra":  {"label": "Secure Infrastructure Code", "latency_ms": 27630, "tok_s": 24.6, "tokens": 680},
        "threat": {"label": "Defense Threat Analysis",    "latency_ms":  5334, "tok_s": 24.4, "tokens": 130},
    },
}

# ── Task definitions (exact prompts from demo_trinity_governed_loop.py) ───────

TASKS = [
    {
        "key":   "infra",
        "label": "Secure Infrastructure Code",
        "messages": [
            {
                "role":    "system",
                "content": "You are a senior infrastructure security engineer "
                           "working in a FedRAMP-certified environment. Return only code.",
            },
            {
                "role":    "user",
                "content": "Write a Python function that validates firewall rules "
                           "against a NIST 800-53 compliance policy. It should check "
                           "port ranges, CIDR blocks, and flag any rule that allows "
                           "unrestricted inbound access.",
            },
        ],
        "max_tokens": int(os.environ.get("DOUBLEWORD_MAX_TOKENS_INFRA", "2000")),
    },
    {
        "key":   "threat",
        "label": "Defense Threat Analysis",
        "messages": [
            {
                "role":    "system",
                "content": "You are a defense cybersecurity analyst. "
                           "Provide concise, actionable analysis.",
            },
            {
                "role":    "user",
                "content": "A classified network SOC detected 47 failed SSH login "
                           "attempts from 3 internal IPs within 90 seconds, followed "
                           "by a successful login and immediate sudo privilege escalation. "
                           "Classify the threat level and recommend immediate response "
                           "actions in 3 bullet points.",
            },
        ],
        "max_tokens": int(os.environ.get("DOUBLEWORD_MAX_TOKENS_THREAT", "500")),
    },
]


# ── curl-based HTTP helpers (no extra dependencies) ──────────────────────────

def _auth_header() -> list[str]:
    return ["-H", f"Authorization: Bearer {DOUBLEWORD_API_KEY}"]


def _curl_get(path: str) -> dict:
    result = subprocess.run(
        ["curl", "-sf", *_auth_header(), f"{DOUBLEWORD_BASE}{path}"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"GET {path} failed: {result.stderr}")
    return json.loads(result.stdout)


def _curl_post_json(path: str, payload: dict) -> dict:
    result = subprocess.run(
        [
            "curl", "-sf", *_auth_header(),
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload),
            f"{DOUBLEWORD_BASE}{path}",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"POST {path} failed: {result.stderr}")
    return json.loads(result.stdout)


def _curl_upload_file(path: str, filepath: str) -> dict:
    result = subprocess.run(
        [
            "curl", "-sf", *_auth_header(),
            "-F", f"file=@{filepath};type=application/jsonl",
            "-F", "purpose=batch",
            f"{DOUBLEWORD_BASE}{path}",
        ],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"File upload {path} failed: {result.stderr}")
    return json.loads(result.stdout)


def _curl_get_content(path: str) -> str:
    result = subprocess.run(
        ["curl", "-sf", *_auth_header(), f"{DOUBLEWORD_BASE}{path}"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"GET content {path} failed: {result.stderr}")
    return result.stdout


# ── Batch API flow ────────────────────────────────────────────────────────────

def build_jsonl(tasks: list[dict]) -> str:
    """Build JSONL batch file path. Returns temp file path."""
    lines = []
    for task in tasks:
        lines.append(json.dumps({
            "custom_id": task["key"],
            "method":    "POST",
            "url":       "/v1/chat/completions",
            "body": {
                "model":       DOUBLEWORD_MODEL,
                "messages":    task["messages"],
                "max_tokens":  task["max_tokens"],
                "temperature": 0.1,
            },
        }))
    tmp = Path("/tmp/trinity_dw_benchmark.jsonl")
    tmp.write_text("\n".join(lines))
    return str(tmp)


def list_models() -> list[dict]:
    return _curl_get("/models").get("data", [])


def upload_file(jsonl_path: str) -> str:
    print("  Uploading JSONL batch file... ", end="", flush=True)
    resp = _curl_upload_file("/files", jsonl_path)
    file_id = resp["id"]
    print(f"OK  (file_id={file_id})")
    return file_id


def create_batch(file_id: str) -> str:
    print(f"  Creating batch job (model={DOUBLEWORD_MODEL}, window={DOUBLEWORD_WINDOW})... ", end="", flush=True)
    resp = _curl_post_json("/batches", {
        "input_file_id":     file_id,
        "endpoint":          "/v1/chat/completions",
        "completion_window": DOUBLEWORD_WINDOW,
    })
    batch_id = resp["id"]
    print(f"OK  (batch_id={batch_id})")
    return batch_id


def poll_batch(batch_id: str) -> tuple[dict, float]:
    """Poll until complete. Returns (batch_object, wall_seconds)."""
    print("  Polling", end="", flush=True)
    started = time.perf_counter()
    while True:
        resp = _curl_get(f"/batches/{batch_id}")
        status    = resp.get("status", "unknown")
        completed = resp.get("request_counts", {}).get("completed", 0)
        total     = resp.get("request_counts", {}).get("total", 0)
        elapsed   = time.perf_counter() - started

        if status == "completed":
            print(f" done  ({elapsed:.0f}s · {completed}/{total} requests)")
            return resp, elapsed
        elif status in ("failed", "cancelled", "expired"):
            print(f" FAILED  (status={status})")
            return resp, elapsed
        else:
            print(".", end="", flush=True)
            if elapsed > MAX_WAIT_S:
                print(f" TIMEOUT ({elapsed:.0f}s)")
                return resp, elapsed
            time.sleep(POLL_INTERVAL_S)


def retrieve_results(output_file_id: str) -> dict[str, dict]:
    print("  Retrieving output... ", end="", flush=True)
    raw = _curl_get_content(f"/files/{output_file_id}/content")
    parsed: dict[str, dict] = {}
    for line in raw.strip().splitlines():
        obj = json.loads(line)
        parsed[obj["custom_id"]] = obj
    print(f"OK  ({len(parsed)} results)")
    return parsed


# ── Cost helpers ──────────────────────────────────────────────────────────────

def dw_cost(input_t: int, output_t: int) -> float:
    return (input_t / 1_000_000) * DW_INPUT_COST_PER_M + (output_t / 1_000_000) * DW_OUTPUT_COST_PER_M


def jprime_cost(task_key: str) -> float:
    latency_s = JPRIME_BASELINE["tasks"][task_key]["latency_ms"] / 1000
    return (latency_s / 3600) * JPRIME_BASELINE["vm_cost_per_hour"]


# ── Output ────────────────────────────────────────────────────────────────────

def sep(char: str = "─", width: int = 70) -> None:
    print(char * width)


def main() -> None:
    print()
    sep("═")
    print("  Trinity AI — Doubleword Batch vs J-Prime Benchmark")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    sep("═")

    if not DOUBLEWORD_API_KEY:
        print("\n  ❌  DOUBLEWORD_API_KEY not set.")
        print("  export DOUBLEWORD_API_KEY=sk-...\n")
        sys.exit(1)

    # ── Model catalog ─────────────────────────────────────────────────────────
    sep()
    print("  Available models on Doubleword")
    sep()
    try:
        models = list_models()
        for m in models:
            print(f"    • {m['id']}")
    except Exception as e:
        print(f"  Warning: could not list models: {e}")
        models = []

    print(f"\n  Benchmark model : {DOUBLEWORD_MODEL}")
    print(f"  J-Prime model   : {JPRIME_BASELINE['model']}")
    print(f"  J-Prime compute : {JPRIME_BASELINE['compute']}")
    print(f"  Batch window    : {DOUBLEWORD_WINDOW}")
    print()

    # ── Build + upload JSONL ──────────────────────────────────────────────────
    sep()
    print("  STEP 1 — Build & upload batch JSONL")
    sep()
    jsonl_path = build_jsonl(TASKS)
    print(f"  JSONL written to {jsonl_path}")
    try:
        file_id = upload_file(jsonl_path)
    except Exception as e:
        print(f"  ❌ Upload failed: {e}")
        sys.exit(1)

    # ── Create batch ──────────────────────────────────────────────────────────
    print()
    sep()
    print("  STEP 2 — Create batch job")
    sep()
    wall_start = time.perf_counter()
    try:
        batch_id = create_batch(file_id)
    except Exception as e:
        print(f"  ❌ Batch creation failed: {e}")
        sys.exit(1)

    # ── Poll ──────────────────────────────────────────────────────────────────
    print()
    sep()
    print(f"  STEP 3 — Wait for completion ({DOUBLEWORD_WINDOW} SLA)")
    sep()
    batch, wall_elapsed = poll_batch(batch_id)

    if batch.get("status") != "completed":
        print(f"  ❌ Batch ended with status: {batch.get('status')}")
        sys.exit(1)

    # ── Retrieve ──────────────────────────────────────────────────────────────
    print()
    sep()
    print("  STEP 4 — Retrieve & parse results")
    sep()
    output_file_id = batch["output_file_id"]
    raw_results = retrieve_results(output_file_id)

    # ── Parse + display ───────────────────────────────────────────────────────
    task_results: dict[str, dict] = {}
    print()

    for task in TASKS:
        k   = task["key"]
        raw = raw_results.get(k, {})
        if raw.get("error"):
            print(f"  ❌ {task['label']}: {raw['error']}")
            continue

        body          = raw.get("response", {}).get("body", {})
        usage         = body.get("usage", {})
        in_t          = usage.get("prompt_tokens", 0)
        out_t         = usage.get("completion_tokens", 0)
        finish_reason = (body.get("choices") or [{}])[0].get("finish_reason", "unknown")
        content       = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
        reasoning     = (body.get("choices") or [{}])[0].get("message", {}).get("reasoning_content", "") or ""

        cost = dw_cost(in_t, out_t)
        jp   = jprime_cost(k)

        task_results[k] = {
            "label":          task["label"],
            "input_tokens":   in_t,
            "output_tokens":  out_t,
            "finish_reason":  finish_reason,
            "input_cost_usd": round((in_t  / 1_000_000) * DW_INPUT_COST_PER_M, 8),
            "output_cost_usd":round((out_t / 1_000_000) * DW_OUTPUT_COST_PER_M, 8),
            "total_cost_usd": round(cost, 8),
            "content":        content,
            "reasoning_preview": reasoning[:300] if reasoning else "",
        }

        baseline = JPRIME_BASELINE["tasks"][k]
        sep()
        print(f"  Task: {task['label']}")
        sep()
        print(f"  {'Metric':<26}  {'J-Prime (14B L4)':<20}  {'Doubleword (' + DOUBLEWORD_MODEL.split('/')[-1] + ')'}")
        print(f"  {'──────':<26}  {'────────────────':<20}  {'─' * 30}")
        print(f"  {'Latency / wall time':<26}  {baseline['latency_ms']/1000:>14.1f}s        {wall_elapsed:>8.0f}s (batch)")
        print(f"  {'Output tokens':<26}  {baseline['tokens']:>18,}    {out_t:>12,}")
        print(f"  {'Input tokens':<26}  {'N/A':>18}    {in_t:>12,}")
        print(f"  {'Cost this request':<26}  ${jp:>18.6f}    ${cost:>12.8f}")
        print(f"  {'Finish reason':<26}  {'stop':>18}    {finish_reason:>12}")
        if finish_reason == "length":
            print(f"  {'':26}  ⚠  finish=length: reasoning model — increase max_tokens")
        if content:
            print(f"\n  Output preview: {content[:200]}...")
        elif reasoning:
            print(f"\n  Reasoning preview: {reasoning[:200]}...")
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    if len(task_results) == len(TASKS):
        sep("═")
        print("  BENCHMARK SUMMARY")
        sep("═")

        total_dw = sum(v["total_cost_usd"] for v in task_results.values())
        total_jp = sum(jprime_cost(k) for k in task_results)
        ratio    = total_jp / total_dw if total_dw else 0
        total_dw_tok = sum(v["output_tokens"] for v in task_results.values())
        total_jp_tok = sum(JPRIME_BASELINE["tasks"][k]["tokens"] for k in task_results)

        print()
        print(f"  {'Metric':<32}  {'J-Prime (L4)':<18}  {'Doubleword'}")
        print(f"  {'──────':<32}  {'────────────':<18}  {'──────────'}")
        print(f"  {'Model':<32}  {'14B Q4_K_M':>18}  {DOUBLEWORD_MODEL.split('/')[-1]}")
        print(f"  {'Total output tokens':<32}  {total_jp_tok:>18,}  {total_dw_tok:>10,}")
        print(f"  {'Total cost (both tasks)':<32}  ${total_jp:>17.6f}  ${total_dw:>10.8f}")
        print(f"  {'Wall / real-time':<32}  {'~33s (streaming)':>18}  {wall_elapsed:.0f}s (batch)")
        print()
        print(f"  💰 {ratio:.0f}x cheaper per request via Doubleword batch")
        print(f"  ⚡ J-Prime {wall_elapsed/33:.1f}x faster for real-time streaming")
        print(f"  🧠 {DOUBLEWORD_MODEL.split('/')[-1]} vs 14B — larger context + reasoning")
        print()

        # Monthly projection
        ops = 100
        print(f"  ─── Projected monthly cost ({ops} complex ops/day) ───────────────")
        print(f"  Doubleword (pay-per-token)  : ${total_dw * ops * 30:.4f}/mo")
        print(f"  J-Prime (6hr/day spot VM)   : ${1.20 * 6 * 30:.2f}/mo")
        print()

        # Save results
        out = {
            "run_at":            datetime.now(timezone.utc).isoformat(),
            "batch_id":          batch_id,
            "input_file_id":     file_id,
            "output_file_id":    output_file_id,
            "doubleword_model":  DOUBLEWORD_MODEL,
            "jprime_model":      JPRIME_BASELINE["model"],
            "jprime_compute":    JPRIME_BASELINE["compute"],
            "wall_elapsed_s":    round(wall_elapsed, 1),
            "pricing": {
                "input_cost_per_m":  DW_INPUT_COST_PER_M,
                "output_cost_per_m": DW_OUTPUT_COST_PER_M,
            },
            "tasks":         task_results,
            "jprime_baseline": JPRIME_BASELINE["tasks"],
            "model_catalog": [{"id": m["id"]} for m in models],
            "summary": {
                "total_dw_cost_usd":  total_dw,
                "total_jp_cost_usd":  total_jp,
                "cost_ratio":         round(ratio, 2),
                "cost_savings_pct":   round((1 - total_dw / total_jp) * 100, 1) if total_jp else 0,
                "total_dw_tokens":    total_dw_tok,
                "total_jp_tokens":    total_jp_tok,
                "batch_wall_time_s":  round(wall_elapsed, 1),
            },
        }

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        out_path = results_dir / f"{ts}-UTC.json"
        out_path.write_text(json.dumps(out, indent=2))
        print(f"  Results saved → {out_path}")

    sep("═")
    print()


if __name__ == "__main__":
    main()
