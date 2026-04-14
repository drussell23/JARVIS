#!/usr/bin/env python3
"""Live-Fire Behavioral Test: ExplorationLedger Iron Gate collision.

Purpose
-------
Prove the neuroplasticity claim for #103 (ExplorationLedger enforcement).
The deterministic infrastructure is unit-tested; this harness is the
missing demonstration that the *model* actually adapts its tool use in
response to ``ExplorationInsufficientError`` feedback.

Execution plan (directly mirrors the Live-Fire Directive):
  1. Enable enforcement         → JARVIS_EXPLORATION_LEDGER_ENABLED=true
  2. Seed an architectural task → description contains architectural
                                  keywords + target_files point at a
                                  throwaway tmp target so nothing real
                                  gets rewritten
  3. Force the failure state    → instrumentation note in the description
                                  tells the model to use read_file only on
                                  the first pass, guaranteeing the Iron
                                  Gate fires at attempt 1
  4. Observe the neuroplasticity→ capture the decision-path log events
                                  across both attempts and print a
                                  structured PASS/FAIL/BORING verdict

The harness is intentionally *single-op*. It does not boot sensors, intake,
consciousness, or the SerpentFlow TUI — it only spins up the minimum
stack required for ``GovernedLoopService.submit(ctx)`` to run the
orchestrator on a real ``ClaudeProvider`` + real Venom tool loop.

Usage::

    ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \\
    python3 scripts/live_fire_exploration_gate.py 2>&1 | tee /tmp/live_fire.log

Exit codes
----------
  0  PASS   — first attempt insufficient AND second attempt sufficient
             (the model pivoted its tool use on retry, covering the
             previously-missing categories).
  1  FAIL   — first attempt already sufficient, OR second attempt still
             insufficient, OR retry never fired.
  2  BORING — pipeline never reached the post-GENERATE gate (provider
             outage, cancellation, etc.). Re-run with network available.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# CRITICAL: env-var injection happens BEFORE any governance import so the
# orchestrator's ``is_ledger_enabled()`` reads the enforcement flag at
# module-load time, not after it already cached the counter path.
# ---------------------------------------------------------------------------
import os as _os
from pathlib import Path as _Path


_HARNESS_REPO_ROOT = _Path(__file__).resolve().parent.parent
# API keys that .env should always override (stale shell exports are a
# common source of 401 errors). Mirrors the battle-test loader.
_FORCE_OVERRIDE_KEYS = frozenset({"ANTHROPIC_API_KEY", "DOUBLEWORD_API_KEY"})


def _load_env_files() -> None:
    """Load .env files from project root and backend/ into os.environ.

    API keys are force-overridden; everything else uses setdefault so
    explicit shell exports still win for non-secret config. This mirrors
    ``scripts/ouroboros_battle_test.py::_load_env_files`` so both entry
    points share identical env-loading semantics.
    """
    for env_path in (_HARNESS_REPO_ROOT / ".env", _HARNESS_REPO_ROOT / "backend" / ".env"):
        if not env_path.exists():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key in _FORCE_OVERRIDE_KEYS:
                _os.environ[key] = value
            else:
                _os.environ.setdefault(key, value)


_load_env_files()

_os.environ.setdefault("JARVIS_EXPLORATION_LEDGER_ENABLED", "true")
# Belt and suspenders — make the architectural floors explicit so the
# test fails loudly if someone dials them down in the module defaults.
_os.environ.setdefault("JARVIS_EXPLORATION_MIN_SCORE_ARCHITECTURAL", "14.0")
_os.environ.setdefault("JARVIS_EXPLORATION_MIN_CATEGORIES_ARCHITECTURAL", "4")
# Keep the blast radius tiny: no voice, no L2, no auto-commit, no worktrees.
_os.environ.setdefault("JARVIS_VOICE_ENABLED", "0")
_os.environ.setdefault("JARVIS_L2_ENABLED", "false")
_os.environ.setdefault("JARVIS_AUTO_COMMIT_ENABLED", "false")
_os.environ.setdefault("JARVIS_GOVERNED_L3_ENABLED", "false")
# The battle-test harness pauses L3 after reproducible probe failures;
# this single-op path has no probe loop, so keep governance in the
# executing mode from the start.
_os.environ.setdefault("JARVIS_GOVERNANCE_MODE", "governed")

# --- Force-overrides (ignore prior setdefaults / .env values) -----------
# Two pipeline-timeout lines exist in .env ("150" for local Qwen2-7B,
# then "1200" as the intended override). Our setdefault() loader picks
# the first occurrence, so 150 won and our first run died at the 210s
# hard cap (150 + 60s grace). This test calls Claude/DW, not the local
# LLM, so override to 900s unconditionally.
#
# Routing strategy for this op (confirmed via urgency_router.py fix):
#   - complexity=architectural → ProviderRoute.COMPLEX
#   - COMPLEX = "Claude plans → DW executes" (CLAUDE.md §Urgency-Aware
#     Provider Routing). Plays to each provider's strength:
#       * Claude (expensive, smart): PLAN phase with extended thinking
#       * DW 397B (cheap, streaming): GENERATE phase via RT SSE
#   - Semantic Triage (DW 35B batch) still runs pre-CLASSIFY but is
#     a single short prompt (~$0.0001) and informative — leave it on.
_os.environ["JARVIS_PIPELINE_TIMEOUT_S"] = "900"

# --- Per-attempt GENERATE window (route-aware) --------------------------
# Raised from the default 240s to 400s for the COMPLEX route after
# bt third-run diagnosis: with the default, DW RT burns 120s then
# Claude fallback has only 100s left — not enough for an architectural
# Venom loop (extended thinking 20s + tool round 0 40s + tool round 1
# 60s + patch stream 40s ≈ 160s minimum). 400s gives DW 150s and
# Claude ~240s of headroom, both comfortable for tool-loop ops.
#
# Exposed as per-route env vars (added in orchestrator.py after the
# bt-2026-04-14-041952 100%-BACKGROUND-timeout diagnosis). Harness
# only raises COMPLEX; the others keep their calibrated defaults.
_os.environ["JARVIS_GEN_TIMEOUT_COMPLEX_S"] = "400"

# --- Nervous-system reflex: BACKGROUND → Claude safety net --------------
# bt-2026-04-14-041952 showed 11/11 BACKGROUND ops dying on
# background_dw_timeout:180s — zero survivors, zero Iron Gate hits.
# Enabling this flag tells CandidateGenerator._generate_background to
# cascade to Claude via _call_fallback when DW times out/errors/empty,
# and widens the BACKGROUND budget profile in urgency_router.py to
# reserve 25s for Claude (instead of 0s).
#
# The live-fire test itself runs on the COMPLEX route, so this flag is
# defensive hygiene here — it rescues any BACKGROUND sensor ops the
# governance stack fires concurrently (intake, opportunity miner, etc.)
# so they don't count as real regressions while we're validating #103.
_os.environ["JARVIS_BACKGROUND_ALLOW_FALLBACK"] = "true"

# Fallback minimum guaranteed window. Default 90s is too tight when
# DW has already burned 120s+ of the parent deadline — Claude's
# _call_fallback refreshes its own internal deadline using
# max(parent_remaining, _FALLBACK_MIN_GUARANTEED_S). Raising to 200s
# guarantees Claude a useful runway even after DW saturates. The
# orchestrator's outer wait_for is still the Iron Gate, so this
# cannot exceed the route's outer window.
_os.environ.setdefault("OUROBOROS_FALLBACK_MIN_GUARANTEED_S", "200")

import argparse
import asyncio
import gc
import importlib.metadata as _metadata
import logging
import re
import sys
import tempfile
import textwrap
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


if not hasattr(_metadata, "packages_distributions"):
    def _packages_distributions_fallback():  # type: ignore[misc]
        try:
            from importlib_metadata import packages_distributions  # type: ignore[import-untyped]
            return packages_distributions()
        except Exception:
            return {}
    _metadata.packages_distributions = _packages_distributions_fallback  # type: ignore[attr-defined]


warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
# aiohttp ClientSession teardown from Langfuse / DoubleWord / Prime-client
# transports fires ResourceWarning after stack.stop(). Not our leak to
# fix — suppressed at the harness boundary.
warnings.filterwarnings("ignore", category=ResourceWarning)


class _SuppressUnclosedAiohttp(logging.Filter):
    """Drop cosmetic ``asyncio`` ERROR lines from aiohttp session teardown.

    The governance stack wires several HTTP-based transports (Langfuse,
    J-Prime, DoubleWord) whose aiohttp ``ClientSession`` instances survive
    ``stack.stop()`` and emit ``Unclosed client session`` / ``Unclosed
    connector`` lines through ``asyncio``'s logger when finalized. The
    fix lives inside each transport's shutdown path — not worth touching
    from a single-op live-fire harness. Suppressing the five specific
    phrases keeps our verdict block the last thing printed.
    """

    _PATTERNS = (
        "Unclosed client session",
        "Unclosed connector",
        "client_session:",
        "connector:",
        "connections:",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(p in msg for p in self._PATTERNS)


_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Log capture
# ---------------------------------------------------------------------------


_DECISION_RE = re.compile(
    r"ExplorationLedger\(decision\)\s+"
    r"op=(?P<op>\S+)\s+"
    r"complexity=(?P<complexity>\S+)\s+"
    r"score=(?P<score>[\d.]+)\s+"
    r"min_score=(?P<min_score>[\d.]+)\s+"
    r"unique=(?P<unique>\d+)\s+"
    r"categories=(?P<categories>\S*)\s+"
    r"would_pass=(?P<would_pass>True|False)"
)
_INSUFFICIENT_RE = re.compile(
    r"Iron Gate — ExplorationLedger\(decision\) insufficient\s+"
    r"op=(?P<op>\S+)\s+"
    r"exploration_insufficient:\s+"
    r"score=(?P<score>[\d.]+)/(?P<min_score>[\d.]+)\s+"
    r"categories=(?P<cats>\d+/\d+)\s+"
    r"missing=(?P<missing>\S+)\s+"
    r"\(attempt=(?P<attempt>\d+)\)"
)


@dataclass
class DecisionEvent:
    attempt: int
    complexity: str
    score: float
    min_score: float
    unique: int
    categories: Tuple[str, ...]
    would_pass: bool
    raw: str


@dataclass
class InsufficientEvent:
    attempt: int
    score: float
    min_score: float
    cats: str
    missing: Tuple[str, ...]
    raw: str


@dataclass
class LedgerCapture:
    decisions: List[DecisionEvent] = field(default_factory=list)
    insufficients: List[InsufficientEvent] = field(default_factory=list)
    retry_advanced: int = 0
    generate_attempts: List[int] = field(default_factory=list)
    raw_relevant: List[str] = field(default_factory=list)


class LedgerCaptureHandler(logging.Handler):
    """Filters log records for ExplorationLedger / Iron Gate / retry events."""

    _KEYWORDS = (
        "ExplorationLedger",
        "Iron Gate",
        "GENERATE_RETRY",
        "exploration_insufficient",
        "ExplorationInsufficientError",
        "GENERATE attempt",
        "advance(",
    )

    def __init__(self, capture: LedgerCapture) -> None:
        super().__init__(level=logging.DEBUG)
        self._cap = capture
        self._decision_attempt = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if not any(k in msg for k in self._KEYWORDS):
            return
        self._cap.raw_relevant.append(msg)

        m = _DECISION_RE.search(msg)
        if m:
            self._decision_attempt += 1
            cats_raw = m.group("categories") or ""
            cats = tuple(c for c in cats_raw.split(",") if c and c != "-")
            self._cap.decisions.append(
                DecisionEvent(
                    attempt=self._decision_attempt,
                    complexity=m.group("complexity"),
                    score=float(m.group("score")),
                    min_score=float(m.group("min_score")),
                    unique=int(m.group("unique")),
                    categories=cats,
                    would_pass=(m.group("would_pass") == "True"),
                    raw=msg,
                )
            )
            return

        m = _INSUFFICIENT_RE.search(msg)
        if m:
            missing_raw = m.group("missing") or ""
            missing = tuple(c for c in missing_raw.split(",") if c and c != "-")
            self._cap.insufficients.append(
                InsufficientEvent(
                    attempt=int(m.group("attempt")),
                    score=float(m.group("score")),
                    min_score=float(m.group("min_score")),
                    cats=m.group("cats"),
                    missing=missing,
                    raw=msg,
                )
            )
            return

        if "GENERATE_RETRY" in msg and "advance" in msg.lower():
            self._cap.retry_advanced += 1


# ---------------------------------------------------------------------------
# Stack boot
# ---------------------------------------------------------------------------


async def _boot_stack() -> Any:
    """Build + start a real GovernanceStack in governed mode."""
    from backend.core.ouroboros.governance.integration import (
        GovernanceConfig,
        create_governance_stack,
    )

    ns = argparse.Namespace(
        skip_governance=False,
        governance_mode="governed",
    )
    gov_config = GovernanceConfig.from_env_and_args(ns)
    stack = await create_governance_stack(gov_config, oracle=None)
    await stack.start()
    # Promote out of the SANDBOX default so can_write() allows the apply
    # path through the GATE phase — same sequence the battle-test harness
    # performs at boot.
    await stack.controller.mark_gates_passed()
    await stack.controller.enable_governed_autonomy()
    return stack


async def _boot_service(stack: Any, project_root: Path) -> Any:
    """Construct + start a GovernedLoopService on top of the stack."""
    import dataclasses as _dc

    from backend.core.ouroboros.governance.governed_loop_service import (
        GovernedLoopConfig,
        GovernedLoopService,
    )

    # --- Test-harness monkey-patches (applied BEFORE service.start() so
    #     the GLS._build_components() pass picks up the new values) ------
    #
    # CandidateGenerator hard-caps the fallback provider (Claude) budget
    # via ``_FALLBACK_MAX_TIMEOUT_S = 120.0`` — a class-level constant
    # with no env override. 120s was tuned for STANDARD-route single-turn
    # generation; it is not enough for an architectural op with the
    # Venom multi-round tool loop and extended thinking.
    #
    # Evidence from bt run bcz8i0vg0:
    #   attempt 1: DW RT 120s + Claude fallback 120s  → both TIMEOUT
    #   attempt 2: Claude only, 31529 bytes received, mid tool_round=1
    #              when the 120s wait_for fired at elapsed=123s
    #              (sem_wait_total_s=122.91)
    # Both Claude runs look like they would have finished with ~60s more.
    # Raising to 300s gives the architectural tool loop room to breathe
    # (extended thinking 20s + tool round 0 40s + tool round 1 60s + ...)
    # without exceeding the 900s pipeline budget. DW tier0 budget is
    # route-computed from remaining deadline so it gets its share too.
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    CandidateGenerator._FALLBACK_MAX_TIMEOUT_S = 300.0

    gls_config = GovernedLoopConfig.from_env(project_root=project_root)
    # Widen the canary to match the battle-test harness — production
    # defaults block autonomous writes to everything outside tests/docs,
    # which would cancel the op at GATE before the gate we actually want
    # to exercise fires.
    gls_config = _dc.replace(gls_config, initial_canary_slices=("",))
    service = GovernedLoopService(stack=stack, config=gls_config)
    await service.start()
    return service


# ---------------------------------------------------------------------------
# Seed op
# ---------------------------------------------------------------------------


_ARCHITECTURAL_DESCRIPTION = textwrap.dedent(
    """
    Design a new capability for the Ouroboros governance architecture: a
    protocol-level event-bus fan-out module that publishes per-phase
    decisions to downstream consumers. This is a deliberately
    architectural change — you must understand the current orchestrator
    call graph, the CommProtocol schema, and recent refactors before
    proposing any patch.

    [LIVE-FIRE TEST INSTRUMENTATION — READ CAREFULLY]
    On your FIRST generation attempt, restrict yourself to read_file
    calls ONLY. Do NOT call search_code, get_callers, list_symbols,
    git_blame, git_log, or git_diff on the first attempt. Read a handful
    of files and then emit your patch. This restriction is lifted on
    subsequent attempts — if the system rejects your first attempt, you
    are expected to follow whatever exploration instructions come back
    in the retry feedback.

    The target_files entry is a throwaway sandbox path; write your
    proposed module there. Explore the real repository at
    backend/core/ouroboros/governance/orchestrator.py and related files
    to ground your design.
    """
).strip()


def _build_target(tmpdir: Path) -> Path:
    """Create a throwaway target file the APPLY phase can safely write to."""
    target = tmpdir / "live_fire_target.py"
    target.write_text(
        "# Live-fire sandbox — safe to overwrite.\n"
        "# This file exists only so APPLY has somewhere to land.\n",
        encoding="utf-8",
    )
    return target


def _build_context(target: Path) -> Any:
    from backend.core.ouroboros.governance.op_context import OperationContext

    return OperationContext.create(
        target_files=(str(target),),
        description=_ARCHITECTURAL_DESCRIPTION,
        op_id="live-fire-exploration-gate",
        signal_source="live_fire_test",
        signal_urgency="normal",
    )


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    status: str  # PASS | FAIL | BORING
    headline: str
    details: List[str]


def _classify(cap: LedgerCapture, result: Any) -> Verdict:
    details: List[str] = []

    if not cap.decisions:
        return Verdict(
            status="BORING",
            headline="No ExplorationLedger(decision) log lines captured.",
            details=[
                "The pipeline never reached the post-GENERATE ledger check.",
                "Likely causes: provider outage, op cancelled before GENERATE, "
                "or the ledger feature flag was not read at import time.",
                f"Terminal phase reported: {getattr(result, 'terminal_phase', '?')}",
                f"Reason code:             {getattr(result, 'reason_code', '?')}",
            ],
        )

    first = cap.decisions[0]
    details.append(
        f"Attempt 1: score={first.score:.1f}/{first.min_score:.1f} "
        f"categories={first.categories or '-'} would_pass={first.would_pass}"
    )

    if first.would_pass:
        return Verdict(
            status="FAIL",
            headline="First attempt already passed the ledger floor — no collision.",
            details=details + [
                "The instrumentation note failed to constrain the model, "
                "or the floors are too low. Try raising "
                "JARVIS_EXPLORATION_MIN_SCORE_ARCHITECTURAL, or strengthen "
                "the restriction phrasing in the seed description.",
            ],
        )

    if not cap.insufficients:
        return Verdict(
            status="FAIL",
            headline="First attempt was insufficient but Iron Gate did not log it.",
            details=details + [
                "Expected an 'Iron Gate — ExplorationLedger(decision) "
                "insufficient' warning after the decision line. Check the "
                "orchestrator log level and the enforcement branch in "
                "orchestrator.py:2400-2499.",
            ],
        )

    first_insuf = cap.insufficients[0]
    details.append(
        f"Iron Gate fired attempt {first_insuf.attempt}: missing="
        f"{','.join(first_insuf.missing) or '-'}"
    )

    if len(cap.decisions) < 2:
        return Verdict(
            status="FAIL",
            headline="Iron Gate fired but no retry decision line observed.",
            details=details + [
                "The orchestrator should have caught "
                "ExplorationInsufficientError, rendered retry feedback, "
                "and re-entered GENERATE — at which point a second "
                "ExplorationLedger(decision) line should have landed.",
                "Check the exception plumbing around orchestrator.py:3017 "
                "(ExplorationInsufficientError catch + _retry_ctx_kwargs).",
            ],
        )

    second = cap.decisions[1]
    details.append(
        f"Attempt 2: score={second.score:.1f}/{second.min_score:.1f} "
        f"categories={second.categories or '-'} would_pass={second.would_pass}"
    )

    # Did the agent actually pivot? Compare category sets.
    first_cats = set(first.categories)
    second_cats = set(second.categories)
    pivoted_to = second_cats - first_cats
    if pivoted_to:
        details.append(
            f"Agent pivoted — new categories used on retry: "
            f"{','.join(sorted(pivoted_to))}"
        )
    else:
        details.append(
            "Agent did NOT invoke any new exploration category on retry."
        )

    covered_missing = set(first_insuf.missing) & second_cats
    if covered_missing:
        details.append(
            f"Retry covered previously-missing categories: "
            f"{','.join(sorted(covered_missing))}"
        )

    if second.would_pass and pivoted_to:
        return Verdict(
            status="PASS",
            headline=(
                "NEUROPLASTICITY CONFIRMED: model pivoted in response to "
                "Iron Gate feedback and cleared the ledger floor on retry."
            ),
            details=details,
        )
    if second.would_pass and not pivoted_to:
        return Verdict(
            status="PASS",
            headline=(
                "Retry cleared the floor, but category delta is empty — "
                "the model may have added volume, not diversity. Still "
                "counts as a pass because the gate is floor-based."
            ),
            details=details,
        )
    return Verdict(
        status="FAIL",
        headline="Retry attempted but still failed the ledger floor.",
        details=details + [
            "The retry feedback reached the model but it did not add "
            "enough diversity. Inspect the captured retry_advanced count "
            "and the raw relevant log lines for clues.",
        ],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Live-fire ExplorationLedger Iron Gate behavioral test",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Stream all captured log lines at the end of the run",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "Boot the stack + service but skip the submit() call. "
            "Use this to verify the environment before the expensive run."
        ),
    )
    p.add_argument(
        "--timeout-s", type=float, default=600.0,
        help="Hard ceiling on submit() duration (seconds, default 600)",
    )
    return p.parse_args()


async def _run() -> int:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Install the aiohttp-leak silencer on both the root logger and the
    # asyncio logger — aiohttp routes its resource warnings through
    # ``asyncio``, but some transports re-raise via the root logger.
    _leak_filter = _SuppressUnclosedAiohttp()
    logging.getLogger().addFilter(_leak_filter)
    logging.getLogger("asyncio").addFilter(_leak_filter)

    capture = LedgerCapture()
    handler = LedgerCaptureHandler(capture)
    root = logging.getLogger()
    root.addHandler(handler)
    # The orchestrator's capture-of-interest lines are at DEBUG/INFO; make
    # sure we see them without drowning stdout.
    logging.getLogger("backend.core.ouroboros.governance.orchestrator").setLevel(
        logging.DEBUG
    )

    api_key = _os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not args.dry_run:
        print(
            "ERROR: ANTHROPIC_API_KEY is not set. Expected to find it in "
            "project-root .env, backend/.env, or the shell environment. "
            "The live-fire test needs a real Claude provider to observe "
            "model behavior.",
            file=sys.stderr,
        )
        return 2
    api_key_fp = (api_key[:12] + "..." + api_key[-6:]) if api_key else "(absent)"

    print("=" * 72)
    print("LIVE-FIRE EXPLORATION GATE — #103 behavioral validation")
    print("=" * 72)
    print(f"API key (fingerprint): {api_key_fp}")
    print(
        "Enforcement flag:  JARVIS_EXPLORATION_LEDGER_ENABLED="
        + _os.environ.get("JARVIS_EXPLORATION_LEDGER_ENABLED", "?")
    )
    print(
        "Architectural floor: "
        f"score>={_os.environ.get('JARVIS_EXPLORATION_MIN_SCORE_ARCHITECTURAL')} "
        f"categories>={_os.environ.get('JARVIS_EXPLORATION_MIN_CATEGORIES_ARCHITECTURAL')} "
        "required={call_graph, history}"
    )
    print()

    stack: Any = None
    service: Any = None
    tmpdir_obj: Optional[tempfile.TemporaryDirectory] = None
    result: Any = None
    try:
        print("[1/4] Booting governance stack...")
        stack = await _boot_stack()
        assert stack is not None
        print(
            f"      stack mode={stack.controller.mode.value} "
            f"writes_allowed={stack.controller.writes_allowed}"
        )

        print("[2/4] Booting GovernedLoopService (real provider + Venom tool loop)...")
        service = await _boot_service(stack, _REPO_ROOT)
        assert service is not None
        print(f"      service state={service.state.name}")

        print("[3/4] Seeding architectural op...")
        tmpdir_obj = tempfile.TemporaryDirectory(prefix="live_fire_")
        tmpdir = Path(tmpdir_obj.name)
        target = _build_target(tmpdir)
        ctx = _build_context(target)
        print(f"      op_id={ctx.op_id} target={target}")

        if args.dry_run:
            print("[4/4] --dry-run: skipping submit(). Stack boot verified.")
            return 0

        print("[4/4] Submitting (this can take 3–10 minutes on a cold run)...")
        assert service is not None
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                service.submit(ctx, trigger_source="live_fire_test"),
                timeout=args.timeout_s,
            )
        except asyncio.TimeoutError:
            print(f"      TIMEOUT after {args.timeout_s}s")
            result = None
        elapsed = time.monotonic() - start
        print(f"      submit completed in {elapsed:.1f}s")

    finally:
        if service is not None:
            try:
                await service.stop()
            except Exception as exc:
                logging.getLogger(__name__).warning("service.stop failed: %s", exc)
        if stack is not None:
            try:
                await stack.stop()
            except Exception as exc:
                logging.getLogger(__name__).warning("stack.stop failed: %s", exc)
        if tmpdir_obj is not None:
            try:
                tmpdir_obj.cleanup()
            except Exception:
                pass
        root.removeHandler(handler)
        # Force finalization of dangling aiohttp ClientSessions (Langfuse /
        # DoubleWord / Prime transports don't close on stack.stop()) so their
        # cosmetic "Unclosed client session" warnings hit the installed
        # filter BEFORE the verdict prints instead of trailing after it.
        gc.collect()

    # -----------------------------------------------------------------
    # Report
    # -----------------------------------------------------------------
    print()
    print("=" * 72)
    print("CAPTURE SUMMARY")
    print("=" * 72)
    print(f"Decision events:   {len(capture.decisions)}")
    print(f"Insufficient fires:{len(capture.insufficients)}")
    print(f"Retry advances:    {capture.retry_advanced}")
    print(f"Raw relevant lines:{len(capture.raw_relevant)}")

    if args.verbose and capture.raw_relevant:
        print()
        print("--- relevant log lines ---")
        for line in capture.raw_relevant:
            print(f"  {line}")

    verdict = _classify(capture, result)

    print()
    print("=" * 72)
    print(f"VERDICT: {verdict.status}")
    print("=" * 72)
    print(verdict.headline)
    for d in verdict.details:
        print(f"  - {d}")
    print()

    return {"PASS": 0, "FAIL": 1, "BORING": 2}[verdict.status]


def main() -> int:
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
