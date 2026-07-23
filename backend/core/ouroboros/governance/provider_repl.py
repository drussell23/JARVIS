"""``/provider`` — operator-facing DoubleWord resilience dashboard.

Surfaces the resilience telemetry the headless Sentinel writes to the shared
``.jarvis/chunk_strategy.db`` substrate — provider state, the Provider Jitter
Index + adaptive-hysteresis threshold, the ΔTTFT health gradient, and the
forecaster's predicted recovery — into the SerpentFlow CLI. Read-only: it opens
the SQLite substrate and renders; it never mutates state, never calls the
network, and imports NO orchestrator / providers / iron_gate (naming-cage
authority asymmetry). Auto-discovered by ``repl_dispatch_registry.try_dispatch``
via the module-level ``dispatch_provider_command`` callable.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Optional

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


@dataclass(frozen=True)
class ProviderReplDispatchResult:
    """Result of a ``/provider`` dispatch. ``matched=False`` → not our line."""
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    f"  {_BOLD}{_CYAN}/provider — DoubleWord resilience dashboard{_RESET}\n"
    f"  {_DIM}Read-only view of the Sentinel's live provider telemetry "
    f"(state, jitter, ΔTTFT gradient, forecast).{_RESET}\n\n"
    f"  {_BOLD}Subcommands:{_RESET}\n"
    f"    {_CYAN}/provider{_RESET}         "
    f"{_DIM}state + jitter + adaptive threshold + ΔTTFT + forecast{_RESET}\n"
    f"    {_CYAN}/provider help{_RESET}    {_DIM}this message{_RESET}\n"
)


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return s in ("/provider", "provider") or s.startswith(("/provider ", "provider "))


def _fmt_ago(updated_ts: Optional[float], now: Optional[float] = None) -> str:
    if not updated_ts:
        return "?"
    import time
    ref = time.time() if now is None else now
    d = max(0.0, ref - float(updated_ts))
    if d < 90:
        return f"{int(d)}s ago"
    if d < 5400:
        return f"{int(d / 60)}m ago"
    return f"{d / 3600:.1f}h ago"


def _render_overview(conn, *, now: Optional[float] = None) -> str:
    """Compose the dashboard from an open telemetry conn. Never raises."""
    try:
        from backend.core.ouroboros.governance.provider_state import get_provider_state
        from backend.core.ouroboros.governance.provider_jitter import (
            jitter_index, required_consecutive_passes,
        )
        from backend.core.ouroboros.governance.dw_outage_forecaster import (
            forecast_ttr, ttft_slope,
        )
    except Exception:  # noqa: BLE001
        return f"  {_DIM}provider telemetry modules unavailable{_RESET}"

    if conn is None:
        return (
            f"  {_DIM}no telemetry DB yet — the Sentinel has not written "
            f"provider_state (is dw_sentinel_daemon running?){_RESET}"
        )

    provider = "doubleword"
    st = get_provider_state(conn, provider)
    state = (st or {}).get("state", "UNKNOWN")
    reason = (st or {}).get("reason", "")
    updated_ts = (st or {}).get("updated_ts")

    scolor = _GREEN if state == "HEALTHY" else (_RED if state == "DEGRADED" else _YELLOW)
    dot = "●"

    ji = jitter_index(conn, provider, now=now)
    req = required_consecutive_passes(conn, provider, now=now)
    jcolor = _GREEN if ji == 0 else (_YELLOW if ji < 5 else _RED)

    slope = ttft_slope(conn, provider, now=now)
    if slope is None:
        grad = f"{_DIM}n/a (need ≥2 successful probes){_RESET}"
    elif slope < 0:
        grad = f"{_GREEN}▼ {slope:+.4f} s/s (VRAM stabilizing){_RESET}"
    else:
        grad = f"{_RED}▲ {slope:+.4f} s/s (latency worsening){_RESET}"

    ttr = forecast_ttr(conn, )

    lines = [
        f"  {_BOLD}{_CYAN}DoubleWord — Resilience{_RESET}",
        f"    {_BOLD}State{_RESET}       {scolor}{dot} {state}{_RESET} "
        f"{_DIM}({reason}, {_fmt_ago(updated_ts, now)}){_RESET}",
        f"    {_BOLD}Jitter{_RESET}      {jcolor}{ji}{_RESET} "
        f"{_DIM}transient errors / 30m{_RESET}",
        f"    {_BOLD}Threshold{_RESET}   {req} "
        f"{_DIM}consecutive 2-stage passes required (adaptive hysteresis){_RESET}",
        f"    {_BOLD}ΔTTFT{_RESET}       {grad}",
        f"    {_BOLD}Forecast{_RESET}    {_DIM}predicted outage ≈ {ttr:.0f}s (EMA/Bayesian){_RESET}",
    ]
    if state != "HEALTHY":
        lines.append(
            f"  {_DIM}→ Sentinel holds until {req} consecutive full passes land; "
            f"launch when it writes HEALTHY.{_RESET}"
        )
    return "\n".join(lines)


def dispatch_provider_command(line: str) -> ProviderReplDispatchResult:
    """Parse a ``/provider`` line and render the dashboard. NEVER raises."""
    if not _matches(line):
        return ProviderReplDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return ProviderReplDispatchResult(ok=False, text=f"  /provider parse error: {exc}")
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "")

    if head in ("help", "?"):
        return ProviderReplDispatchResult(ok=True, text=_HELP)

    try:
        from backend.core.ouroboros.governance.dw_outage_forecaster import open_forecast_db
        conn = open_forecast_db()
    except Exception:  # noqa: BLE001
        conn = None
    return ProviderReplDispatchResult(ok=True, text=_render_overview(conn))


def register_verbs(registry) -> int:
    """Auto-discovered by the /help index registrar. Registers ``/provider``."""
    try:
        registry.register(
            verb="provider",
            description=(
                "DoubleWord resilience dashboard — provider state, jitter index + "
                "adaptive-hysteresis threshold, ΔTTFT health gradient, and the "
                "outage forecaster. Read-only view of the Sentinel's SQLite telemetry."
            ),
            posture_relevance="RELEVANT",
            since="Sentinel telemetry surface (2026-07-23)",
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "ProviderReplDispatchResult",
    "dispatch_provider_command",
    "register_verbs",
]
