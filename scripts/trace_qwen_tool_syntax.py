#!/usr/bin/env python3
"""Slice 45 Phase 1 — out-of-band Qwen tool-syntax bisection diagnostic.

WHY THIS EXISTS
---------------
v40b (bt-2026-05-29-200702) produced the arc's first DW candidate but the
Iron Gate rejected it: ``exploration_insufficient: 0/1`` — the model made
**0 tool calls**. Code reading found the root cause is a *predicate
mismatch*, not a parse bug:

  * PROMPT layer  (providers._build_tool_section, providers.py:1745):
        ``should_skip_venom_for_route("background")`` -> returns ""  ->
        the model is NEVER shown the 2b.2-tool schema or the tool list.
  * EXEC  layer  (doubleword_provider.py:2632):
        ``_skip_tools = _complexity == "trivial"``  ->  non-trivial
        background still RUNS the Venom tool loop -> parse_fn is called
        on output the model was never instructed to shape -> None every
        round -> 0 tool calls -> Iron Gate 0/1 -> deadlock.

This script settles the FIX SHAPE empirically before any production edit.
The pipeline never sends a native OpenAI ``tools=[]`` array (the protocol
is *prompt-embedded*), so the model can only signal tool intent **in its
content text** — inspecting ``choices[0].message.content`` is sufficient
and correct to bisect Branch A vs B.

It sends a real codegen-style prompt through the Doubleword **Batch API**
lane (``prompt_only`` -> upload -> create -> poll -> retrieve) under two
conditions:

  COND_A  "tools_advertised"  — prompt INCLUDES the 2b.2-tool section
          (what we would send if we align the background predicate).
  COND_B  "tools_suppressed"  — prompt OMITS the tool section (exactly
          what the background route sends TODAY; the control).

VERDICTS
--------
  PREDICATE_FIX_SUFFICIENT  Qwen emits a parseable 2b.2-tool envelope when
                            advertised -> aligning the predicate is enough.
  NEEDS_PARSE_ADAPTER       Qwen emits tool intent in a NON-2b.2 shape
                            (markdown-wrapped / native tool_calls / alt
                            JSON / <tool_call> tags) -> ALSO build a
                            translation adapter (runbook Branch A).
  BEHAVIORAL_SKIP           Qwen emits a patch with no tool intent even
                            when advertised -> exploration-enforcement
                            prompt modifier needed (runbook Branch B).

This is a READ-ONLY diagnostic. It imports production helpers but mutates
no production code paths and writes only under a trace output directory.

Usage:
    python3 scripts/trace_qwen_tool_syntax.py [--cond both|a|b] [--model SLUG]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency). Does not override existing env."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


# ── tool-intent modality classifiers ───────────────────────────────────
# Each returns the matched snippet (truncated) or "" if no match.

def _find_2b2_envelope(text: str) -> str:
    m = re.search(r'\{\s*"schema_version"\s*:\s*"2b\.2-tool".*?"tool_call', text, re.DOTALL)
    return text[m.start():m.start() + 160] if m else ""


def _find_markdown_tool_json(text: str) -> str:
    # ```json ... {"tool_call"/"tool_calls"/"name"+"arguments"} ... ```
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        blk = m.group(1)
        if re.search(r'"tool_calls?"\s*:', blk) or (
            '"name"' in blk and '"arguments"' in blk
        ):
            return blk[:200]
    return ""


def _find_native_toolcalls(text: str) -> str:
    # OpenAI-native object leaked into content, or a bare tool_calls array.
    m = re.search(r'"tool_calls"\s*:\s*\[', text)
    if m:
        return text[m.start():m.start() + 160]
    m = re.search(r'"function"\s*:\s*\{\s*"name"', text)
    return text[m.start():m.start() + 160] if m else ""


def _find_xml_toolcall(text: str) -> str:
    # Qwen/Hermes-style <tool_call>...</tool_call> or <function=...>
    m = re.search(r"<tool_call>.*?</tool_call>", text, re.DOTALL)
    if m:
        return m.group(0)[:200]
    m = re.search(r"<function[ =].*?>", text)
    return m.group(0)[:160] if m else ""


def _find_alt_json_toolish(text: str) -> str:
    # Any JSON object carrying name+arguments but NOT our schema_version.
    for m in re.finditer(r"\{[^{}]*\"name\"[^{}]*\"arguments\"[^{}]*\}", text, re.DOTALL):
        blk = m.group(0)
        if "2b.2-tool" not in blk:
            return blk[:200]
    return ""


def _looks_like_patch(text: str) -> bool:
    return bool(
        re.search(r"^\s*(diff --git|--- a/|\+\+\+ b/|@@ )", text, re.MULTILINE)
        or re.search(r"```(?:diff|python|py)\b", text)
        or '"file_path"' in text
        or '"full_content"' in text
    )


def classify(content: str, parser_caught: bool) -> Dict[str, Any]:
    """Classify the model's tool-call modality from raw content."""
    hits = {
        "envelope_2b2": _find_2b2_envelope(content),
        "markdown_tool_json": _find_markdown_tool_json(content),
        "native_tool_calls": _find_native_toolcalls(content),
        "xml_tool_call": _find_xml_toolcall(content),
        "alt_json_toolish": _find_alt_json_toolish(content),
    }
    any_alt_intent = any(
        hits[k] for k in ("markdown_tool_json", "native_tool_calls", "xml_tool_call", "alt_json_toolish")
    )
    if parser_caught and hits["envelope_2b2"]:
        verdict = "PREDICATE_FIX_SUFFICIENT"
    elif hits["envelope_2b2"] and not parser_caught:
        # Envelope present but parser missed it (e.g. nested/markdown-wrapped).
        verdict = "NEEDS_PARSE_ADAPTER"
    elif any_alt_intent:
        verdict = "NEEDS_PARSE_ADAPTER"
    elif _looks_like_patch(content):
        verdict = "BEHAVIORAL_SKIP"
    else:
        verdict = "INCONCLUSIVE"
    return {
        "verdict": verdict,
        "parser_caught": parser_caught,
        "hits": {k: (v[:200] if v else "") for k, v in hits.items()},
        "any_alt_intent": any_alt_intent,
        "looks_like_patch": _looks_like_patch(content),
    }


async def run_condition(
    label: str,
    prompt: str,
    model: Optional[str],
) -> Dict[str, Any]:
    from backend.core.ouroboros.governance.doubleword_provider import DoublewordProvider

    provider = DoublewordProvider()
    # _parse_tool_call_response is defined inline inside generate(); replicate
    # its EXACT regex+brace-scan here so the diagnostic mirrors production.
    def _parse_tool_call_response(raw: str) -> bool:
        m = re.search(r'\{\s*"schema_version"\s*:\s*"2b\.2-tool".*?"tool_call', raw, re.DOTALL)
        if not m:
            return False
        brace = 0
        start = m.start()
        for i in range(start, len(raw)):
            if raw[i] == "{":
                brace += 1
            elif raw[i] == "}":
                brace -= 1
                if brace == 0:
                    try:
                        obj = json.loads(raw[start:i + 1])
                    except json.JSONDecodeError:
                        return False
                    if isinstance(obj.get("tool_calls"), list) and obj["tool_calls"]:
                        return True
                    return bool(obj.get("tool_call", {}).get("name"))
        return False

    print(f"\n{'='*70}\n[{label}] sending batch completion ({len(prompt)} chars, model={model or 'default'})...", flush=True)
    t0 = time.monotonic()
    err = ""
    content = ""
    try:
        content = await provider.prompt_only(
            prompt,
            model=model,
            caller_id=f"slice45_trace_{label}",
            max_tokens=2048,
        )
    except Exception as exc:  # noqa: BLE001 — diagnostic must never crash silently
        err = f"{type(exc).__name__}: {exc}"
    dt = time.monotonic() - t0
    parser_caught = _parse_tool_call_response(content) if content else False
    result = {
        "label": label,
        "elapsed_s": round(dt, 1),
        "error": err,
        "content_len": len(content),
        "content": content,
        "classification": classify(content, parser_caught) if content else {
            "verdict": "NO_RESPONSE", "parser_caught": False, "hits": {},
        },
    }
    v = result["classification"]["verdict"]
    print(f"[{label}] {dt:.1f}s  len={len(content)}  verdict={v}  err={err or '-'}", flush=True)
    if content:
        head = content[:600].replace("\n", "\n  ")
        print(f"[{label}] content head:\n  {head}\n  ...", flush=True)
    return result


def build_prompts() -> Tuple[str, str]:
    """Return (tools_advertised_prompt, tools_suppressed_prompt)."""
    from backend.core.ouroboros.governance.providers import _build_tool_section

    # A real, small, exploration-warranting task against our own codebase.
    task = (
        "TASK: Add a `__repr__` method to the `SurfaceHealthRecord` dataclass "
        "in `backend/core/ouroboros/governance/dw_surface_health.py` that returns "
        "a compact one-line summary of its fields.\n\n"
        "You do NOT have the file contents. Before proposing any patch you must "
        "investigate the codebase to read the dataclass definition.\n"
    )
    # COND_A: full tool section as a NON-skip route would advertise it.
    tool_section = _build_tool_section(mcp_tools=None, provider_route="standard")
    cond_a = task + "\n" + tool_section
    # COND_B: exactly what background route sends today (tool section == "").
    suppressed = _build_tool_section(mcp_tools=None, provider_route="background")
    cond_b = task + "\n" + suppressed
    return cond_a, cond_b


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cond", choices=["both", "a", "b"], default="both")
    ap.add_argument("--model", default=None, help="override DW model slug")
    ap.add_argument("--aegis", choices=["on", "off"], default="off",
                    help="off = direct to api.doubleword.ai using DOUBLEWORD_API_KEY")
    args = ap.parse_args()

    _load_dotenv(REPO_ROOT / ".env")
    # Direct path by default — out-of-band, no daemon dependency.
    if args.aegis == "off":
        os.environ["JARVIS_AEGIS_ENABLED"] = "false"

    cond_a, cond_b = build_prompts()
    print(f"COND_A tools_advertised prompt: {len(cond_a)} chars "
          f"(tool_section present={'## Available Tools' in cond_a})")
    print(f"COND_B tools_suppressed prompt: {len(cond_b)} chars "
          f"(tool_section present={'## Available Tools' in cond_b})")

    results: List[Dict[str, Any]] = []
    if args.cond in ("both", "a"):
        results.append(await run_condition("tools_advertised", cond_a, args.model))
    if args.cond in ("both", "b"):
        results.append(await run_condition("tools_suppressed", cond_b, args.model))

    out_dir = REPO_ROOT / ".ouroboros" / "slice45_trace"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(int(time.time()))
    report = {
        "stamp": stamp,
        "model": args.model or os.environ.get("DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8"),
        "results": results,
    }
    report_path = out_dir / f"trace_{stamp}.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{'='*70}\nSUMMARY")
    for r in results:
        print(f"  {r['label']:18s} verdict={r['classification']['verdict']:24s} "
              f"parser_caught={r['classification'].get('parser_caught')} "
              f"len={r['content_len']} err={r['error'] or '-'}")
    print(f"\nFull report (incl. raw content): {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
