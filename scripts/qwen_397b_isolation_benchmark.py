"""
qwen_397b_isolation_benchmark — force a batch of ops through the STANDARD
route with the Claude fallback severed, so we can collect raw telemetry on
DoubleWord Qwen 3.5 397B completion rate.

The directive (2026-04-14): after bt-2026-04-14-182446 proved Gemma 4 31B
stream-stalls on every BACKGROUND op (0/13), and after bt-2026-04-14-174615
showed Qwen 397B streaming-but-not-completing on a single demoted STANDARD
op, the STANDARD route (Qwen 397B primary) is structurally alive but
empirically unvalidated. This harness isolates it: ops are submitted with
``provider_route="standard"`` pre-stamped, and the Claude fallback is
disabled so failures surface as clean exhaustion sentinels instead of
being masked by Tier 1.

Three switches make this benchmark possible without any production risk:

1. ``JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED=1`` — honor a pre-stamped
   ``provider_route`` on the OperationContext (default OFF in production).
2. ``JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES=standard`` — skip the Claude
   fallback for any op on the STANDARD route; raise
   ``fallback_disabled_by_env:standard`` through the existing exhaustion
   path (default unset → normal cascade).
3. ``OUROBOROS_BATTLE_COST_CAP=0.05`` — hard spend cap as a belt-and-braces
   guard. The benchmark should cost ~$0 if the fallback seal is working.

All three are set below before any ouroboros imports happen, so the env
values are the ones the modules see at import time.
"""
from __future__ import annotations

import os
import sys

# ── Environment setup (MUST precede all ouroboros imports) ──────────────
# These three env vars are the entire benchmark contract. If any of them
# fails to land before the governance modules import, the run will not
# isolate STANDARD — it will silently route normally.
os.environ.setdefault("JARVIS_URGENCY_ROUTER_RESPECT_PRE_STAMPED", "1")
os.environ.setdefault("JARVIS_DISABLE_CLAUDE_FALLBACK_ROUTES", "standard")
os.environ.setdefault("OUROBOROS_BATTLE_COST_CAP", "0.05")
os.environ.setdefault("OUROBOROS_BATTLE_IDLE_TIMEOUT", "420")
os.environ.setdefault("JARVIS_GOVERNANCE_MODE", "governed")

# Ensure repo root is on sys.path so ``backend.*`` imports resolve when
# this script is run from any CWD.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import dataclasses  # noqa: E402
import logging  # noqa: E402
import re  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any, Dict, List, Tuple  # noqa: E402


# ── Benchmark operation definitions ─────────────────────────────────────
#
# Three ops chosen to be small, well-scoped, and target stable read-mostly
# modules unrelated to the governance hot path. The point is not to land
# commits — it is to exercise Qwen 397B's full GENERATE → VALIDATE cycle
# and see whether it produces a usable candidate within the STANDARD
# budget. The files are safe to touch: any output is validated through
# the normal pipeline (Iron Gate, ASCII strictness, exploration gate)
# and rolled back on failure.
BENCHMARK_OPS: Tuple[Dict[str, Any], ...] = (
    {
        "label": "op_a_signals_docstring",
        "description": (
            "Add a one-paragraph module-level docstring to "
            "backend/core/ouroboros/governance/intent/signals.py summarizing "
            "the IntentSignal contract (source, target_files, urgency, "
            "confidence, stable). Do not change any existing code — append "
            "the docstring at the top of the module only."
        ),
        "target_files": (
            "backend/core/ouroboros/governance/intent/signals.py",
        ),
    },
    {
        "label": "op_b_topology_repr",
        "description": (
            "Add a __repr__ method to the RouteTopology dataclass in "
            "backend/core/ouroboros/governance/provider_topology.py that "
            "returns a compact single-line string of the form "
            "'RouteTopology(allowed=<bool>, model=<str>, block=<str>)'. "
            "The dataclass is frozen — use a regular method (not a field)."
        ),
        "target_files": (
            "backend/core/ouroboros/governance/provider_topology.py",
        ),
    },
    {
        "label": "op_c_urgency_describe_fallback",
        "description": (
            "In backend/core/ouroboros/governance/urgency_router.py, add a "
            "module-level constant _UNKNOWN_ROUTE_DESCRIPTION = 'unknown "
            "route' and reference it from UrgencyRouter.describe_route's "
            "final return statement instead of the inline string literal. "
            "No other changes."
        ),
        "target_files": (
            "backend/core/ouroboros/governance/urgency_router.py",
        ),
    },
)


# ── Telemetry log handler ───────────────────────────────────────────────
#
# We attach an in-memory handler to the ``backend.core.ouroboros.governance``
# logger hierarchy and capture every record whose message matches a known
# interesting pattern. The raw records are kept for post-run analysis;
# the summary counts only what we care about for the directive.
_INTERESTING_PATTERNS = {
    "tier0_rt_success": re.compile(
        r"Tier 0 RT: \d+ candidates in [\d.]+s",
    ),
    "tier0_rt_streaming_extension": re.compile(
        r"Tier 0 RT: actively streaming, granting",
    ),
    "tier0_rt_no_candidates": re.compile(
        r"Tier 0 RT: no candidates",
    ),
    "fallback_disabled_sentinel": re.compile(
        r"Fallback disabled by env for route=standard",
    ),
    "exhaustion": re.compile(
        r"all_providers_exhausted|EXHAUSTION",
    ),
    "exploration_ledger_decision": re.compile(
        r"ExplorationLedger\(decision\)",
    ),
    "exploration_ledger_shadow": re.compile(
        r"ExplorationLedger\(shadow",
    ),
    "exploration_insufficient": re.compile(
        r"ExplorationInsufficientError",
    ),
    "dw_files_endpoint_error": re.compile(
        r"/v1/files.*(Timeout|ConnectionError|ConnectTimeout)",
        re.IGNORECASE,
    ),
    "sse_stream_stall": re.compile(
        r"SSE stream stalled|first_chunk",
    ),
    "topology_block_standard": re.compile(
        r"Topology block: route=standard",
    ),
}


class MemoryHandler(logging.Handler):
    """Minimal logging handler that records matching messages in memory."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: List[logging.LogRecord] = []
        self.counts: Dict[str, int] = {k: 0 for k in _INTERESTING_PATTERNS}

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        matched_any = False
        for key, pat in _INTERESTING_PATTERNS.items():
            if pat.search(msg):
                self.counts[key] += 1
                matched_any = True
        if matched_any:
            self.records.append(record)


def attach_memory_handler() -> MemoryHandler:
    handler = MemoryHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    # Attach at the root of the governance package so we catch everything
    # from CandidateGenerator, orchestrator, DoublewordProvider, etc.
    logger = logging.getLogger("backend.core.ouroboros.governance")
    logger.addHandler(handler)
    # Belt-and-braces: also attach to the root logger in case any module
    # uses the root logger directly.
    logging.getLogger().addHandler(handler)
    return handler


# ── Op injection ────────────────────────────────────────────────────────


async def _wait_for_boot(harness: Any, max_s: float = 120.0) -> bool:
    """Poll until the governed loop service and shutdown event are both ready.

    Returns True on success, False on timeout. We don't reach into harness
    internals beyond what the harness itself sets during its boot sequence
    (governed_loop_service + shutdown_event) — those are our sync points.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        if (
            getattr(harness, "_governed_loop_service", None) is not None
            and getattr(harness, "_shutdown_event", None) is not None
            and getattr(harness, "_intake_service", None) is not None
        ):
            return True
        await asyncio.sleep(0.5)
    return False


def _build_op_context(
    op_spec: Dict[str, Any],
) -> Any:
    """Build an OperationContext pre-stamped for the STANDARD route.

    The create() classmethod doesn't accept provider_route as a kwarg, so we
    produce the base context first, then use dataclasses.replace to stamp
    provider_route + provider_route_reason. The resulting context_hash is
    stale, but it gets recomputed at the first advance() call inside the
    orchestrator (CLASSIFY → ROUTE transition), so nothing downstream cares.
    """
    from backend.core.ouroboros.governance.op_context import OperationContext

    ctx = OperationContext.create(
        target_files=op_spec["target_files"],
        description=op_spec["description"],
        signal_urgency="normal",
        signal_source="qwen_iso_bench",
    )
    ctx = dataclasses.replace(
        ctx,
        provider_route="standard",
        provider_route_reason="forced_by_qwen_isolation_benchmark",
    )
    return ctx


async def _submit_one(
    harness: Any,
    op_spec: Dict[str, Any],
    results: List[Dict[str, Any]],
) -> None:
    logger = logging.getLogger("qwen_iso_bench")
    gls = harness._governed_loop_service
    ctx = _build_op_context(op_spec)
    label = op_spec["label"]
    op_id = ctx.op_id
    logger.info("[bench] submitting %s op_id=%s", label, op_id)
    t_start = time.monotonic()
    outcome: Dict[str, Any] = {
        "label": label,
        "op_id": op_id,
        "target_files": list(op_spec["target_files"]),
    }
    try:
        result = await gls.submit(ctx, trigger_source="qwen_iso_bench")
        outcome["terminal_phase"] = getattr(
            getattr(result, "terminal_phase", None), "name", "?",
        )
        outcome["reason_code"] = getattr(result, "reason_code", "?") or "?"
        outcome["terminal_class"] = getattr(result, "terminal_class", "?") or "?"
    except BaseException as exc:  # pragma: no cover - benchmark hot path
        outcome["exception"] = f"{type(exc).__name__}: {exc}"
        outcome["terminal_phase"] = "EXCEPTION"
        outcome["reason_code"] = type(exc).__name__
    finally:
        outcome["duration_s"] = round(time.monotonic() - t_start, 2)
        results.append(outcome)
        logger.info(
            "[bench] done %s phase=%s reason=%s duration=%.2fs",
            label,
            outcome.get("terminal_phase", "?"),
            outcome.get("reason_code", "?"),
            outcome["duration_s"],
        )


async def _run_injection(
    harness: Any,
    mode: str,
) -> List[Dict[str, Any]]:
    logger = logging.getLogger("qwen_iso_bench")
    logger.info("[bench] waiting for harness boot...")
    ready = await _wait_for_boot(harness)
    if not ready:
        logger.error("[bench] boot timeout — aborting without injecting")
        return []

    # Belt-and-braces grace so intake + orchestrator wiring finishes.
    await asyncio.sleep(3.0)
    logger.info("[bench] boot complete — injecting %d ops (mode=%s)", len(BENCHMARK_OPS), mode)

    results: List[Dict[str, Any]] = []
    if mode == "concurrent":
        await asyncio.gather(
            *[_submit_one(harness, op, results) for op in BENCHMARK_OPS],
        )
    else:
        for op in BENCHMARK_OPS:
            await _submit_one(harness, op, results)

    logger.info("[bench] all ops complete — signaling harness shutdown")
    evt = getattr(harness, "_shutdown_event", None)
    if evt is not None:
        evt.set()
    return results


# ── Report rendering ────────────────────────────────────────────────────


def _render_report(
    results: List[Dict[str, Any]],
    handler: MemoryHandler,
) -> str:
    lines: List[str] = []
    lines.append("=" * 78)
    lines.append("Qwen 397B Isolation Benchmark — report")
    lines.append("=" * 78)
    lines.append("")
    lines.append("Per-op outcomes:")
    lines.append("-" * 78)
    for r in results:
        lines.append(
            f"  {r.get('label', '?'):<36}  "
            f"phase={r.get('terminal_phase', '?'):<12} "
            f"reason={(r.get('reason_code') or '?')[:40]:<40} "
            f"{r.get('duration_s', 0):>6.1f}s"
        )
    lines.append("")
    lines.append("Telemetry counters (log-line matches):")
    lines.append("-" * 78)
    for key in sorted(handler.counts.keys()):
        lines.append(f"  {key:<36}  {handler.counts[key]:>4}")
    lines.append("")
    lines.append("Verdicts:")
    lines.append("-" * 78)
    tier0_ok = handler.counts.get("tier0_rt_success", 0)
    seal_ok = handler.counts.get("fallback_disabled_sentinel", 0)
    files_err = handler.counts.get("dw_files_endpoint_error", 0)
    lines.append(
        f"  Tier 0 RT completions (Qwen produced valid candidate): {tier0_ok}"
    )
    lines.append(
        f"  Fallback seal fired (Claude cascade skipped):           {seal_ok}"
    )
    lines.append(
        f"  /v1/files endpoint errors:                              {files_err}"
    )
    lines.append("")
    if tier0_ok >= len(results) and files_err == 0:
        lines.append("  ✓ STANDARD route empirically healthy under isolation.")
    elif tier0_ok > 0:
        lines.append(
            f"  ~ Partial success: {tier0_ok}/{len(results)} completed. "
            "Check per-op outcomes for the failure mode."
        )
    else:
        lines.append(
            "  ✗ Zero completions. STANDARD route is structurally broken "
            "under isolation — same failure mode as Gemma BG."
        )
    lines.append("")
    lines.append("=" * 78)
    return "\n".join(lines)


# ── Main entrypoint ─────────────────────────────────────────────────────


async def _amain(mode: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger = logging.getLogger("qwen_iso_bench")

    # Attach the memory handler BEFORE booting the harness so we catch
    # every governance log line from boot onward.
    handler = attach_memory_handler()

    # Load .env files so DOUBLEWORD_API_KEY, ANTHROPIC_API_KEY, etc. are
    # available. override=True is intentional: bt-2026-04-14-203740 saw
    # Claude return 401 because a stale shell-exported ANTHROPIC_API_KEY
    # beat the fresh .env value under the old override=False semantics.
    # Inside this benchmark process, .env is the authoritative source.
    try:
        from dotenv import load_dotenv
        for _dot in (".env", ".env.local"):
            _p = Path(_REPO_ROOT) / _dot
            if _p.is_file():
                load_dotenv(_p, override=True)
                logger.info("[bench] loaded env from %s (override=True)", _p)
    except ImportError:
        logger.warning("[bench] python-dotenv not installed; skipping .env load")

    from backend.core.ouroboros.battle_test.harness import (
        BattleTestHarness,
        HarnessConfig,
    )

    config = HarnessConfig.from_env()
    logger.info(
        "[bench] starting harness cost_cap=$%.2f idle=%.0fs mode=%s",
        config.cost_cap_usd,
        config.idle_timeout_s,
        mode,
    )

    harness = BattleTestHarness(config)

    # Kick off the injection task concurrently with harness.run(). It polls
    # for boot completion, injects ops, then signals shutdown.
    injection_task = asyncio.ensure_future(_run_injection(harness, mode))

    results: List[Dict[str, Any]] = []
    try:
        await harness.run()
    except BaseException as exc:
        logger.error("[bench] harness.run() raised: %s", exc)
    finally:
        if injection_task.done():
            try:
                results = injection_task.result() or []
            except BaseException as exc:
                logger.error("[bench] injection task raised: %s", exc)
        else:
            injection_task.cancel()
            try:
                results = await injection_task
            except BaseException:
                results = []

    report = _render_report(results, handler)
    print("\n" + report)

    # Persist report next to the harness session dir for audit.
    try:
        session_dir = getattr(harness, "_session_dir", None)
        if session_dir is not None:
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "qwen_iso_benchmark_report.txt").write_text(report, encoding="utf-8")
    except Exception as exc:  # pragma: no cover
        logger.warning("[bench] failed to persist report: %s", exc)

    # Exit code: 0 if at least one op succeeded at Tier 0, else 1.
    tier0_ok = handler.counts.get("tier0_rt_success", 0)
    return 0 if tier0_ok > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Isolate the STANDARD route (Qwen 397B primary) by pre-stamping "
            "provider_route and severing the Claude fallback. Produces raw "
            "telemetry on DW completion rate without Claude masking."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("concurrent", "serial"),
        default="concurrent",
        help="Submit ops concurrently (via asyncio.gather) or serially.",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(_amain(args.mode))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
