#!/usr/bin/env python3
"""
Trinity AI — Doubleword Vision/OCR Model Benchmark
===================================================
Tests all available vision and OCR models against a live screenshot.
Measures latency, output quality, and coordinate extraction capability.

Usage:
    export DOUBLEWORD_API_KEY=sk-...
    python3 benchmarks/doubleword/benchmark_vision.py

    # Use a specific screenshot (default: captures current screen)
    VISION_INPUT=/path/to/screenshot.png python3 ...

    # Run N iterations per model for latency averaging
    VISION_ITERATIONS=3 python3 ...

Outputs:
    benchmarks/doubleword/results/vision-<timestamp>-UTC.json
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Configuration ────────────────────────────────────────────────────────────

DOUBLEWORD_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
DOUBLEWORD_BASE = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
ITERATIONS = int(os.environ.get("VISION_ITERATIONS", "2"))

# All vision/OCR models in the Doubleword catalog
VISION_MODELS = [
    {
        "id": "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
        "label": "Qwen3-VL-235B (deep vision)",
        "params": "235B",
        "active": "~22B",
        "type": "vision",
    },
    {
        "id": "Qwen/Qwen3-VL-30B-A3B-Instruct-FP8",
        "label": "Qwen3-VL-30B (fast vision)",
        "params": "30B",
        "active": "~3B",
        "type": "vision",
    },
    {
        "id": "deepseek-ai/DeepSeek-OCR-2",
        "label": "DeepSeek-OCR-2",
        "params": "unknown",
        "active": "unknown",
        "type": "ocr",
    },
    {
        "id": "allenai/olmOCR-2-7B-1025-FP8",
        "label": "olmOCR-2-7B (document OCR)",
        "params": "7B",
        "active": "7B",
        "type": "ocr",
    },
    {
        "id": "lightonai/LightOnOCR-2-1B-bbox-soup",
        "label": "LightOnOCR-1B (bbox coords)",
        "params": "1B",
        "active": "1B",
        "type": "ocr",
    },
]

# Two prompts to test different capabilities
PROMPTS = {
    "describe": {
        "system": (
            "You are a vision assistant. Describe what you see on screen in detail. "
            "Read ALL visible text and numbers exactly. Note positions of UI elements. "
            "Be precise and concise."
        ),
        "user": "Describe everything visible on this screen. Read all text and numbers exactly.",
        "max_tokens": 500,
    },
    "coordinates": {
        "system": (
            "You are a UI element detector. For each clickable element visible on screen, "
            "return its approximate pixel coordinates as (x, y) and a label. "
            "Return JSON array: [{\"label\": \"...\", \"x\": N, \"y\": N}, ...]"
        ),
        "user": "List all clickable UI elements with their pixel coordinates. Return JSON only.",
        "max_tokens": 800,
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def capture_screenshot() -> str:
    """Capture screen and return base64 PNG."""
    custom = os.environ.get("VISION_INPUT")
    if custom and Path(custom).exists():
        print(f"  Using custom screenshot: {custom}")
        return base64.b64encode(Path(custom).read_bytes()).decode()

    tmp = "/tmp/vision_benchmark_input.png"
    subprocess.run(["screencapture", "-x", "-t", "png", tmp],
                   capture_output=True, timeout=5)
    if not Path(tmp).exists():
        print("  ERROR: screencapture failed")
        sys.exit(1)
    size = Path(tmp).stat().st_size
    print(f"  Screenshot captured: {size:,} bytes")
    return base64.b64encode(Path(tmp).read_bytes()).decode()


async def call_vision_model(
    session: Any,
    model_id: str,
    b64_png: str,
    prompt_key: str,
    timeout_s: float = 30,
) -> Dict[str, Any]:
    """Call a single vision model and return timing + output."""
    prompt = PROMPTS[prompt_key]

    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": prompt["system"]},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_png}"}},
                {"type": "text", "text": prompt["user"]},
            ]},
        ],
        "max_tokens": prompt["max_tokens"],
        "temperature": 0.1,
    }

    t0 = time.perf_counter()
    try:
        import aiohttp
        async with session.post(
            f"{DOUBLEWORD_BASE}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {DOUBLEWORD_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            elapsed = time.perf_counter() - t0
            if resp.status != 200:
                body = await resp.text()
                return {
                    "status": resp.status,
                    "error": body[:300],
                    "latency_s": round(elapsed, 2),
                    "content": "",
                }
            data = await resp.json()
            choices = data.get("choices", [])
            content = ""
            if choices:
                content = choices[0].get("message", {}).get("content", "") or ""
            usage = data.get("usage", {})
            return {
                "status": 200,
                "latency_s": round(elapsed, 2),
                "content": content,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "finish_reason": choices[0].get("finish_reason", "unknown") if choices else "no_choices",
            }
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "error": f"Timed out after {timeout_s}s",
            "latency_s": round(time.perf_counter() - t0, 2),
            "content": "",
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc)[:300],
            "latency_s": round(time.perf_counter() - t0, 2),
            "content": "",
        }


# ── Main ─────────────────────────────────────────────────────────────────────

async def main() -> None:
    print()
    print("=" * 70)
    print("  Trinity AI — Doubleword Vision/OCR Model Benchmark")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 70)

    if not DOUBLEWORD_API_KEY:
        print("\n  DOUBLEWORD_API_KEY not set.\n")
        sys.exit(1)

    # Capture screenshot
    print("\n  STEP 1 — Capture screenshot")
    print("-" * 70)
    b64_png = capture_screenshot()
    img_kb = len(b64_png) * 3 // 4 // 1024
    print(f"  Image size: ~{img_kb} KB (base64)")

    import aiohttp
    async with aiohttp.ClientSession() as session:

        all_results: Dict[str, Any] = {}

        for model in VISION_MODELS:
            model_id = model["id"]
            label = model["label"]

            print(f"\n{'─' * 70}")
            print(f"  Model: {label}")
            print(f"  ID: {model_id}")
            print(f"  Type: {model['type']} | Params: {model['params']} | Active: {model['active']}")
            print(f"{'─' * 70}")

            model_results: Dict[str, Any] = {
                "model_id": model_id,
                "label": label,
                "type": model["type"],
                "params": model["params"],
                "active": model["active"],
                "prompts": {},
            }

            for prompt_key in ["describe", "coordinates"]:
                print(f"\n  Prompt: {prompt_key} ({ITERATIONS} iterations)")

                iterations: List[Dict[str, Any]] = []
                for i in range(ITERATIONS):
                    result = await call_vision_model(
                        session, model_id, b64_png, prompt_key,
                        timeout_s=60,
                    )
                    status = result.get("status", "unknown")
                    latency = result.get("latency_s", 0)
                    content = result.get("content", "")

                    if status == 200:
                        preview = content[:120].replace("\n", " ")
                        print(f"    [{i+1}] {latency:.1f}s — {preview}...")
                    else:
                        error = result.get("error", "unknown")[:80]
                        print(f"    [{i+1}] {latency:.1f}s — FAILED ({status}: {error})")

                    iterations.append(result)

                # Compute summary stats
                successes = [r for r in iterations if r.get("status") == 200]
                latencies = [r["latency_s"] for r in successes]

                model_results["prompts"][prompt_key] = {
                    "iterations": iterations,
                    "success_rate": len(successes) / len(iterations) if iterations else 0,
                    "avg_latency_s": round(sum(latencies) / len(latencies), 2) if latencies else None,
                    "min_latency_s": round(min(latencies), 2) if latencies else None,
                    "max_latency_s": round(max(latencies), 2) if latencies else None,
                    "best_content": successes[0]["content"] if successes else "",
                }

            all_results[model_id] = model_results

        # ── Summary ───────────────────────────────────────────────────────
        print(f"\n{'=' * 70}")
        print("  VISION BENCHMARK SUMMARY")
        print(f"{'=' * 70}")
        print()
        print(f"  {'Model':<35} {'Describe':<12} {'Coords':<12} {'Type'}")
        print(f"  {'─' * 35} {'─' * 12} {'─' * 12} {'─' * 8}")

        for model in VISION_MODELS:
            mid = model["id"]
            mr = all_results.get(mid, {})
            desc = mr.get("prompts", {}).get("describe", {})
            coords = mr.get("prompts", {}).get("coordinates", {})
            desc_lat = desc.get("avg_latency_s")
            coords_lat = coords.get("avg_latency_s")
            desc_str = f"{desc_lat:.1f}s" if desc_lat else "FAIL"
            coords_str = f"{coords_lat:.1f}s" if coords_lat else "FAIL"
            print(f"  {model['label']:<35} {desc_str:<12} {coords_str:<12} {model['type']}")

        print()

        # Save results
        out = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "iterations_per_prompt": ITERATIONS,
            "screenshot_kb": img_kb,
            "models": all_results,
        }

        results_dir = Path(__file__).parent / "results"
        results_dir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
        out_path = results_dir / f"vision-{ts}-UTC.json"
        out_path.write_text(json.dumps(out, indent=2))
        print(f"  Results saved -> {out_path}")
        print("=" * 70)
        print()


if __name__ == "__main__":
    asyncio.run(main())
