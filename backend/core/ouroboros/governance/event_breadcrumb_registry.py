"""Event Breadcrumb Registry — connect the ENTIRE backend event surface to the CLI.

The ``StreamEventBroker`` carries ~149 event types, but the TUI historically
surfaced them with BESPOKE per-type listener methods (one clone per event). That
does not scale and leaves most of the organism invisible. This is the descriptor-
driven replacement — the same pattern as ``ToolRenderRegistry``:

  * A ``BreadcrumbDescriptor`` maps an ``event_type`` → glyph + color + severity +
    a payload formatter.
  * The registry seeds tailored descriptors for the high-value events, and — the
    key move — SYNTHESIZES a descriptor for ANY unregistered type via a
    name-based severity heuristic + a generic key/value formatter. So a brand-new
    backend event lights up the CLI with ZERO TUI code, at a sensible severity.
  * A single router (in the TUI) subscribes ONCE and renders every event through
    this registry, filtered by the operator's chosen verbosity and de-flooded by
    a coalescer.

No hardcoded per-event ``if/elif`` in the TUI; no duplication; new events are
first-class automatically. Pure formatting — imports nothing heavy, touches no
model-selection path (the Fable model is never flagged). Never raises.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

# ── Severity ranks (higher = louder). Operator filter shows rank >= threshold. ──
SEV_VERBOSE = 0
SEV_INFO = 1
SEV_IMPORTANT = 2
SEV_CRITICAL = 3

_SEV_NAMES = {SEV_VERBOSE: "verbose", SEV_INFO: "info",
              SEV_IMPORTANT: "important", SEV_CRITICAL: "critical"}
_NAME_TO_SEV = {v: k for k, v in _SEV_NAMES.items()}
_NAME_TO_SEV["all"] = SEV_VERBOSE
_NAME_TO_SEV["off"] = 99  # sentinel — show nothing

_LEVEL_ENV = "JARVIS_TUI_EVENT_BREADCRUMB_LEVEL"
_DEFAULT_LEVEL = SEV_IMPORTANT


PayloadFormatter = Callable[[dict], str]


@dataclass(frozen=True)
class BreadcrumbDescriptor:
    event_type: str
    glyph: str
    color: str                     # a Rich color/style token
    severity: int
    formatter: Optional[PayloadFormatter] = None
    category: str = "general"


# ---------------------------------------------------------------------------
# Name-based heuristics — how UNREGISTERED events get a sensible descriptor
# ---------------------------------------------------------------------------

_CRIT_MARKERS = ("tripped", "exhausted", "emergency", "anomaly", "overrun",
                 "sustained_low", "circuit_tripped")
_IMPORTANT_MARKERS = ("degraded", "throttle", "pressure", "drift", "_drop",
                      "warning", "brake", "rejected", "expired", "denied",
                      "confidence", "cost_band", "budget_action", "failure",
                      "state_change", "state_changed", "approaching")
_INFO_MARKERS = ("changed", "detected", "crossed", "graduated", "approved",
                 "absorbed", "decomposed", "proposal", "learned", "fused",
                 "convergence", "calibrated", "found", "generated")
_VERBOSE_MARKERS = ("heartbeat", "tick", "lag", "rendered", "snapshot",
                    "updated", "progress", "prefetch", "compacted", "created",
                    "started", "completed", "cancelled", "closed", "replay",
                    "dashboard", "portrait", "story", "flow", "ledger",
                    "browsing", "dream", "cue", "scheduled")

_CATEGORY_PREFIXES = (
    ("provider", "provider"), ("circuit_breaker", "provider"),
    ("golden_image", "provider"), ("governor", "governance"),
    ("memory", "memory"), ("posture", "posture"), ("plan", "plan"),
    ("inline_", "permission"), ("shadow", "permission"), ("cost", "cost"),
    ("budget", "cost"), ("model_", "model"), ("drift", "integrity"),
    ("git_", "integrity"), ("flag_", "flags"), ("task_", "tasks"),
    ("soak", "soak"), ("session", "session"),
)


def _humanize(event_type: str) -> str:
    return event_type.replace("_", " ").strip()


def _heuristic_severity(event_type: str) -> int:
    t = event_type.lower()
    if any(m in t for m in _CRIT_MARKERS):
        return SEV_CRITICAL
    if any(m in t for m in _IMPORTANT_MARKERS):
        return SEV_IMPORTANT
    if any(m in t for m in _VERBOSE_MARKERS):
        return SEV_VERBOSE
    if any(m in t for m in _INFO_MARKERS):
        return SEV_INFO
    return SEV_INFO


def _heuristic_category(event_type: str) -> str:
    t = event_type.lower()
    for prefix, cat in _CATEGORY_PREFIXES:
        if t.startswith(prefix) or prefix in t:
            return cat
    return "general"


def _generic_formatter(payload: dict) -> str:
    """Compact, safe key=value summary of the most salient payload fields. Skips
    bulky / noisy values so an unknown event still reads cleanly. Never raises."""
    if not isinstance(payload, dict) or not payload:
        return ""
    skip = {"op_id", "event_id", "ts", "timestamp", "schema_version",
            "monotonic_ts", "seq", "provider"}
    parts = []
    for k, v in payload.items():
        if k in skip:
            continue
        if isinstance(v, (dict, list)):
            continue
        s = str(v)
        if not s or len(s) > 48:
            continue
        parts.append(f"{k}={s}")
        if len(parts) >= 3:
            break
    return " ".join(parts)


_GLYPH_BY_SEV = {SEV_CRITICAL: "✖", SEV_IMPORTANT: "▲", SEV_INFO: "·", SEV_VERBOSE: "·"}
_COLOR_BY_SEV = {SEV_CRITICAL: "red", SEV_IMPORTANT: "yellow",
                 SEV_INFO: "cyan", SEV_VERBOSE: "bright_black"}


class EventBreadcrumbRegistry:
    """event_type → descriptor, with a heuristic fallback for unknown types."""

    def __init__(self) -> None:
        self._by_type: Dict[str, BreadcrumbDescriptor] = {}
        # Event types the TUI already handles with a bespoke, richer listener —
        # the router skips these so there is no double-print.
        self._bespoke: set = set()

    def register(self, desc: BreadcrumbDescriptor) -> None:
        self._by_type[desc.event_type] = desc

    def mark_bespoke(self, *event_types: str) -> None:
        self._bespoke.update(event_types)

    def is_bespoke(self, event_type: str) -> bool:
        return event_type in self._bespoke

    def describe(self, event_type: str) -> BreadcrumbDescriptor:
        """Registered descriptor, else a synthesized one (name-based severity +
        generic formatter) so EVERY event surfaces. Never raises."""
        d = self._by_type.get(event_type)
        if d is not None:
            return d
        sev = _heuristic_severity(event_type)
        return BreadcrumbDescriptor(
            event_type=event_type, glyph=_GLYPH_BY_SEV[sev],
            color=_COLOR_BY_SEV[sev], severity=sev,
            formatter=_generic_formatter, category=_heuristic_category(event_type),
        )

    def render(self, event_type: str, payload: dict) -> Tuple[int, str]:
        """Return ``(severity, plain_text)`` for an event. Never raises."""
        desc = self.describe(event_type)
        label = _humanize(event_type)
        try:
            body = desc.formatter(payload) if desc.formatter else ""
        except Exception:  # noqa: BLE001
            body = ""
        text = f"{label}" + (f" — {body}" if body else "")
        return desc.severity, text


# ---------------------------------------------------------------------------
# Seed descriptors for high-value events (tailored formatting)
# ---------------------------------------------------------------------------


def _f_circuit(p: dict) -> str:
    return f"{p.get('provider', '?')} → {p.get('to_state', p.get('state', '?'))} " \
           f"({p.get('terminal_reason_code', p.get('reason', ''))})".strip()


def _f_governor(p: dict) -> str:
    return f"cap {p.get('applied_cap', p.get('cap', '?'))} " \
           f"(posture={p.get('posture', '?')}, reason={p.get('reason', '')})".strip()


def _f_memory(p: dict) -> str:
    return f"{p.get('level', p.get('to_level', '?'))} " \
           f"(fanout≤{p.get('fanout_cap', '?')})"


def _f_posture(p: dict) -> str:
    return f"{p.get('from', p.get('previous', '?'))} → {p.get('to', p.get('posture', '?'))}"


def _f_costband(p: dict) -> str:
    return f"crossed {p.get('band', '?')} (${p.get('cost_usd', p.get('spend', '?'))})"


def _f_confidence(p: dict) -> str:
    return f"model {p.get('model', '?')} conf={p.get('confidence', '?')}"


def _f_awe(p: dict) -> str:
    rid = str(p.get("run_id", "?"))
    prov = str(p.get("provider", "doubleword"))
    reason = str(p.get("reason", "") or "")
    tail = f" — {reason}" if reason else ""
    return f"{prov} recovery → soak {rid}{tail}".strip()


def build_default_registry() -> EventBreadcrumbRegistry:
    reg = EventBreadcrumbRegistry()
    # These two already have tailored, prompt-aware bespoke listeners in the TUI.
    reg.mark_bespoke("shadow_action_trapped", "provider_state_changed")

    seed = [
        BreadcrumbDescriptor("circuit_breaker_tripped", "✖", "red bold", SEV_CRITICAL, _f_circuit, "provider"),
        BreadcrumbDescriptor("circuit_breaker_state_change", "▲", "yellow", SEV_IMPORTANT, _f_circuit, "provider"),
        BreadcrumbDescriptor("provider_failure_classified", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"{p.get('provider','?')}: {p.get('failure_class', p.get('class',''))}", "provider"),
        BreadcrumbDescriptor("golden_image_degraded", "✖", "red", SEV_CRITICAL,
                             lambda p: f"failover cold-boot ({p.get('reason','')})", "provider"),
        BreadcrumbDescriptor("governor_throttle_applied", "▲", "yellow", SEV_IMPORTANT, _f_governor, "governance"),
        BreadcrumbDescriptor("governor_emergency_brake", "✖", "red bold", SEV_CRITICAL, _f_governor, "governance"),
        BreadcrumbDescriptor("memory_pressure_changed", "▲", "yellow", SEV_IMPORTANT, _f_memory, "memory"),
        BreadcrumbDescriptor("posture_changed", "◆", "cyan", SEV_INFO, _f_posture, "posture"),
        BreadcrumbDescriptor("drift_detected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"target {p.get('target', p.get('file',''))} drifted", "integrity"),
        BreadcrumbDescriptor("git_index_anomaly", "✖", "red", SEV_CRITICAL,
                             lambda p: f"{p.get('detail', p.get('reason',''))}", "integrity"),
        BreadcrumbDescriptor("cost_band_crossed", "▲", "yellow", SEV_IMPORTANT, _f_costband, "cost"),
        BreadcrumbDescriptor("budget_action_taken", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"{p.get('action','?')} (${p.get('spend', p.get('cost_usd',''))})", "cost"),
        BreadcrumbDescriptor("session_exhausted", "✖", "red bold", SEV_CRITICAL,
                             lambda p: f"{p.get('reason','all providers exhausted')}", "session"),
        BreadcrumbDescriptor("soak_circuit_tripped", "✖", "red", SEV_CRITICAL,
                             lambda p: f"{p.get('reason','')}", "soak"),
        BreadcrumbDescriptor("soak_budget_warning", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"${p.get('spend','?')}/{p.get('cap','?')}", "soak"),
        BreadcrumbDescriptor("model_confidence_drop", "▲", "yellow", SEV_IMPORTANT, _f_confidence, "model"),
        BreadcrumbDescriptor("model_sustained_low_confidence", "✖", "red", SEV_CRITICAL, _f_confidence, "model"),
        BreadcrumbDescriptor("guidance_absorbed", "◆", "cyan", SEV_INFO,
                             lambda p: f"steering folded into op {p.get('op_id','')[:8]}", "governance"),
        BreadcrumbDescriptor("flag_typo_detected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"{p.get('typo','?')} → {p.get('suggestion','?')}", "flags"),
        BreadcrumbDescriptor("plan_rejected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"op {p.get('op_id','')[:8]} plan rejected", "plan"),
        BreadcrumbDescriptor("cancellation_overrun_detected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"cancel guard overran ({p.get('provider','?')})", "provider"),
        BreadcrumbDescriptor("trajectory_drift_detected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"{p.get('detail','trajectory drift')}", "integrity"),
        BreadcrumbDescriptor("decision_drift_detected", "▲", "yellow", SEV_IMPORTANT,
                             lambda p: f"{p.get('detail','decision drift')}", "integrity"),
        BreadcrumbDescriptor("awe_soak_launched", "⚡", "green bold", SEV_CRITICAL, _f_awe, "soak"),
        BreadcrumbDescriptor("awe_soak_suppressed", "·", "bright_black", SEV_INFO,
                             lambda p: f"flap guard held ({p.get('reason','lock held')})", "soak"),
        BreadcrumbDescriptor("awe_soak_complete", "✓", "green", SEV_IMPORTANT, _f_awe, "soak"),
        BreadcrumbDescriptor("awe_soak_failed", "✖", "red bold", SEV_CRITICAL, _f_awe, "soak"),
        BreadcrumbDescriptor("supervisor_armed", "◆", "cyan bold", SEV_IMPORTANT,
                             lambda p: f"armed — {p.get('pending','?')} pending, DW {p.get('provider_state','?')} "
                                       f"→ Sentinel pid {p.get('pid','?')}", "supervisor"),
        BreadcrumbDescriptor("supervisor_disarmed", "◇", "cyan", SEV_IMPORTANT,
                             lambda p: f"disarmed — queue clear ({p.get('reason','soak complete')})", "supervisor"),
        BreadcrumbDescriptor("sentinel_restarted", "↻", "yellow", SEV_IMPORTANT,
                             lambda p: f"Sentinel self-healed (attempt {p.get('attempt','?')}, "
                                       f"backoff {p.get('backoff_s','?')}s): {p.get('reason','')}", "supervisor"),
        BreadcrumbDescriptor("sentinel_telemetry", "·", "bright_black", SEV_VERBOSE,
                             lambda p: str(p.get("line", "")).strip(), "supervisor"),
    ]
    for d in seed:
        reg.register(d)
    return reg


# ---------------------------------------------------------------------------
# Operator verbosity (shared by the /breadcrumbs verb + the TUI router)
# ---------------------------------------------------------------------------

_active_level: Optional[int] = None


def get_min_severity() -> int:
    """Active breadcrumb verbosity floor (env-seeded, verb-overridable)."""
    global _active_level
    if _active_level is not None:
        return _active_level
    raw = (os.environ.get(_LEVEL_ENV, "") or "").strip().lower()
    if raw in _NAME_TO_SEV:
        return _NAME_TO_SEV[raw]
    return _DEFAULT_LEVEL


def set_min_severity(level) -> int:
    """Set the floor from a name ('off'/'critical'/'important'/'info'/'all') or
    an int rank. Returns the resolved rank. Never raises."""
    global _active_level
    if isinstance(level, str):
        _active_level = _NAME_TO_SEV.get(level.strip().lower(), _DEFAULT_LEVEL)
    else:
        try:
            _active_level = int(level)
        except (TypeError, ValueError):
            _active_level = _DEFAULT_LEVEL
    return _active_level


def severity_name(rank: int) -> str:
    return _SEV_NAMES.get(rank, "off" if rank >= 99 else "?")


# ---------------------------------------------------------------------------
# Coalescer — de-flood repeated events (edge case: storms of one type)
# ---------------------------------------------------------------------------


class BreadcrumbCoalescer:
    """Suppresses a repeated ``(event_type, key)`` within ``window_s`` so a burst
    (e.g. rapid circuit_breaker_approaching) prints once, not 50×. Never raises."""

    def __init__(self, window_s: float = 6.0) -> None:
        self._window_s = window_s
        self._last: Dict[Tuple[str, str], float] = {}

    def should_show(self, event_type: str, key: str = "", *, now: Optional[float] = None) -> bool:
        ref = time.time() if now is None else now
        k = (event_type, key)
        last = self._last.get(k)
        if last is not None and (ref - last) < self._window_s:
            return False
        self._last[k] = ref
        # opportunistic prune
        if len(self._last) > 512:
            cutoff = ref - 4.0 * self._window_s
            for kk in [kk for kk, ts in self._last.items() if ts < cutoff]:
                self._last.pop(kk, None)
        return True


__all__ = [
    "BreadcrumbCoalescer",
    "BreadcrumbDescriptor",
    "EventBreadcrumbRegistry",
    "SEV_CRITICAL",
    "SEV_IMPORTANT",
    "SEV_INFO",
    "SEV_VERBOSE",
    "build_default_registry",
    "get_min_severity",
    "set_min_severity",
    "severity_name",
]
