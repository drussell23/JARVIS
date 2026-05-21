#!/usr/bin/env python3
"""DW Multi-Turn Tool-Loop Stress Harness — Hypothesis #3 isolation.

What this is
============
The third and final harness in the §25.2 isolation series. Closes the last
remaining open hypothesis from the Apr 16 benchmark report after the
May 21 single-stream + concurrent-burst tests eliminated #2, #4, #5.

Hypothesis #3 (verbatim from §25.2):

> "Multi-turn tool-loop-specific SSE edge case. Tool-loop generation has
>  distinctive SSE framing (role switches, tool-call deltas, tool-result
>  injection). The 30s no-data window may be too tight for specific
>  transitions within a tool-loop flow."

This harness reproduces production multi-turn tool-loop SSE behavior:

  * Sends OpenAI-compatible ``tools=[...]`` function definitions whose
    names + argument shapes mirror Venom's actual built-in tool surface
    (``read_file``, ``search_code``, ``glob_files``, ``list_dir``).
  * Provokes the model into multi-turn tool use via a research-shape
    prompt that requires multiple tool calls before reaching an answer.
  * Streams every turn's SSE response with the production-faithful
    ``asyncio.wait_for(resp.content.readline(), timeout=30s)`` stall
    detection — same primitive as ``doubleword_provider.py:1812``.
  * Watches inter-chunk gaps across role transitions
    (assistant -> tool_calls -> tool_role -> assistant ...).
  * Async tool execution simulation (``asyncio.sleep``) with realistic
    per-tool latency distributions — never blocks the event loop.

Single-seam discipline
======================
All shared primitives are imported from the burst harness
(``scripts/dw_concurrent_stress.py``):

  * ``_LocalStreamRuptureError``, ``_ProdStreamRuptureError``,
    ``_STREAM_RUPTURE_SOURCE`` — identical failure class to production.
  * ``_env``, ``_env_int``, ``_env_float`` — same env-knob discipline.
  * ``_percentile``, ``_verdict`` — same aggregation primitives.
  * Threaded resolver + connector config — same DNS path as production.

Cost estimate
=============
~$0.01 per multi-turn stream (3 default concurrent streams = ~$0.03).
Trivial for the diagnostic value.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Make the repo root importable + scripts/ importable for sibling-module reuse
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for _p in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# Single-seam reuse of shared primitives from the burst harness.
# This is the production-faithful failure class + helpers.
from dw_concurrent_stress import (  # noqa: E402,F401  # _LocalStreamRuptureError re-exported for shared-seam reuse
    _LocalStreamRuptureError,  # noqa: F401  # single-seam discipline: importing signals reuse contract
    _ProdStreamRuptureError,
    _STREAM_RUPTURE_SOURCE,
    _env,
    _env_int,
    _env_float,
    _percentile,
)

import aiohttp  # noqa: E402


# ---------------------------------------------------------------------------
# Env-driven configuration (no hardcoding)
# ---------------------------------------------------------------------------

CONCURRENCY = _env_int("JARVIS_TOOLLOOP_CONCURRENCY", 3)
MODEL = _env("JARVIS_TOOLLOOP_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8")
MAX_OUTPUT_TOKENS_PER_TURN = _env_int("JARVIS_TOOLLOOP_MAX_OUTPUT_TOKENS", 1200)
MAX_TURNS = _env_int("JARVIS_TOOLLOOP_MAX_TURNS", 6)
PER_CHUNK_TIMEOUT_S = _env_float("JARVIS_TOOLLOOP_PER_CHUNK_TIMEOUT_S", 30.0)
PER_REQUEST_TIMEOUT_S = _env_float("JARVIS_TOOLLOOP_PER_REQUEST_TIMEOUT_S", 180.0)
TEMPERATURE = _env_float("JARVIS_TOOLLOOP_TEMPERATURE", 0.2)
CONNECTOR_LIMIT = _env_int("JARVIS_TOOLLOOP_CONNECTOR_LIMIT", CONCURRENCY + 5)
OUTPUT_DIR = _env("JARVIS_TOOLLOOP_OUTPUT_DIR", os.environ.get("TMPDIR", "/tmp"))

DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
DW_BASE_URL = os.environ.get("DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1")
if DW_BASE_URL.endswith("/v1/"):
    DW_BASE_URL = DW_BASE_URL[:-1]
elif not DW_BASE_URL.endswith("/v1"):
    DW_BASE_URL = DW_BASE_URL.rstrip("/") + "/v1"


# ---------------------------------------------------------------------------
# Tool schemas — names + arg shapes mirror Venom's production tool surface.
# Built as OpenAI-compatible function-call definitions so DW emits native
# tool_calls deltas in the SSE stream (the exact behavior hypothesis #3
# targets). Each tool name + parameter list matches the Venom production
# descriptors in providers.py / tool_executor.py.
# ---------------------------------------------------------------------------

VENOM_FUNCTION_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search the repository for a substring or regex pattern. Returns "
                "up to N matching file:line snippets. Use for locating symbols, "
                "config keys, or call sites before reading any file in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Substring or regex pattern to find.",
                    },
                    "path_glob": {
                        "type": "string",
                        "description": "Optional path glob to scope the search (e.g. 'backend/**/*.py').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matches to return (default 20).",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file from the repository. Returns the file content as text. "
                "Use a line range for large files. Required before edit_file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative path to the file.",
                    },
                    "lines_from": {
                        "type": "integer",
                        "description": "First line to include (1-indexed, optional).",
                    },
                    "lines_to": {
                        "type": "integer",
                        "description": "Last line to include (1-indexed, optional).",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "List files matching a glob pattern across the repository. "
                "Useful for discovering related files (e.g. all tests for a module)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern (e.g. 'tests/**/test_*.py').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum paths to return (default 50).",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": (
                "List the immediate children of a directory in the repository. "
                "Returns file + subdirectory names sorted lexicographically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repository-relative directory path.",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Async tool execution simulation — realistic per-tool latencies, no blocking.
# ---------------------------------------------------------------------------

# Latency distributions matching production observation: search_code is the
# slowest (ripgrep + scoring), read_file is fast (disk read), glob and
# list_dir are very fast. Values are (min_s, max_s) sampled uniformly.
_TOOL_LATENCY_PROFILE: dict[str, tuple[float, float]] = {
    "search_code": (0.18, 0.55),
    "read_file": (0.02, 0.08),
    "glob_files": (0.04, 0.12),
    "list_dir": (0.01, 0.04),
}


async def _simulate_tool_exec(
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[str, float]:
    """Simulate one tool call asynchronously. Returns (result_text, elapsed_s).

    Sleeps within the per-tool latency profile via ``asyncio.sleep`` — never
    blocks the event loop. Result text is synthesized to look like real tool
    output (the model doesn't care about exact content; what matters for
    hypothesis #3 is that the tool-result message gets sent back and the
    next SSE stream starts cleanly).
    """
    lo, hi = _TOOL_LATENCY_PROFILE.get(tool_name, (0.05, 0.20))
    elapsed = random.uniform(lo, hi)
    await asyncio.sleep(elapsed)

    # Synthesize a plausible result that gives the model something to reason
    # over without making the tool result enormous. The model will treat
    # this as ground truth, which is the point — we don't need correctness,
    # we need realistic SSE framing.
    if tool_name == "search_code":
        query = str(arguments.get("query", ""))[:60]
        result = (
            f"Found 3 matches for '{query}':\n"
            f"  backend/core/ouroboros/governance/doubleword_provider.py:1689\n"
            f"  backend/core/ouroboros/governance/providers.py:7449\n"
            f"  backend/core/ouroboros/governance/candidate_generator.py:1097"
        )
    elif tool_name == "read_file":
        path = str(arguments.get("path", "<unknown>"))[:80]
        result = (
            f"# File: {path} (excerpt, ~30 lines)\n"
            f"# This is a synthesized excerpt for the SSE stall test.\n"
            f"# The content here is intentionally short so the model can\n"
            f"# reason over it and either call another tool or conclude.\n\n"
            f"async def _stream_one_request(...):\n"
            f"    line = await asyncio.wait_for(\n"
            f"        resp.content.readline(), timeout=30.0,\n"
            f"    )\n"
            f"    # ... 25 more lines ..."
        )
    elif tool_name == "glob_files":
        pattern = str(arguments.get("pattern", ""))[:60]
        result = (
            f"Matched 4 files for '{pattern}':\n"
            f"  backend/core/ouroboros/governance/doubleword_provider.py\n"
            f"  backend/core/ouroboros/governance/candidate_generator.py\n"
            f"  backend/core/ouroboros/governance/providers.py\n"
            f"  backend/core/ouroboros/governance/brain_selection_policy.yaml"
        )
    elif tool_name == "list_dir":
        path = str(arguments.get("path", "<unknown>"))[:60]
        result = (
            f"Directory listing of {path}:\n"
            f"  doubleword_provider.py\n  providers.py\n  candidate_generator.py\n"
            f"  brain_selection_policy.yaml\n  compaction_caller.py\n  __init__.py"
        )
    else:
        result = f"(tool '{tool_name}' returned synthetic placeholder)"

    return result, elapsed


# ---------------------------------------------------------------------------
# The provoking research prompt — designed to force multi-turn tool use.
# ---------------------------------------------------------------------------

RESEARCH_PROMPT = (
    "You are a senior systems architect investigating a real engineering "
    "question in this codebase. You have four tools available. "
    "Use them in sequence — not in a single batch — to do this:\n\n"
    "1. Use `search_code` to find every place the 30-second SSE per-chunk "
    "timeout is configured in the streaming provider code.\n"
    "2. Then use `read_file` to read the most relevant file you found.\n"
    "3. Then use `glob_files` to find related test files for the streaming "
    "behavior.\n"
    "4. Then summarize what you discovered in 4-6 sentences. Do not call "
    "more tools after the summary.\n\n"
    "Reason carefully between tool calls. When you have all you need, "
    "produce the final summary as your assistant message and stop."
)


# ---------------------------------------------------------------------------
# Per-turn + per-stream telemetry dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TurnResult:
    turn_index: int
    start_time: float
    end_time: float = 0.0
    duration_s: float = 0.0
    chunks: int = 0
    content_chunks: int = 0
    reasoning_chunks: int = 0
    tool_call_chunks: int = 0
    role: str = "assistant"   # role of model's output
    inter_chunk_gaps_s: list[float] = field(default_factory=list)
    max_gap_s: float = 0.0
    finish_reason: str = ""
    tool_calls_emitted: list[dict[str, Any]] = field(default_factory=list)
    error_class: str = ""
    error_msg: str = ""
    stalled: bool = False
    completed: bool = False
    http_status: int = 0


@dataclass
class ToolLoopStreamResult:
    stream_id: int
    start_time: float
    end_time: float = 0.0
    duration_s: float = 0.0
    turns: list[TurnResult] = field(default_factory=list)
    total_tool_calls_simulated: int = 0
    total_tool_simulation_time_s: float = 0.0
    completed: bool = False
    stalled: bool = False
    max_turns_hit: bool = False
    error_class: str = ""
    error_msg: str = ""

    @property
    def total_chunks(self) -> int:
        return sum(t.chunks for t in self.turns)

    @property
    def all_gaps(self) -> list[float]:
        out: list[float] = []
        for t in self.turns:
            out.extend(t.inter_chunk_gaps_s)
        return out

    @property
    def max_gap_s(self) -> float:
        gaps = self.all_gaps
        return max(gaps) if gaps else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "stream_id": self.stream_id,
            "completed": self.completed,
            "stalled": self.stalled,
            "max_turns_hit": self.max_turns_hit,
            "n_turns": len(self.turns),
            "total_chunks": self.total_chunks,
            "duration_s": self.duration_s,
            "tool_calls": self.total_tool_calls_simulated,
            "tool_sim_time_s": self.total_tool_simulation_time_s,
            "max_gap_s": self.max_gap_s,
            "error_class": self.error_class,
            "error_msg": self.error_msg,
            "turns": [
                {
                    "i": t.turn_index,
                    "dur_s": t.duration_s,
                    "chunks": t.chunks,
                    "content": t.content_chunks,
                    "reasoning": t.reasoning_chunks,
                    "tool_call_chunks": t.tool_call_chunks,
                    "finish_reason": t.finish_reason,
                    "max_gap_s": t.max_gap_s,
                    "tool_calls": [tc["function"]["name"] for tc in t.tool_calls_emitted],
                    "stalled": t.stalled,
                }
                for t in self.turns
            ],
        }


# ---------------------------------------------------------------------------
# Single-turn SSE consumer (production-faithful) — emits a TurnResult.
# ---------------------------------------------------------------------------

async def _stream_one_turn(
    session: aiohttp.ClientSession,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    turn_index: int,
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
) -> TurnResult:
    """Send one /v1/chat/completions request and stream the SSE.

    Returns a TurnResult capturing every chunk + tool-call delta + role
    transition observed during this turn. Uses the same readline + wait_for
    primitive as production (doubleword_provider.py:1812).
    """
    result = TurnResult(turn_index=turn_index, start_time=time.monotonic())

    headers = {
        "Authorization": f"Bearer {DW_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    body: dict[str, Any] = {
        "model": MODEL,
        "stream": True,
        "temperature": TEMPERATURE,
        "max_tokens": MAX_OUTPUT_TOKENS_PER_TURN,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }

    url = f"{DW_BASE_URL}/chat/completions"

    # Per-tool-call accumulator: tool_calls deltas arrive incrementally with
    # function.arguments built up token by token. We have to reassemble them
    # using the OpenAI-compat index/id keys.
    pending_tool_calls: dict[int, dict[str, Any]] = {}

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
                err_body = ""
                try:
                    err_body = (await resp.text())[:240]
                except Exception:  # noqa: BLE001
                    pass
                result.error_msg = f"status={resp.status} body={err_body}"
                return result

            last_chunk_time = result.start_time

            while True:
                try:
                    line = await asyncio.wait_for(
                        resp.content.readline(),
                        timeout=per_chunk_timeout_s,
                    )
                except asyncio.TimeoutError:
                    elapsed = time.monotonic() - result.start_time
                    phase = "ttft" if result.chunks == 0 else "inter_chunk"
                    rupture = _ProdStreamRuptureError(
                        provider="doubleword",
                        elapsed_s=elapsed,
                        bytes_received=0,
                        rupture_timeout_s=per_chunk_timeout_s,
                        phase=phase,
                    )
                    result.error_class = type(rupture).__name__
                    result.error_msg = str(rupture)
                    result.stalled = True
                    return result

                if not line:
                    break

                now = time.monotonic()
                gap = now - last_chunk_time
                last_chunk_time = now

                line_str = line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line_str:
                    continue
                if line_str.startswith(":"):
                    # SSE keepalive comment line — record gap but no payload
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
                    if not result.finish_reason:
                        result.finish_reason = "[DONE]"
                    break

                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue

                if result.chunks > 0:
                    result.inter_chunk_gaps_s.append(gap)
                    if gap > result.max_gap_s:
                        result.max_gap_s = gap
                result.chunks += 1

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                ch0 = choices[0]
                delta = ch0.get("delta") or {}

                if delta.get("content"):
                    result.content_chunks += 1
                if delta.get("reasoning") or delta.get("reasoning_content"):
                    result.reasoning_chunks += 1

                # tool_calls deltas — assemble incrementally by index
                tcs = delta.get("tool_calls") or []
                for tc in tcs:
                    idx = tc.get("index", 0)
                    cur = pending_tool_calls.setdefault(
                        idx,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tc.get("id"):
                        cur["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        cur["function"]["name"] = fn["name"]
                    if fn.get("arguments"):
                        cur["function"]["arguments"] += fn["arguments"]
                    result.tool_call_chunks += 1

                fr = ch0.get("finish_reason")
                if fr:
                    result.finish_reason = fr

    except asyncio.TimeoutError:
        result.error_class = "TotalRequestTimeout"
        result.error_msg = f"request exceeded {per_request_timeout_s:.0f}s"
    except aiohttp.ClientError as e:
        result.error_class = type(e).__name__
        result.error_msg = str(e)[:240]
    except Exception as e:  # noqa: BLE001
        result.error_class = type(e).__name__
        result.error_msg = str(e)[:240]
    finally:
        result.end_time = time.monotonic()
        result.duration_s = result.end_time - result.start_time
        # Materialize the assembled tool calls into the result
        for idx in sorted(pending_tool_calls):
            result.tool_calls_emitted.append(pending_tool_calls[idx])

    return result


# ---------------------------------------------------------------------------
# Multi-turn driver — orchestrates one full tool-loop conversation.
# ---------------------------------------------------------------------------

async def _run_tool_loop_stream(
    session: aiohttp.ClientSession,
    stream_id: int,
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
    max_turns: int,
) -> ToolLoopStreamResult:
    """Drive one complete multi-turn tool-loop conversation."""
    stream_result = ToolLoopStreamResult(stream_id=stream_id, start_time=time.monotonic())

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": RESEARCH_PROMPT},
    ]

    try:
        for turn_idx in range(max_turns):
            turn = await _stream_one_turn(
                session,
                messages,
                VENOM_FUNCTION_TOOLS,
                turn_idx,
                per_chunk_timeout_s,
                per_request_timeout_s,
            )
            stream_result.turns.append(turn)

            if turn.stalled:
                stream_result.stalled = True
                stream_result.error_class = turn.error_class
                stream_result.error_msg = turn.error_msg
                break
            if turn.error_class:
                stream_result.error_class = turn.error_class
                stream_result.error_msg = turn.error_msg
                break

            if turn.finish_reason in ("stop", "length"):
                stream_result.completed = True
                break

            if turn.finish_reason == "tool_calls" and turn.tool_calls_emitted:
                # Add the assistant message with tool_calls to history,
                # then execute each tool asynchronously and add the
                # corresponding tool-role messages — production role
                # transition shape.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": turn.tool_calls_emitted,
                }
                messages.append(assistant_msg)

                # Execute all tool calls in parallel — async, no blocking
                async def _exec(tc: dict[str, Any]) -> tuple[dict[str, Any], float]:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    res_text, elapsed = await _simulate_tool_exec(name, args)
                    return (
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"] or f"call_{stream_id}_{turn_idx}_{name}",
                            "content": res_text,
                        },
                        elapsed,
                    )

                tool_msgs_with_timing = await asyncio.gather(
                    *[_exec(tc) for tc in turn.tool_calls_emitted]
                )
                for msg, elapsed in tool_msgs_with_timing:
                    messages.append(msg)
                    stream_result.total_tool_calls_simulated += 1
                    stream_result.total_tool_simulation_time_s += elapsed

                continue

            # Unknown / unexpected finish_reason — treat as termination
            stream_result.completed = bool(turn.completed)
            break
        else:
            # Hit max_turns without natural termination
            stream_result.max_turns_hit = True
    except Exception as e:  # noqa: BLE001
        stream_result.error_class = type(e).__name__
        stream_result.error_msg = str(e)[:240]
    finally:
        stream_result.end_time = time.monotonic()
        stream_result.duration_s = stream_result.end_time - stream_result.start_time

    return stream_result


# ---------------------------------------------------------------------------
# Concurrent runner
# ---------------------------------------------------------------------------

async def _run_burst(
    concurrency: int,
    per_chunk_timeout_s: float,
    per_request_timeout_s: float,
    max_turns: int,
    connector_limit: int,
) -> list[ToolLoopStreamResult]:
    """Launch N concurrent multi-turn tool-loop streams."""
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
                _run_tool_loop_stream(
                    session, i, per_chunk_timeout_s, per_request_timeout_s, max_turns,
                ),
                name=f"tool-loop-{i}",
            )
            for i in range(concurrency)
        ]
        return await asyncio.gather(*tasks)


# ---------------------------------------------------------------------------
# Aggregation + reporting
# ---------------------------------------------------------------------------

def _aggregate(results: list[ToolLoopStreamResult]) -> dict[str, Any]:
    n = len(results)
    completed = [r for r in results if r.completed]
    stalled = [r for r in results if r.stalled]
    errored = [r for r in results if r.error_class and not r.stalled]

    all_gaps: list[float] = []
    all_turn_count: list[int] = []
    all_tool_calls: list[int] = []
    for r in results:
        all_gaps.extend(r.all_gaps)
        all_turn_count.append(len(r.turns))
        all_tool_calls.append(r.total_tool_calls_simulated)

    return {
        "n_streams": n,
        "n_completed": len(completed),
        "n_stalled": len(stalled),
        "n_other_errors": len(errored),
        "success_rate": (len(completed) / n) if n else 0.0,
        "stall_rate": (len(stalled) / n) if n else 0.0,
        "turns_per_stream": {
            "median": statistics.median(all_turn_count) if all_turn_count else 0,
            "max": max(all_turn_count) if all_turn_count else 0,
        },
        "tool_calls_per_stream": {
            "median": statistics.median(all_tool_calls) if all_tool_calls else 0,
            "total": sum(all_tool_calls),
        },
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
    }


def _verdict(agg: dict[str, Any]) -> tuple[str, str]:
    if agg["n_streams"] == 0:
        return "INDETERMINATE", "no streams ran"
    if agg["stall_rate"] > 0.5:
        return "FAIL", f"{agg['n_stalled']}/{agg['n_streams']} multi-turn streams stalled — hypothesis #3 confirmed"
    if agg["stall_rate"] > 0:
        return "PARTIAL_FAIL", f"{agg['n_stalled']}/{agg['n_streams']} multi-turn streams stalled — load-dependent reproduction"
    if agg["n_other_errors"] > 0:
        return "DEGRADED", f"{agg['n_other_errors']}/{agg['n_streams']} non-stall errors"
    if agg["gaps"]["n_over_10s"] > 0:
        return "CONCERNING", f"{agg['gaps']['n_over_10s']} inter-chunk gaps > 10s — no stalls but margins thin"
    if agg["success_rate"] == 1.0 and agg["gaps"]["max_ms"] < 5000:
        return "CLEAN", "all multi-turn streams completed cleanly with max gap < 5s — hypothesis #3 ELIMINATED"
    return "PASS", f"all multi-turn streams completed; max gap {agg['gaps']['max_ms']:.0f} ms"


def _print_report(agg: dict[str, Any], results: list[ToolLoopStreamResult]) -> None:
    print()
    print("=" * 80)
    print(f"Multi-Turn Tool-Loop Stress: concurrency = {len(results)}")
    print("=" * 80)

    print(f"  Streams:           {agg['n_streams']}")
    print(f"  Completed:         {agg['n_completed']} ({100*agg['success_rate']:.1f}%)")
    print(f"  Stalled (rupture): {agg['n_stalled']} ({100*agg['stall_rate']:.1f}%)")
    print(f"  Other errors:      {agg['n_other_errors']}")
    print()
    print(f"  Turns per stream:      median={agg['turns_per_stream']['median']}  max={agg['turns_per_stream']['max']}")
    print(f"  Tool calls (total):    {agg['tool_calls_per_stream']['total']}")

    print()
    print("  Inter-chunk gap distribution (across ALL turns of ALL streams):")
    g = agg["gaps"]
    print(f"    count={g['count']}  mean={g['mean_ms']:.1f}ms  median={g['median_ms']:.1f}ms")
    print(f"    p95={g['p95_ms']:.1f}ms  p99={g['p99_ms']:.1f}ms  max={g['max_ms']:.1f}ms")
    print(f"    gaps > 1s:  {g['n_over_1s']}")
    print(f"    gaps > 5s:  {g['n_over_5s']}")
    print(f"    gaps > 10s: {g['n_over_10s']}")
    print(f"    gaps > 30s: {g['n_over_30s']}    ← production stall threshold")

    print()
    print("  Per-stream summary:")
    print(f"    {'#':>3}  {'status':>10}  {'turns':>5}  {'tools':>5}  {'dur':>7}  {'max_gap':>8}  finish_chain")
    for r in sorted(results, key=lambda x: x.stream_id):
        if r.stalled:
            status = "STALLED"
        elif r.completed:
            status = "OK"
        elif r.max_turns_hit:
            status = "MAX_TURNS"
        elif r.error_class:
            status = r.error_class[:10]
        else:
            status = "PARTIAL"
        chain = " > ".join(t.finish_reason or "?" for t in r.turns)
        print(
            f"    {r.stream_id:>3}  {status:>10}  {len(r.turns):>5}  "
            f"{r.total_tool_calls_simulated:>5}  {r.duration_s:>6.2f}s  "
            f"{r.max_gap_s*1000:>7.0f}ms  {chain[:60]}"
        )

    print()
    print("  Per-turn detail (every turn of every stream):")
    print(f"    {'stream':>6}  {'turn':>4}  {'dur':>6}  {'chunks':>6}  {'tool_chunks':>11}  {'max_gap':>8}  finish_reason")
    for r in sorted(results, key=lambda x: x.stream_id):
        for t in r.turns:
            tools_called = ",".join(tc["function"]["name"] for tc in t.tool_calls_emitted) or "-"
            print(
                f"    {r.stream_id:>6}  {t.turn_index:>4}  {t.duration_s:>5.2f}s  "
                f"{t.chunks:>6}  {t.tool_call_chunks:>11}  "
                f"{t.max_gap_s*1000:>7.0f}ms  {(t.finish_reason or '-'):>13}  [{tools_called}]"
            )

    print()
    v, why = _verdict(agg)
    print(f"  VERDICT: {v}")
    print(f"  WHY:     {why}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _amain() -> int:
    if not DW_API_KEY:
        print("ERROR: DOUBLEWORD_API_KEY not set in environment", file=sys.stderr)
        return 2

    print("=" * 80)
    print("DW Multi-Turn Tool-Loop Stress Harness")
    print("=" * 80)
    print(f"  Endpoint:                  {DW_BASE_URL}/chat/completions")
    print(f"  Model:                     {MODEL}")
    print(f"  Concurrency:               {CONCURRENCY} concurrent tool-loop conversations")
    print(f"  Max turns per stream:      {MAX_TURNS}")
    print(f"  Per-chunk timeout:         {PER_CHUNK_TIMEOUT_S}s   (prod default: 30.0s)")
    print(f"  Per-request wall-clock:    {PER_REQUEST_TIMEOUT_S}s")
    print(f"  Max output tokens / turn:  {MAX_OUTPUT_TOKENS_PER_TURN}")
    print(f"  Temperature:               {TEMPERATURE}")
    print(f"  Tools exposed:             {[t['function']['name'] for t in VENOM_FUNCTION_TOOLS]}")
    print(f"  StreamRuptureError source: {_STREAM_RUPTURE_SOURCE}")
    print()

    overall_start = time.monotonic()
    results = await _run_burst(
        CONCURRENCY,
        PER_CHUNK_TIMEOUT_S,
        PER_REQUEST_TIMEOUT_S,
        MAX_TURNS,
        CONNECTOR_LIMIT,
    )
    overall_dur = time.monotonic() - overall_start

    agg = _aggregate(results)
    _print_report(agg, results)

    print()
    print(f"  Total harness wall-clock: {overall_dur:.2f}s")

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"dw_tool_loop_stress_{ts}.json"
    archive = {
        "harness": "dw_tool_loop_stress.py",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "endpoint": f"{DW_BASE_URL}/chat/completions",
        "model": MODEL,
        "concurrency": CONCURRENCY,
        "max_turns": MAX_TURNS,
        "per_chunk_timeout_s": PER_CHUNK_TIMEOUT_S,
        "per_request_timeout_s": PER_REQUEST_TIMEOUT_S,
        "stream_rupture_source": _STREAM_RUPTURE_SOURCE,
        "tools": [t["function"]["name"] for t in VENOM_FUNCTION_TOOLS],
        "overall_wall_clock_s": overall_dur,
        "aggregate": agg,
        "streams": [r.summary() for r in results],
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
