"""REPL command handlers + status renderers for the VisionSensor.

Task 21 of the VisionSensor + Visual VERIFY arc. Provides the
operator-facing surface that SerpentFlow wires up as slash commands
and as inline status output. Keeping these as pure functions
(sensor injected) means:

* the SerpentFlow flowing CLI needs no new imports other than the
  five tiny handlers exposed here,
* every branch is exercisable in isolation without booting the full
  battle-test harness,
* the TTY-only `/vision boost` gate is an injectable probe so CI can
  deny-list it deterministically.

The ``/verify-confirm`` + ``/verify-undemote`` advisory REPL commands
live in ``visual_verify.py`` (Task 19) — this module handles only
the `/vision` family.

Spec: ``docs/superpowers/specs/2026-04-18-vision-sensor-verify-design.md``
§Observability (dashboard status line) + §Cost / Latency Envelope
(boost override).
"""
from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Active-sensor registry (process-global, set by IntakeLayerService at
# boot). Mirrors ``get_protected_app_provider`` pattern from Task 4 —
# the REPL + dashboard consume the sensor reference without needing the
# whole intake-layer object graph.
# ---------------------------------------------------------------------------


_ACTIVE_SENSOR: Optional[Any] = None
_ACTIVE_SENSOR_LOCK = threading.Lock()


def register_active_vision_sensor(sensor: Optional[Any]) -> None:
    """Install (or clear) the active VisionSensor reference.

    Called once by ``IntakeLayerService`` when the sensor is constructed
    during boot. Passing ``None`` clears the registry — useful for
    tests that manage the singleton manually.
    """
    global _ACTIVE_SENSOR
    with _ACTIVE_SENSOR_LOCK:
        _ACTIVE_SENSOR = sensor


def get_active_vision_sensor() -> Optional[Any]:
    """Return the currently-registered VisionSensor, or ``None``."""
    with _ACTIVE_SENSOR_LOCK:
        return _ACTIVE_SENSOR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _default_tty_check() -> bool:
    """Return True when stdin is an interactive TTY.

    ``/vision boost`` refuses to operate in headless / CI environments
    because the spec requires the bypass be explicit. Injectable for
    tests.
    """
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001
        return False


def _cost_cap_str(sensor: Any) -> str:
    cap = getattr(sensor, "_daily_cost_cap_usd", 0.0)
    try:
        return f"${float(cap):.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _cost_today_str(sensor: Any) -> str:
    spent = getattr(sensor, "_cost_today_usd", 0.0)
    try:
        return f"${float(spent):.4f}"
    except (TypeError, ValueError):
        return "$0.0000"


# ---------------------------------------------------------------------------
# /vision status — multi-line detailed dump
# ---------------------------------------------------------------------------


def handle_vision_status(sensor: Optional[Any]) -> str:
    """Render the `/vision status` command output.

    Accepts ``None`` when the sensor wasn't wired (master switch off
    at boot) — returns a compact "not configured" line so the operator
    can tell the difference between "sensor present but idle" and
    "sensor never constructed".
    """
    if sensor is None:
        return (
            "vision: not configured (set JARVIS_VISION_SENSOR_ENABLED=1 "
            "and re-boot to enable)"
        )

    paused = bool(getattr(sensor, "paused", False))
    pause_reason = getattr(sensor, "pause_reason", "") or ""
    state_line = "paused" if paused else "armed"
    if paused and pause_reason:
        state_line = f"paused reason={pause_reason}"

    tier2 = bool(getattr(sensor, "_tier2_enabled", False))
    chain_remaining = int(getattr(sensor, "chain_budget_remaining", 0) or 0)
    chain_max = int(getattr(sensor, "_chain_max", 0) or 0)

    stats = getattr(sensor, "stats", None)
    signals = int(getattr(stats, "signals_emitted", 0) or 0) if stats else 0
    tier2_calls = int(getattr(stats, "tier2_calls", 0) or 0) if stats else 0

    fp_rate_fn = getattr(sensor, "fp_rate", None)
    try:
        rate = fp_rate_fn() if callable(fp_rate_fn) else None
    except Exception:  # noqa: BLE001
        rate = None
    if rate is None:
        fp_line = "FP rate: n/a (window not full)"
    else:
        fp_line = f"FP rate: {rate:.2%}"

    cost_line = f"today: {_cost_today_str(sensor)} / cap {_cost_cap_str(sensor)}"

    boost_active = False
    boost_fn = getattr(sensor, "is_boost_active", None)
    if callable(boost_fn):
        try:
            boost_active = bool(boost_fn())
        except Exception:  # noqa: BLE001
            boost_active = False
    if boost_active:
        remaining = 0.0
        rem_fn = getattr(sensor, "boost_remaining_s", None)
        if callable(rem_fn):
            try:
                remaining = float(rem_fn())
            except Exception:  # noqa: BLE001
                remaining = 0.0
        boost_line = f"boost: active {remaining:.0f}s remaining"
    else:
        boost_line = "boost: off"

    lines = [
        f"vision: {state_line}",
        f"  tier2: {'on' if tier2 else 'off'}   chain: {chain_remaining}/{chain_max}   signals: {signals}   tier2_calls: {tier2_calls}",
        f"  {fp_line}",
        f"  {cost_line}",
        f"  {boost_line}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# /vision resume — clear a manual/FP-budget/chain-cap/cost pause
# ---------------------------------------------------------------------------


def handle_vision_resume(sensor: Optional[Any]) -> str:
    """Clear the sensor's pause state (delegates to ``sensor.resume``)."""
    if sensor is None:
        return "vision: not configured — nothing to resume"
    try:
        was_paused = bool(getattr(sensor, "paused", False))
        prev_reason = getattr(sensor, "pause_reason", "") or ""
    except Exception:  # noqa: BLE001
        was_paused = False
        prev_reason = ""
    resume_fn = getattr(sensor, "resume", None)
    if not callable(resume_fn):
        return "vision: sensor has no resume() method (incompatible version?)"
    try:
        resume_fn()
    except Exception as exc:  # noqa: BLE001
        return f"vision: resume failed: {exc}"
    if was_paused:
        return f"vision: resumed (cleared pause reason={prev_reason})"
    return "vision: already armed (no pause to clear)"


# ---------------------------------------------------------------------------
# /vision boost — operator-triggered cost-cascade bypass
# ---------------------------------------------------------------------------


def handle_vision_boost(
    sensor: Optional[Any],
    args: str,
    *,
    tty_check_fn: Callable[[], bool] = _default_tty_check,
) -> str:
    """Parse ``<seconds>`` and enable a boost window on the sensor.

    Gated by ``tty_check_fn()`` — refuses in CI / headless because the
    spec requires the operator be physically present to accept the
    spend. ``seconds`` is clamped to ``[1, 300]`` by the sensor.
    """
    if sensor is None:
        return "vision: not configured — nothing to boost"

    if not tty_check_fn():
        return (
            "/vision boost: refused — REPL is non-interactive / headless. "
            "The boost override requires explicit operator presence."
        )

    tokens = (args or "").strip().split()
    if len(tokens) != 1:
        return (
            "usage: /vision boost <seconds>\n"
            "  temporarily suppresses the cost cascade (max 300s)."
        )
    try:
        seconds = float(tokens[0])
    except ValueError:
        return f"/vision boost: invalid seconds value {tokens[0]!r}"
    if seconds <= 0:
        return "/vision boost: seconds must be > 0"

    enable_fn = getattr(sensor, "enable_boost", None)
    if not callable(enable_fn):
        return "/vision boost: sensor has no enable_boost() method"
    try:
        granted = float(enable_fn(seconds))
    except Exception as exc:  # noqa: BLE001
        return f"/vision boost: enable_boost failed: {exc}"

    clamped_msg = " (clamped to 300s max)" if seconds > 300.0 else ""
    return (
        f"/vision boost: active for {granted:.0f}s{clamped_msg}. "
        f"Cost cascade suppressed until boost window expires."
    )


# ---------------------------------------------------------------------------
# Vision status line — single-line render for inline SerpentFlow output
# ---------------------------------------------------------------------------


def format_vision_status_line(sensor: Optional[Any]) -> str:
    """Render the dashboard's single-line status.

    Target format (spec §Observability):

        vision: armed today=$0.015 / $1.00
        vision: paused reason=fp_budget_exhausted today=$0.25 / $1.00
        vision: armed boost=250s today=$0.85 / $1.00

    Designed to fit in one terminal cell under Rich's compact mode;
    keep tokens short and space-delimited for easy grepping.
    """
    if sensor is None:
        return "vision: off"

    paused = bool(getattr(sensor, "paused", False))
    reason = getattr(sensor, "pause_reason", "") or ""
    state = (
        f"paused reason={reason}" if paused and reason else
        "paused" if paused else
        "armed"
    )

    boost_token = ""
    boost_fn = getattr(sensor, "is_boost_active", None)
    if callable(boost_fn):
        try:
            if boost_fn():
                rem_fn = getattr(sensor, "boost_remaining_s", None)
                remaining = float(rem_fn()) if callable(rem_fn) else 0.0
                boost_token = f" boost={remaining:.0f}s"
        except Exception:  # noqa: BLE001
            pass

    return (
        f"vision: {state}{boost_token} "
        f"today={_cost_today_str(sensor)} / {_cost_cap_str(sensor)}"
    )


# ---------------------------------------------------------------------------
# [vision-origin] tag — prefix for SerpentFlow Update blocks
# ---------------------------------------------------------------------------


def vision_origin_tag(source: Optional[str]) -> str:
    """Return ``[vision-origin] `` (with trailing space) for vision-
    originated envelopes, else empty string.

    Takes the envelope's ``source`` string directly — lightweight
    enough that SerpentFlow can call it inside a tight render loop
    without a per-op object construction cost.
    """
    if not source:
        return ""
    if source.strip().lower() == "vision_sensor":
        return "[vision-origin] "
    return ""
