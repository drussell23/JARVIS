"""Glanceable one-line operator status for the Ouroboros battle-test CLI.

Closes UX Priority 2B: operators want a scannable one-liner of current
state. Target format (example):

    Phase: L2 Repair 2/8 · Cost: $0.22 / $0.50 · Idle: 847s / 2400s
    · Op: 019d9368 [complex·claude]

This module owns the data aggregation + format contract. The flowing
SerpentFlow CLI consumes it via the ``/status`` REPL command and via
event-driven receipt lines on op completion (UI Slices 5-6, 2026-04-30).
The legacy ``render_prompt_toolkit()`` path that fed a persistent
bottom toolbar is retired as of UI Slice 3 — see
``memory/project_move_2_closure.md`` for context on why fixed UI panels
were removed in favor of a pure flowing CLI.

Architectural mandates (matching stream_renderer / diff_preview):

  • **Pull model, no subscriptions** — builder holds weak refs to the
    ``CostTracker``, ``IdleWatchdog``, ``GovernedLoopService``,
    ``RepairEngine``. On each render call (~500ms via
    ``PromptSession(refresh_interval=…)``), it pulls current state,
    formats the one-liner, returns. No event wiring, no background task.
  • **Kill switch** — ``JARVIS_UI_STATUS_LINE_ENABLED`` (default on).
    When off, ``render()`` returns the empty string and SerpentFlow's
    toolbar falls back to its legacy verbose content.
  • **TTY gate** — same pattern as diff_preview / stream_renderer:
    non-TTY → skip rendering.
  • **Compact mode** — ``JARVIS_UI_STATUS_LINE_COMPACT=1`` drops
    route badge + op tail; keeps Phase + Cost + Idle.
  • **Super-beef extras** (all env-tunable):
        - Color gradient (green <50%, yellow 50-80%, red >80%) on
          Cost/Idle bars
        - Phase sub-detail (``L2 Repair 2/8``, ``GENERATE 47s``,
          ``APPLY mode=multi/4``, ``VALIDATE retry 1/2``)
        - Route + provider badge (``[complex·claude]`` / ``[bg·dw]``)
        - Multi-op indicator (``Op: 019d9368 (+2)``)
        - Proactive warnings inline at >80% cost / idle
        - 500ms refresh (``JARVIS_UI_STATUS_LINE_REFRESH_MS``)
        - Op-id truncation (last 10 chars)

Authority invariant: this module writes ONLY to the terminal's status
region. It does NOT mutate cost, idle timer, FSM state, risk tier,
cancel flag, or any governance surface. Pure read-only.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, List, Optional

logger = logging.getLogger("Ouroboros.StatusLine")

_ENV_ENABLED = "JARVIS_UI_STATUS_LINE_ENABLED"
_ENV_COMPACT = "JARVIS_UI_STATUS_LINE_COMPACT"
_ENV_REFRESH_MS = "JARVIS_UI_STATUS_LINE_REFRESH_MS"
_ENV_WARN_PCT = "JARVIS_UI_STATUS_LINE_WARN_PCT"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def status_line_enabled() -> bool:
    """Master kill switch. Default: ON."""
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def compact_mode_enabled() -> bool:
    """Compact layout gate. Default: OFF (full line)."""
    return os.environ.get(_ENV_COMPACT, "0").strip().lower() in _TRUTHY


def refresh_interval_s() -> float:
    """Refresh cadence used by the PromptSession. Default: 500ms."""
    try:
        ms = int(os.environ.get(_ENV_REFRESH_MS, "500"))
    except (TypeError, ValueError):
        ms = 500
    return max(0.1, min(5.0, ms / 1000.0))


def warn_threshold_pct() -> int:
    """Threshold above which Cost/Idle bars show the ⚠ marker. Default 80."""
    try:
        pct = int(os.environ.get(_ENV_WARN_PCT, "80"))
    except (TypeError, ValueError):
        pct = 80
    return max(1, min(99, pct))


# ---------------------------------------------------------------------------
# StatusSnapshot — immutable snapshot used by render()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusSnapshot:
    """Point-in-time aggregate of everything the one-liner shows.

    Held immutable so test cases can construct one by hand and exercise
    the rendering contract without booting the full harness.
    """

    # Phase + sub-detail
    phase: str = "IDLE"                # e.g. "GENERATE", "VALIDATE", "L2", "APPLY"
    phase_detail: str = ""             # e.g. "2/8" for L2, "47s" for elapsed
    # Cost
    cost_spent_usd: float = 0.0
    cost_budget_usd: float = 0.0
    # Idle window
    idle_elapsed_s: float = 0.0
    idle_timeout_s: float = 0.0
    # Active op
    primary_op_id: str = ""
    extra_op_count: int = 0            # >0 triggers "(+N)" suffix
    # Route / provider badge
    route: str = ""                    # "complex" / "standard" / "background" / ...
    provider: str = ""                 # "claude" / "dw" / "prime" / ""


# ---------------------------------------------------------------------------
# StatusLineBuilder — aggregates live state → one-line render
# ---------------------------------------------------------------------------


class StatusLineBuilder:
    """Pull-model aggregator for the glanceable status line.

    Holds references to the four live state sources (cost tracker, idle
    watchdog, GLS, repair engine). Any ref may be ``None`` — the builder
    degrades to sensible defaults (e.g. no ref → phase="IDLE").
    """

    def __init__(
        self,
        *,
        cost_tracker: Any = None,
        idle_watchdog: Any = None,
        governed_loop_service: Any = None,
        repair_engine: Any = None,
    ) -> None:
        self._cost = cost_tracker
        self._idle = idle_watchdog
        self._gls = governed_loop_service
        # Repair engine may be passed explicitly (tests, direct wiring)
        # OR resolved lazily from ``gls._orchestrator._config.repair_engine``
        # during each snapshot — preferred because the harness doesn't
        # hold the engine directly (it's owned by GLS / the orchestrator).
        self._repair_explicit = repair_engine

    def _resolve_repair_engine(self) -> Any:
        """Prefer explicit ref when provided; else walk GLS.

        Defensive: any attribute error returns None — missing repair
        engine just means the status line skips the L2-iter sub-detail.
        """
        if self._repair_explicit is not None:
            return self._repair_explicit
        if self._gls is None:
            return None
        try:
            orch = getattr(self._gls, "_orchestrator", None)
            if orch is None:
                return None
            cfg = getattr(orch, "_config", None)
            if cfg is None:
                return None
            return getattr(cfg, "repair_engine", None)
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    # Public API — snapshot + render
    # ------------------------------------------------------------------

    def snapshot(self) -> StatusSnapshot:
        """Sample current state from all refs and return an immutable snapshot.

        Never raises: any missing attribute / exception degrades to the
        field's default. The status line must never break the TUI even
        if the harness is mid-reload / mid-boot.
        """
        phase, phase_detail = self._sample_phase_and_detail()
        cost_spent, cost_budget = self._sample_cost()
        idle_elapsed, idle_timeout = self._sample_idle()
        primary_op, extra_ops = self._sample_ops()
        route, provider = self._sample_route_and_provider(primary_op)

        return StatusSnapshot(
            phase=phase,
            phase_detail=phase_detail,
            cost_spent_usd=cost_spent,
            cost_budget_usd=cost_budget,
            idle_elapsed_s=idle_elapsed,
            idle_timeout_s=idle_timeout,
            primary_op_id=primary_op,
            extra_op_count=extra_ops,
            route=route,
            provider=provider,
        )

    def render_prompt_toolkit(self) -> str:
        """Render for the prompt_toolkit ``bottom_toolbar`` callable.

        Returns an HTML string with prompt_toolkit's inline style tags.
        Empty string when the master kill-switch is off — the caller
        should fall back to its legacy toolbar content in that case.
        """
        if not status_line_enabled():
            return ""
        try:
            snap = self.snapshot()
            return _format_html(snap, compact=compact_mode_enabled())
        except Exception:  # noqa: BLE001
            logger.debug(
                "[StatusLine] render failed; empty line returned",
                exc_info=True,
            )
            return ""

    def render_plain(self) -> str:
        """Plain ANSI-free rendering for logs / unit tests."""
        if not status_line_enabled():
            return ""
        try:
            snap = self.snapshot()
            return _format_plain(snap, compact=compact_mode_enabled())
        except Exception:  # noqa: BLE001
            logger.debug(
                "[StatusLine] plain render failed", exc_info=True,
            )
            return ""

    # ------------------------------------------------------------------
    # Samplers — each guards against missing refs / missing attrs
    # ------------------------------------------------------------------

    def _sample_cost(self) -> tuple:
        if self._cost is None:
            return (0.0, 0.0)
        try:
            spent = float(getattr(self._cost, "total_spent", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            spent = 0.0
        try:
            budget = float(
                getattr(self._cost, "budget_usd", 0.0)
                or getattr(self._cost, "_budget_usd", 0.0)
                or 0.0
            )
        except Exception:  # noqa: BLE001
            budget = 0.0
        return (spent, budget)

    def _sample_idle(self) -> tuple:
        if self._idle is None:
            return (0.0, 0.0)
        try:
            timeout = float(
                getattr(self._idle, "timeout_s", 0.0)
                or getattr(self._idle, "_timeout_s", 0.0)
                or 0.0
            )
        except Exception:  # noqa: BLE001
            timeout = 0.0
        # IdleWatchdog doesn't expose ``elapsed`` as a public property;
        # we compute it from ``_last_poke`` with defensive fallbacks.
        elapsed = 0.0
        try:
            last_poke = getattr(self._idle, "_last_poke", None)
            if last_poke is not None:
                elapsed = max(0.0, time.monotonic() - float(last_poke))
            else:
                # Try diagnostics snapshot if private field shape changed.
                diag = getattr(self._idle, "diagnostics", None)
                if diag is not None:
                    elapsed = float(
                        getattr(diag, "seconds_since_last_poke", 0.0) or 0.0
                    )
        except Exception:  # noqa: BLE001
            elapsed = 0.0
        return (elapsed, timeout)

    def _sample_ops(self) -> tuple:
        """Return (primary_op_id, extra_op_count).

        Picks the op whose FSM context was most recently advanced (proxy
        for "what the operator is watching"). ``extra_op_count`` is the
        number of additional in-flight ops.
        """
        if self._gls is None:
            return ("", 0)
        try:
            active = getattr(self._gls, "_active_ops", None) or set()
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
        except Exception:  # noqa: BLE001
            return ("", 0)

        if not active and not fsm_contexts:
            return ("", 0)

        # Prefer FSM-context ordering; pick the op with the largest
        # ``phase_entered_at`` (= most recent transition).
        primary: Optional[str] = None
        primary_ts: float = -1.0
        ids: List[str] = []
        for op_id, fsm_ctx in fsm_contexts.items():
            ids.append(op_id)
            try:
                pe = getattr(fsm_ctx, "phase_entered_at", None)
                # ``phase_entered_at`` is a datetime on OperationContext.
                # Comparison via .timestamp() — missing → skip.
                if pe is not None:
                    ts = float(pe.timestamp())
                    if ts > primary_ts:
                        primary_ts = ts
                        primary = op_id
            except Exception:  # noqa: BLE001
                pass

        if primary is None and ids:
            primary = ids[0]

        total = len(fsm_contexts) if fsm_contexts else len(active)
        extras = max(0, total - 1)
        return (primary or "", extras)

    def _sample_phase_and_detail(self) -> tuple:
        """Return (phase, phase_detail).

        Sub-detail resolution order (first match wins):
          1. L2 Repair iteration (``repair_engine.is_running``)
          2. FSM phase name of the primary op + elapsed-in-phase
        """
        # L2 Repair has highest-priority detail — it's the only phase
        # where operators explicitly asked for an iter/max breakdown.
        _repair = self._resolve_repair_engine()
        if _repair is not None:
            try:
                if getattr(_repair, "is_running", False):
                    cur = int(
                        getattr(_repair, "current_iteration", 0) or 0
                    )
                    mx = int(
                        getattr(_repair, "max_iterations_live", 0) or 0
                    )
                    if mx > 0:
                        return ("L2 Repair", f"{cur}/{mx}")
                    return ("L2 Repair", str(cur) if cur else "")
            except Exception:  # noqa: BLE001
                pass

        if self._gls is None:
            return ("IDLE", "")

        try:
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
        except Exception:  # noqa: BLE001
            fsm_contexts = {}
        if not fsm_contexts:
            return ("IDLE", "")

        # Pick the most-recently-entered phase across all ops (same
        # selector as _sample_ops for consistency).
        primary_phase: str = ""
        primary_ts: float = -1.0
        primary_entered_at = None
        for fsm_ctx in fsm_contexts.values():
            try:
                pe = getattr(fsm_ctx, "phase_entered_at", None)
                phase_obj = getattr(fsm_ctx, "phase", None)
                if pe is None or phase_obj is None:
                    continue
                ts = float(pe.timestamp())
                if ts > primary_ts:
                    primary_ts = ts
                    primary_phase = _phase_label(phase_obj)
                    primary_entered_at = pe
            except Exception:  # noqa: BLE001
                continue

        if not primary_phase:
            return ("IDLE", "")

        # Elapsed-in-phase sub-detail (compact, e.g. "47s"). Only show
        # for phases where "how long has this been running?" is useful —
        # GENERATE / VALIDATE / APPLY / VERIFY.
        detail = ""
        try:
            if primary_entered_at is not None and primary_phase in {
                "GENERATE", "VALIDATE", "APPLY", "VERIFY",
            }:
                from datetime import datetime, timezone
                elapsed_s = (
                    datetime.now(tz=timezone.utc) - primary_entered_at
                ).total_seconds()
                if elapsed_s >= 1.0:
                    detail = f"{int(elapsed_s)}s"
        except Exception:  # noqa: BLE001
            pass

        return (primary_phase, detail)

    def _sample_route_and_provider(self, op_id: str) -> tuple:
        """Pull route + provider for the primary op. Both optional."""
        if not op_id or self._gls is None:
            return ("", "")
        try:
            fsm_contexts = getattr(self._gls, "_fsm_contexts", None) or {}
            ctx = fsm_contexts.get(op_id)
            if ctx is None:
                return ("", "")
            route = str(getattr(ctx, "provider_route", "") or "").lower()
            # Provider usually on ctx.generation.provider_name post-GENERATE.
            provider = ""
            gen = getattr(ctx, "generation", None)
            if gen is not None:
                provider = str(getattr(gen, "provider_name", "") or "").lower()
            return (route, provider)
        except Exception:  # noqa: BLE001
            return ("", "")


# ---------------------------------------------------------------------------
# Helpers — phase label, formatting, color thresholds
# ---------------------------------------------------------------------------


def _phase_label(phase_obj: Any) -> str:
    """Coerce an OperationPhase enum (or any object) into a short label."""
    try:
        name = getattr(phase_obj, "name", None)
        if name:
            return str(name)
        return str(phase_obj)
    except Exception:  # noqa: BLE001
        return "?"


def _cost_fraction(spent: float, budget: float) -> float:
    if budget <= 0:
        return 0.0
    return max(0.0, min(1.0, spent / budget))


def _idle_fraction(elapsed: float, timeout: float) -> float:
    if timeout <= 0:
        return 0.0
    return max(0.0, min(1.0, elapsed / timeout))


def _level_for_fraction(fraction: float) -> str:
    """Gradient level key: 'ok' (<50%) → 'warn' (50-80%) → 'hot' (>80%)."""
    if fraction >= (warn_threshold_pct() / 100.0):
        return "hot"
    if fraction >= 0.5:
        return "warn"
    return "ok"


# Color tokens keyed by level, per render backend.
_HTML_COLORS = {"ok": "ansigreen", "warn": "ansiyellow", "hot": "ansired"}


def _short_op_id(op_id: str) -> str:
    if not op_id:
        return ""
    # Trim the suffix variants the orchestrator appends ("-cau", "-lse", etc.).
    core = op_id.split("-", 1)[1] if op_id.count("-") >= 1 else op_id
    # Show just the first prefix-chunk for scannability.
    return core.split("-", 1)[0] if "-" in core else core[:10]


def _format_phase(snap: StatusSnapshot) -> str:
    if snap.phase_detail:
        return f"{snap.phase} {snap.phase_detail}"
    return snap.phase or "IDLE"


def _format_badge(route: str, provider: str) -> str:
    """Compact route·provider badge. Empty when neither present."""
    if not route and not provider:
        return ""
    # Abbreviate long route names.
    route_abbrev = {
        "immediate": "imm",
        "standard": "std",
        "complex": "complex",
        "background": "bg",
        "speculative": "spec",
    }.get(route, route)
    prov_abbrev = {
        "claude": "claude",
        "doubleword": "dw",
        "dw": "dw",
        "prime": "prime",
        "j-prime": "prime",
    }.get(provider, provider)
    parts = [p for p in (route_abbrev, prov_abbrev) if p]
    return "[" + "·".join(parts) + "]" if parts else ""


# ---------------------------------------------------------------------------
# Render backends
# ---------------------------------------------------------------------------


def _format_plain(snap: StatusSnapshot, *, compact: bool) -> str:
    """Plain-text rendering for tests / non-TTY logs."""
    cost_fr = _cost_fraction(snap.cost_spent_usd, snap.cost_budget_usd)
    idle_fr = _idle_fraction(snap.idle_elapsed_s, snap.idle_timeout_s)
    parts: List[str] = []

    phase_txt = _format_phase(snap)
    parts.append(f"Phase: {phase_txt}")

    cost_txt = f"Cost: ${snap.cost_spent_usd:.2f} / ${snap.cost_budget_usd:.2f}"
    if cost_fr >= (warn_threshold_pct() / 100.0):
        cost_txt += " ⚠"
    parts.append(cost_txt)

    idle_txt = (
        f"Idle: {int(snap.idle_elapsed_s)}s / {int(snap.idle_timeout_s)}s"
    )
    if idle_fr >= (warn_threshold_pct() / 100.0):
        idle_txt += " ⚠"
    parts.append(idle_txt)

    if not compact and snap.primary_op_id:
        op_txt = f"Op: {_short_op_id(snap.primary_op_id)}"
        if snap.extra_op_count > 0:
            op_txt += f" (+{snap.extra_op_count})"
        parts.append(op_txt)

    if not compact:
        badge = _format_badge(snap.route, snap.provider)
        if badge:
            parts.append(badge)

    return " · ".join(parts)


def _format_html(snap: StatusSnapshot, *, compact: bool) -> str:
    """prompt_toolkit HTML rendering with color gradient."""
    from html import escape

    cost_fr = _cost_fraction(snap.cost_spent_usd, snap.cost_budget_usd)
    idle_fr = _idle_fraction(snap.idle_elapsed_s, snap.idle_timeout_s)
    cost_color = _HTML_COLORS[_level_for_fraction(cost_fr)]
    idle_color = _HTML_COLORS[_level_for_fraction(idle_fr)]

    parts: List[str] = []

    phase_txt = escape(_format_phase(snap))
    parts.append(f"<b>Phase:</b> <ansicyan>{phase_txt}</ansicyan>")

    cost_inner = (
        f"${snap.cost_spent_usd:.2f} / ${snap.cost_budget_usd:.2f}"
    )
    if cost_fr >= (warn_threshold_pct() / 100.0):
        cost_inner += " ⚠"
    parts.append(
        f"<b>Cost:</b> <{cost_color}>{escape(cost_inner)}</{cost_color}>"
    )

    idle_inner = (
        f"{int(snap.idle_elapsed_s)}s / {int(snap.idle_timeout_s)}s"
    )
    if idle_fr >= (warn_threshold_pct() / 100.0):
        idle_inner += " ⚠"
    parts.append(
        f"<b>Idle:</b> <{idle_color}>{escape(idle_inner)}</{idle_color}>"
    )

    if not compact and snap.primary_op_id:
        op_inner = escape(_short_op_id(snap.primary_op_id))
        if snap.extra_op_count > 0:
            op_inner += f" (+{snap.extra_op_count})"
        parts.append(
            f"<b>Op:</b> <ansimagenta>{op_inner}</ansimagenta>"
        )

    if not compact:
        badge = _format_badge(snap.route, snap.provider)
        if badge:
            parts.append(f"<ansiblue>{escape(badge)}</ansiblue>")

    return " <ansiwhite>·</ansiwhite> ".join(parts)


# ---------------------------------------------------------------------------
# TTY gate + module-level singleton
# ---------------------------------------------------------------------------


def should_render() -> bool:
    """Combined gate: env enabled + stdout is a real TTY."""
    if not status_line_enabled():
        return False
    try:
        return bool(sys.stdout.isatty())
    except Exception:  # noqa: BLE001
        return False


_DEFAULT_BUILDER: Optional[StatusLineBuilder] = None


def register_status_line_builder(builder: Optional[StatusLineBuilder]) -> None:
    """Harness calls this at boot once CostTracker / IdleWatchdog / GLS /
    RepairEngine are all constructed. SerpentFlow's toolbar looks up
    via :func:`get_status_line_builder`."""
    global _DEFAULT_BUILDER
    _DEFAULT_BUILDER = builder


def get_status_line_builder() -> Optional[StatusLineBuilder]:
    return _DEFAULT_BUILDER


def reset_status_line_builder() -> None:
    """Clear the singleton. Primarily for tests."""
    global _DEFAULT_BUILDER
    _DEFAULT_BUILDER = None
