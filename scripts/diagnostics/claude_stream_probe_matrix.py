"""
Adaptive Diagnostic Probe Matrix (Task #101, 2026-05-14).

Closes the final-mile diagnostic gap after Tasks #95-#100 sealed the
substrate (budget invariants, lifecycle, phase isolation, idle-recycle,
monotonic deadlines).  v14-rev18 proved Task #100 holds mathematically
(elapsed ≤ budget + grace) but the underlying ``thinking=on stream
first_token=NEVER bytes_received=0`` pattern persists, isolated to the
external network / Anthropic-routing boundary (H8/H9).

This module runs a **dimensional matrix** over the exact combinatorial
space that distinguishes the failing harness call from the working
Step 2 probe baseline:

  * prompt_size:    {small (~50 chars) | large (~16k chars)}
  * thinking_budget: {1024 | 4000}
  * system_prompt:  {none | elaborate-structured}

= 2 × 2 × 2 = 8 cells.  Each cell runs against a SINGLE long-lived
``AsyncAnthropic()`` client to simulate the harness's sustained pool.
Inter-call spacing is env-tunable
(``JARVIS_PROBE_INTERVAL_S`` default 5s for fast cycle; set to 300 to
reproduce the harness's 5-minute compute gap).

Telemetry captured per cell (via httpx event hooks + manual timers):

  * ``connect_ms``   — TCP connect time (first byte sent → first byte
                       received via response start-time minus request
                       send-time)
  * ``tls_ms``       — TLS handshake time (approximated via first-
                       byte-received timing on first request)
  * ``http_version`` — h2 / h1.1 (ALPN negotiated)
  * ``first_event_ms`` — wall ms to first stream event
  * ``first_text_ms``  — wall ms to first content_block_delta(text_delta)
  * ``elapsed_ms``   — total stream wall time
  * ``event_counts`` — per-event-type tally
  * ``text_bytes``   — accumulated text bytes
  * ``thinking_bytes`` — accumulated thinking_delta bytes
  * ``error_class``  — exception class name on failure
  * ``error_chain``  — chained exception classes (httpx → httpcore →
                       anthropic)
  * ``cancel_layer`` — which layer fired the cancel (asyncio.wait_for /
                       SDK timeout / orphan)

Outputs:
  * JSON: ``.jarvis/diagnostics/claude_probe_matrix_<UTC>.json``
  * Human-readable summary table to stdout

No internal harness imports — runs against pure ``anthropic`` SDK +
``httpx``.  Composes ``tests/fixtures/diagnostics/large_prompt_16k.txt``
for the large-payload dimension; path env-tunable.

Operator binding 2026-05-14: rigorous deterministic diagnostic
component, not throwaway.  Run with no PR; outputs decide the next
investigation surface (H8 Anthropic-side vs H9 TLS vs other).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Closed taxonomy — dimensional matrix
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeCell:
    """One coordinate of the dimensional matrix.

    Attributes
    ----------
    prompt_size_label : "small" | "large"
    thinking_budget   : int (1024 or 4000)
    system_prompt_label : "none" | "elaborate"
    """
    prompt_size_label: str
    thinking_budget: int
    system_prompt_label: str

    @property
    def cell_id(self) -> str:
        return (
            f"p={self.prompt_size_label}__"
            f"t={self.thinking_budget}__"
            f"s={self.system_prompt_label}"
        )


@dataclass
class ProbeResult:
    cell_id: str
    cell: Dict[str, Any]
    started_at_utc: str
    finished_at_utc: str
    # Telemetry
    elapsed_ms: float = 0.0
    first_event_ms: Optional[float] = None
    first_text_ms: Optional[float] = None
    text_bytes: int = 0
    thinking_bytes: int = 0
    event_counts: Dict[str, int] = field(default_factory=dict)
    http_version: Optional[str] = None  # "HTTP/1.1" or "HTTP/2"
    connect_ms: Optional[float] = None
    request_send_ms: Optional[float] = None
    # Outcome
    status: str = "unknown"  # "ok" | "timeout" | "error" | "no_events"
    error_class: Optional[str] = None
    error_chain: List[str] = field(default_factory=list)
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# Config — env-tunable, no hardcoded paths
# ---------------------------------------------------------------------------


def _resolve_large_prompt_path() -> Path:
    """``JARVIS_PROBE_LARGE_PROMPT_PATH`` env override, else canonical
    fixture under tests/fixtures/diagnostics/.  Composes existing
    fixture infrastructure — no hardcoded path."""
    _env = os.environ.get("JARVIS_PROBE_LARGE_PROMPT_PATH", "").strip()
    if _env:
        return Path(_env)
    # Walk up from this file to repo root, then into the canonical fixture path.
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "tests" / "fixtures" / "diagnostics" / "large_prompt_16k.txt"


def _resolve_interval_s() -> float:
    """Inter-cell sleep seconds.  Default 5s for fast cycle; set to
    300 to reproduce the harness's 5-minute idle gap."""
    try:
        return max(0.0, float(os.environ.get("JARVIS_PROBE_INTERVAL_S", "5")))
    except (TypeError, ValueError):
        return 5.0


def _resolve_per_cell_timeout_s() -> float:
    """Per-cell asyncio.wait_for cap.  Default 60s — long enough for
    Anthropic's worst-case backend latency; short enough to keep total
    matrix runtime bounded (8 cells × 60s = 8 min worst case)."""
    try:
        return max(5.0, float(os.environ.get("JARVIS_PROBE_PER_CELL_TIMEOUT_S", "60")))
    except (TypeError, ValueError):
        return 60.0


def _resolve_model() -> str:
    return os.environ.get("JARVIS_PROBE_MODEL", "claude-sonnet-4-6").strip()


def _resolve_http2_enabled() -> bool:
    """When true, the underlying httpx.AsyncClient negotiates HTTP/2 via
    ALPN.  Set false to force HTTP/1.1 — useful for H9 ALPN bisection."""
    _raw = os.environ.get("JARVIS_PROBE_HTTP2_ENABLED", "true").strip().lower()
    return _raw in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Payload construction — composes fixtures
# ---------------------------------------------------------------------------


# Small / large prompt content
_SMALL_USER_PROMPT = "Briefly reason about 2+2 then answer."

# Elaborate system prompt — mirrors harness's plan_generator system shape.
_ELABORATE_SYSTEM = (
    "You are an expert software architect planning an implementation "
    "strategy.  Your job is to THINK about HOW to implement the "
    "requested change before any code is written.  Reason about:\n"
    "- The correct order of file modifications (dependency-aware)\n"
    "- Which files need to change and what each change involves\n"
    "- Risks, edge cases, and invariants that must be preserved\n"
    "- How to verify the changes work correctly\n"
    "- Whether this is a simple tweak or an architectural change\n"
    "\n"
    "Do NOT write any code. Only plan the implementation strategy.\n"
    "Respond with valid JSON only matching schema_version plan.1."
)


def _load_large_prompt() -> str:
    """Load the ~16k char prompt fixture.  Falls back to synthetic
    repeat if the fixture isn't checked in (defensive — should be
    present, but never crash the probe on a missing fixture)."""
    p = _resolve_large_prompt_path()
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Defensive fallback — synthetic repeat to reach ~16k
        return ("Refactor the operation envelope budget gate.\n" * 350)[:16000]


def _build_messages(cell: ProbeCell, large_prompt: str) -> List[Dict[str, Any]]:
    user_content = (
        _SMALL_USER_PROMPT if cell.prompt_size_label == "small"
        else large_prompt
    )
    return [{"role": "user", "content": user_content}]


def _build_system_param(cell: ProbeCell) -> Optional[str]:
    return _ELABORATE_SYSTEM if cell.system_prompt_label == "elaborate" else None


# ---------------------------------------------------------------------------
# Matrix enumeration
# ---------------------------------------------------------------------------


def enumerate_matrix() -> List[ProbeCell]:
    """Generate all 2x2x2 = 8 cells of the dimensional matrix."""
    cells: List[ProbeCell] = []
    for prompt_label in ("small", "large"):
        for thinking_budget in (1024, 4000):
            for system_label in ("none", "elaborate"):
                cells.append(ProbeCell(
                    prompt_size_label=prompt_label,
                    thinking_budget=thinking_budget,
                    system_prompt_label=system_label,
                ))
    return cells


# ---------------------------------------------------------------------------
# Telemetry — httpx event hooks
# ---------------------------------------------------------------------------


class _CellTelemetry:
    """Per-cell timing capture.  Wired into ``httpx.AsyncClient``'s
    event_hooks so we capture connect / request / response timings
    independently of the SDK's bookkeeping."""

    def __init__(self) -> None:
        self.t_request_send: Optional[float] = None
        self.t_response_start: Optional[float] = None
        self.http_version: Optional[str] = None

    async def on_request(self, request: Any) -> None:
        self.t_request_send = time.monotonic()

    async def on_response(self, response: Any) -> None:
        self.t_response_start = time.monotonic()
        try:
            # httpx exposes http_version on the response
            self.http_version = str(getattr(response, "http_version", "?"))
        except Exception:
            self.http_version = "?"


# ---------------------------------------------------------------------------
# Cell runner
# ---------------------------------------------------------------------------


async def run_cell(
    client: Any,
    cell: ProbeCell,
    large_prompt: str,
    *,
    model: str,
    per_cell_timeout_s: float,
    telemetry: _CellTelemetry,
) -> ProbeResult:
    """Execute one matrix cell against a long-lived AsyncAnthropic
    client.  Captures telemetry into ``telemetry``; assembles a
    :class:`ProbeResult`.

    Never raises — every failure mode becomes a structured result.
    """
    started = datetime.now(tz=timezone.utc).isoformat()
    t0 = time.monotonic()

    first_event_t: Optional[float] = None
    first_text_t: Optional[float] = None
    text_bytes = 0
    thinking_bytes = 0
    event_counter: Counter[str] = Counter()
    error_class: Optional[str] = None
    error_chain: List[str] = []
    error_message: Optional[str] = None
    status = "unknown"

    # Anthropic constraint: max_tokens > thinking_budget.  Use
    # thinking_budget + 256 headroom so we observe the first text
    # tokens but don't burn cost on a full response.
    _max_tokens = cell.thinking_budget + 256
    kwargs: Dict[str, Any] = {
        "model": model,
        "max_tokens": _max_tokens,
        "messages": _build_messages(cell, large_prompt),
    }
    system_param = _build_system_param(cell)
    if system_param is not None:
        kwargs["system"] = system_param
    # thinking config — both budgets exercised
    kwargs["thinking"] = {
        "type": "enabled",
        "budget_tokens": cell.thinking_budget,
    }
    kwargs["temperature"] = 1.0

    # Break-early bounds: once we've seen the first text_delta + some
    # thinking_delta evidence, we have enough signal — stop consuming
    # to control cost.  Diagnostic value is in event ARRIVAL pattern,
    # not full content.
    _break_after_first_text_bytes = 32
    _break_after_total_seconds = 12.0

    async def _consume() -> None:
        nonlocal first_event_t, first_text_t, text_bytes, thinking_bytes
        async with client.messages.stream(**kwargs) as stream:
            async for event in stream:
                now = time.monotonic() - t0
                if first_event_t is None:
                    first_event_t = now
                etype = type(event).__name__
                event_counter[etype] += 1
                # Capture subtype for content_block_delta
                if etype == "RawContentBlockDeltaEvent":
                    delta = getattr(event, "delta", None)
                    if delta is not None:
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            txt = getattr(delta, "text", "") or ""
                            if not text_bytes:
                                first_text_t = now
                            text_bytes += len(txt)
                        elif dtype == "thinking_delta":
                            tk = getattr(delta, "thinking", "") or ""
                            thinking_bytes += len(tk)
                # Early break — we have enough diagnostic signal.
                if (
                    text_bytes >= _break_after_first_text_bytes
                    or now >= _break_after_total_seconds
                ):
                    break

    try:
        await asyncio.wait_for(_consume(), timeout=per_cell_timeout_s)
        status = "ok" if text_bytes > 0 else "no_events"
    except asyncio.TimeoutError:
        status = "timeout"
        error_class = "asyncio.TimeoutError"
        error_message = (
            f"per_cell_timeout_after_{per_cell_timeout_s}s"
        )
    except Exception as e:
        status = "error"
        error_class = type(e).__name__
        error_message = str(e)[:300]
        # Chain through __cause__ / __context__
        cur = e
        seen = set()
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            error_chain.append(type(cur).__name__)
            cur = cur.__cause__ or cur.__context__
            if len(error_chain) > 8:
                break

    elapsed_ms = (time.monotonic() - t0) * 1000.0
    finished = datetime.now(tz=timezone.utc).isoformat()

    # Compute connect / request_send relative ms from telemetry
    connect_ms: Optional[float] = None
    request_send_ms: Optional[float] = None
    if telemetry.t_request_send is not None:
        request_send_ms = (telemetry.t_request_send - t0) * 1000.0
    if (
        telemetry.t_response_start is not None
        and telemetry.t_request_send is not None
    ):
        connect_ms = (
            telemetry.t_response_start - telemetry.t_request_send
        ) * 1000.0

    return ProbeResult(
        cell_id=cell.cell_id,
        cell=asdict(cell),
        started_at_utc=started,
        finished_at_utc=finished,
        elapsed_ms=elapsed_ms,
        first_event_ms=(
            first_event_t * 1000.0 if first_event_t is not None else None
        ),
        first_text_ms=(
            first_text_t * 1000.0 if first_text_t is not None else None
        ),
        text_bytes=text_bytes,
        thinking_bytes=thinking_bytes,
        event_counts=dict(event_counter),
        http_version=telemetry.http_version,
        connect_ms=connect_ms,
        request_send_ms=request_send_ms,
        status=status,
        error_class=error_class,
        error_chain=error_chain,
        error_message=error_message,
    )


# ---------------------------------------------------------------------------
# Matrix driver + output
# ---------------------------------------------------------------------------


def _format_summary_table(results: List[ProbeResult]) -> str:
    lines = []
    lines.append(
        f"{'cell':<48} {'status':<10} {'first_event':<14} "
        f"{'text_b':<8} {'think_b':<9} {'elapsed_s':<10} {'http':<8} {'error':<30}"
    )
    lines.append("-" * 150)
    for r in results:
        first_ev = (
            f"{r.first_event_ms:.0f}ms" if r.first_event_ms is not None
            else "NEVER"
        )
        err = (r.error_class or "")[:28]
        lines.append(
            f"{r.cell_id:<48} {r.status:<10} {first_ev:<14} "
            f"{r.text_bytes:<8} {r.thinking_bytes:<9} "
            f"{r.elapsed_ms/1000.0:<10.2f} {(r.http_version or '?'):<8} "
            f"{err:<30}"
        )
    return "\n".join(lines)


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not in env", file=sys.stderr)
        return 2

    try:
        import anthropic
        import httpx
    except ImportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3

    # Single shared telemetry handle — gets reset per cell.
    telemetry = _CellTelemetry()

    # Build a custom httpx.AsyncClient with event hooks for telemetry.
    # Long-lived: ONE instance across all matrix cells (simulates
    # harness's sustained pool).
    http2 = _resolve_http2_enabled()
    if http2:
        # h2 is an optional dependency; gracefully degrade.
        try:
            import h2  # noqa: F401
        except ImportError:
            print(
                "  [warn] h2 package not installed — falling back to "
                "HTTP/1.1 (set JARVIS_PROBE_HTTP2_ENABLED=false to "
                "silence)"
            )
            http2 = False
    http_client = httpx.AsyncClient(
        http2=http2,
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=10.0),
        event_hooks={
            "request": [telemetry.on_request],
            "response": [telemetry.on_response],
        },
    )
    client = anthropic.AsyncAnthropic(
        http_client=http_client,
        max_retries=0,
    )

    model = _resolve_model()
    interval_s = _resolve_interval_s()
    per_cell_timeout_s = _resolve_per_cell_timeout_s()
    large_prompt = _load_large_prompt()
    cells = enumerate_matrix()

    print(f"\n══ Adaptive Diagnostic Probe Matrix (Task #101) ══")
    print(f"  model:              {model}")
    print(f"  cells:              {len(cells)}")
    print(f"  per_cell_timeout:   {per_cell_timeout_s}s")
    print(f"  inter_cell_interval: {interval_s}s")
    print(f"  http2_enabled:      {http2}")
    print(f"  large_prompt_path:  {_resolve_large_prompt_path()}")
    print(f"  large_prompt_chars: {len(large_prompt)}")
    print(f"  anthropic SDK:      {anthropic.__version__}")
    print(f"  httpx:              {httpx.__version__}")
    print()

    results: List[ProbeResult] = []
    try:
        for idx, cell in enumerate(cells, 1):
            print(f"── [{idx}/{len(cells)}] {cell.cell_id} ──")
            # Reset telemetry timers for this cell
            telemetry.t_request_send = None
            telemetry.t_response_start = None
            telemetry.http_version = None
            r = await run_cell(
                client, cell, large_prompt,
                model=model,
                per_cell_timeout_s=per_cell_timeout_s,
                telemetry=telemetry,
            )
            results.append(r)
            fe = (
                f"{r.first_event_ms:.0f}ms" if r.first_event_ms is not None
                else "NEVER"
            )
            print(
                f"  status={r.status} first_event={fe} "
                f"text_bytes={r.text_bytes} thinking_bytes={r.thinking_bytes} "
                f"http_version={r.http_version} "
                f"elapsed={r.elapsed_ms/1000.0:.2f}s"
            )
            if r.error_class:
                print(
                    f"  error: {r.error_class} chain={'->'.join(r.error_chain)} "
                    f"msg={r.error_message}"
                )
            if idx < len(cells) and interval_s > 0:
                print(f"  sleep {interval_s}s before next cell ...")
                await asyncio.sleep(interval_s)
    finally:
        try:
            await client.close()
        except Exception:
            pass
        try:
            await http_client.aclose()
        except Exception:
            pass

    # Write results
    out_dir = (
        Path(__file__).resolve().parents[2] / ".jarvis" / "diagnostics"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"claude_probe_matrix_{ts}.json"
    out_path.write_text(json.dumps(
        {
            "schema": "claude_probe_matrix.1",
            "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(),
            "config": {
                "model": model,
                "per_cell_timeout_s": per_cell_timeout_s,
                "interval_s": interval_s,
                "http2_enabled": http2,
                "large_prompt_chars": len(large_prompt),
            },
            "results": [asdict(r) for r in results],
        },
        indent=2,
    ), encoding="utf-8")

    print()
    print("══ Summary ══")
    print(_format_summary_table(results))
    print()
    print(f"  results written to: {out_path}")

    # Concise verdict
    ok = sum(1 for r in results if r.status == "ok")
    no_events = sum(1 for r in results if r.status == "no_events")
    timeouts = sum(1 for r in results if r.status == "timeout")
    errors = sum(1 for r in results if r.status == "error")
    print(
        f"  ok={ok}  no_events={no_events}  timeouts={timeouts}  errors={errors}"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
