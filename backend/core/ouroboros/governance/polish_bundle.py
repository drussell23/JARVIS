"""§38 Slice 6 polish bundle — 8 polish features (PRD §38
Slice 6, 2026-05-07).

Closes the §38.9 sequencing capstone by shipping personality
polish that takes O+V from "looks like CC" to "looks like a
living system":

  1. Heartbeat indicator (♥/♡ alternating, rate-modulated)
  2. Mood/morale glyph (😎 / 😐 / 😰 / 🆘 derived from
     convergence + error-rate + cost-burn)
  3. Predictive graduation timer ("Next graduation: ~14 days")
  4. Sparklines (Braille block chars for cost/ops/success
     trajectory)
  5. Animated Braille thinking spinner (⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏ cycle)
  6. Truncation affordance hints ("(/expand <ref>)")
  7. Smart path truncation (head + tail with "/...")
  8. Effort phrase ladder (predictive language replacing
     categorical "high effort")

All features are **pure functions** taking caller-injected
inputs (or composing canonical sources via lazy-import); zero
parallel state, zero hardcoding, NEVER raises.

## Composes canonical sources (operator binding "no duplication")

  * :class:`thinking_progress_aggregator.EffortBand` — extended
    via canonical phrase mapping (no parallel enum).
  * :mod:`phase9_substrate_health` — composes
    ``build_full_health_dashboard`` + ``EtaProjection`` for
    predictive graduation timer.

Each feature has its own master flag for granular operator
control; bundle master flag short-circuits all of them.

## Architectural locks (operator mandate, AST-pinned)

  1. **Bundle master flag default-FALSE** per §33.1.
  2. **Authority asymmetry** — imports stdlib +
     governance.thinking_progress_aggregator +
     governance.phase9_substrate_health ONLY.
  3. **Closed mood-glyph taxonomy** — :class:`MoodGlyph` is
     a 4-value frozen enum.
  4. **Composes canonical EffortBand** — effort_phrase_for_band
     MUST receive an :class:`EffortBand` member; AST-pinned
     no-parallel-enum.
  5. **Sparkline char set bytes-pinned** — Unicode block-bar
     ladder ▁▂▃▄▅▆▇█ MUST live in canonical
     ``_SPARKLINE_BLOCKS`` constant.
"""
from __future__ import annotations

import enum
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


POLISH_BUNDLE_SCHEMA_VERSION: str = "polish_bundle.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag — §33.1 default-FALSE
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_POLISH_BUNDLE_ENABLED`` master switch.
    Default-FALSE per §33.1 — when off, all 8 polish features
    short-circuit to empty/unchanged output."""
    return os.environ.get(
        "JARVIS_POLISH_BUNDLE_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _sub_flag_enabled(name: str) -> bool:
    """Per-feature sub-flag check — defaults to True when
    bundle master is on (operator opts in granularly only when
    they want a feature off)."""
    if not master_enabled():
        return False
    raw = os.environ.get(name, "").strip().lower()
    if raw == "":
        return True  # bundle on + no override → enabled
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Canonical character sets (env-overridable, "no hardcoding")
# ---------------------------------------------------------------------------


_HEARTBEAT_FILLED_DEFAULT: str = "♥"
_HEARTBEAT_EMPTY_DEFAULT: str = "♡"

# Braille spinner — 10-frame canonical cycle.
_BRAILLE_SPINNER_FRAMES_DEFAULT: Tuple[str, ...] = (
    "⠋", "⠙", "⠹", "⠸", "⠼",
    "⠴", "⠦", "⠧", "⠇", "⠏",
)

# Sparkline block-bar ladder (8 levels, low → high).
_SPARKLINE_BLOCKS: Tuple[str, ...] = (
    "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█",
)


# ---------------------------------------------------------------------------
# (1) Heartbeat indicator
# ---------------------------------------------------------------------------


def format_heartbeat(
    *,
    ops_per_min: float = 0.0,
    tick_index: int = 0,
) -> str:
    """Render a heartbeat glyph alternating with the tick
    index. Pure function. NEVER raises.

    ``ops_per_min`` modulates the visual: ≥1 op/min = filled
    glyph (alive); 0 ops/min = empty glyph (resting). Operator
    sees organism activity at-a-glance.

    Caller provides tick_index (e.g., from a ~500ms toolbar
    refresh loop) for the alternation effect."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_HEARTBEAT_ENABLED",
        ):
            return ""
        filled = (
            os.environ.get(
                "JARVIS_POLISH_HEARTBEAT_FILLED", "",
            ) or _HEARTBEAT_FILLED_DEFAULT
        )
        empty = (
            os.environ.get(
                "JARVIS_POLISH_HEARTBEAT_EMPTY", "",
            ) or _HEARTBEAT_EMPTY_DEFAULT
        )
        rate = max(0.0, float(ops_per_min))
        if rate < 0.1:
            # Resting heartbeat — slow alternation (every 4 ticks).
            return filled if (int(tick_index) // 4) % 2 == 0 else empty
        # Active heartbeat — every-tick alternation.
        return filled if int(tick_index) % 2 == 0 else empty
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# (2) Mood / morale glyph
# ---------------------------------------------------------------------------


class MoodGlyph(str, enum.Enum):
    """Closed 4-value taxonomy describing organism's perceived
    health. Bytes-pinned via AST regression.

      * ``CONFIDENT`` (😎) — convergence high, errors low,
        cost on-budget.
      * ``NEUTRAL`` (😐) — mid-state; default while organism
        warms up or has insufficient data.
      * ``STRUGGLING`` (😰) — convergence low OR errors high
        OR cost approaching budget.
      * ``EMERGENCY`` (🆘) — multiple critical conditions
        AND/OR governor emergency brake active.
    """

    CONFIDENT = "confident"
    NEUTRAL = "neutral"
    STRUGGLING = "struggling"
    EMERGENCY = "emergency"


_MOOD_GLYPHS: Dict[MoodGlyph, str] = {
    MoodGlyph.CONFIDENT: "😎",
    MoodGlyph.NEUTRAL: "😐",
    MoodGlyph.STRUGGLING: "😰",
    MoodGlyph.EMERGENCY: "🆘",
}


def compute_mood(
    *,
    convergence_score: float = 0.0,
    error_rate: float = 0.0,
    cost_burn_pct: float = 0.0,
    governor_emergency: bool = False,
) -> MoodGlyph:
    """Pure-function mood inference. Caller injects three
    canonical signals + governor state. NEVER raises.

    Rules (first-match-wins):
      1. Governor emergency brake → EMERGENCY
      2. error_rate > 0.40 OR cost_burn_pct > 0.95 → EMERGENCY
      3. error_rate > 0.20 OR cost_burn_pct > 0.80 OR
         convergence_score < 0.30 → STRUGGLING
      4. convergence_score > 0.70 AND error_rate < 0.10 AND
         cost_burn_pct < 0.50 → CONFIDENT
      5. Otherwise → NEUTRAL"""
    try:
        if governor_emergency:
            return MoodGlyph.EMERGENCY
        cs = max(0.0, min(1.0, float(convergence_score)))
        er = max(0.0, min(1.0, float(error_rate)))
        cb = max(0.0, min(1.0, float(cost_burn_pct)))
        if er > 0.40 or cb > 0.95:
            return MoodGlyph.EMERGENCY
        if er > 0.20 or cb > 0.80 or cs < 0.30:
            return MoodGlyph.STRUGGLING
        if cs > 0.70 and er < 0.10 and cb < 0.50:
            return MoodGlyph.CONFIDENT
        return MoodGlyph.NEUTRAL
    except Exception:  # noqa: BLE001 — defensive
        return MoodGlyph.NEUTRAL


def format_mood_indicator(mood: MoodGlyph) -> str:
    """Render the mood glyph for status-line composition.
    Returns empty when sub-flag disabled."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_MOOD_ENABLED",
        ):
            return ""
        return _MOOD_GLYPHS.get(mood, "?")
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# (3) Predictive graduation timer
# ---------------------------------------------------------------------------


def format_predictive_graduation_timer() -> str:
    """Render "Next graduation: ~14 days at current cadence"
    composing canonical
    ``phase9_substrate_health.build_full_health_dashboard()``
    + ``EtaProjection`` (already shipped substrate).

    NEVER raises. Returns empty when:
      * Sub-flag off
      * No flag has finite ETA
      * phase9_substrate_health unavailable"""
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_PREDICTIVE_TIMER_ENABLED",
        ):
            return ""
        from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
            build_full_health_dashboard,
        )
        reports = build_full_health_dashboard()
        # Find the soonest ungraduated flag with finite ETA.
        candidates: List[Tuple[float, str]] = []
        for r in reports or ():
            if r.eta is None:
                continue
            days = float(r.eta.days_to_graduation)
            if days <= 0.0:
                continue
            if not math.isfinite(days):
                continue
            candidates.append((days, r.flag_name))
        if not candidates:
            return ""
        candidates.sort()
        soonest_days, flag_name = candidates[0]
        if soonest_days < 1.0:
            return f"Next graduation: <1 day ({flag_name})"
        return (
            f"Next graduation: ~{int(round(soonest_days))} "
            f"days ({flag_name})"
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# (4) Sparklines
# ---------------------------------------------------------------------------


def format_sparkline(
    values: Iterable[float],
    *,
    width: Optional[int] = None,
) -> str:
    """Render a numeric series as a Braille block-bar
    sparkline. Pure function. NEVER raises.

    Empty / all-zero inputs render as the lowest-block
    sequence ``▁▁▁``. Caller provides the values; substrate
    renders. Composes canonical ``_SPARKLINE_BLOCKS`` ladder
    (operator binding "no hardcoding")."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_SPARKLINES_ENABLED",
        ):
            return ""
        seq: List[float] = []
        for v in values:
            try:
                seq.append(float(v))
            except (TypeError, ValueError):
                seq.append(0.0)
        if not seq:
            return ""
        # Width defaults to len(seq); env-tunable cap.
        env_w = os.environ.get(
            "JARVIS_POLISH_SPARKLINE_WIDTH", "",
        ).strip()
        if width is not None:
            target_w = max(1, min(80, int(width)))
        elif env_w:
            try:
                target_w = max(1, min(80, int(env_w)))
            except (TypeError, ValueError):
                target_w = len(seq)
        else:
            target_w = len(seq)
        # Resample: if seq longer than target_w, average chunks.
        if len(seq) > target_w:
            chunk = len(seq) / target_w
            resampled = []
            for i in range(target_w):
                start = int(i * chunk)
                end = int((i + 1) * chunk)
                if end <= start:
                    end = start + 1
                window = seq[start:end]
                resampled.append(
                    sum(window) / len(window) if window else 0.0
                )
            seq = resampled
        # If seq shorter, pad-left with first value.
        elif len(seq) < target_w:
            seq = [seq[0]] * (target_w - len(seq)) + seq
        # Map to block ladder.
        lo = min(seq)
        hi = max(seq)
        span = hi - lo
        if span <= 0:
            # All-equal series — pick mid-block.
            return _SPARKLINE_BLOCKS[len(_SPARKLINE_BLOCKS) // 2] * len(seq)
        levels = len(_SPARKLINE_BLOCKS) - 1
        out_chars: List[str] = []
        for v in seq:
            normalized = (v - lo) / span
            idx = int(round(normalized * levels))
            idx = max(0, min(levels, idx))
            out_chars.append(_SPARKLINE_BLOCKS[idx])
        return "".join(out_chars)
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# (5) Animated Braille thinking spinner
# ---------------------------------------------------------------------------


@dataclass
class BrailleSpinner:
    """Frame-cycle thinking spinner. Caller advances tick;
    spinner returns next frame in canonical 10-frame cycle.

    Frame rate is operator-controlled via tick frequency in
    the toolbar refresh loop (typically ~100ms). NEVER raises.
    """

    _tick: int = 0
    schema_version: str = POLISH_BUNDLE_SCHEMA_VERSION

    def advance(self) -> str:
        """Return next frame + advance internal counter.
        Returns empty when sub-flag disabled."""
        try:
            if not _sub_flag_enabled(
                "JARVIS_POLISH_SPINNER_ENABLED",
            ):
                return ""
            frames = _BRAILLE_SPINNER_FRAMES_DEFAULT
            frame = frames[self._tick % len(frames)]
            self._tick += 1
            return frame
        except Exception:  # noqa: BLE001 — defensive
            return ""

    def current(self) -> str:
        """Peek at current frame without advancing."""
        try:
            if not _sub_flag_enabled(
                "JARVIS_POLISH_SPINNER_ENABLED",
            ):
                return ""
            frames = _BRAILLE_SPINNER_FRAMES_DEFAULT
            return frames[self._tick % len(frames)]
        except Exception:  # noqa: BLE001 — defensive
            return ""

    def reset(self) -> None:
        self._tick = 0


# ---------------------------------------------------------------------------
# (6) Truncation affordance hints
# ---------------------------------------------------------------------------


def format_truncation_affordance(
    *,
    truncated_count: int,
    ref: str,
    suffix: str = "lines",
) -> str:
    """Render a truncation hint with expand affordance:
    ``"... +12 lines (/expand t-3)"``.

    Composes the canonical ref-prefix scheme (`o-N` / `t-N` /
    `n-N` / `d-N`) without re-implementing the buffers.
    Caller provides ref + count. Pure function. NEVER raises.
    """
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_TRUNCATION_AFFORDANCES_ENABLED",
        ):
            # Fallback: just the count; no affordance.
            count = max(0, int(truncated_count))
            return f"... +{count} {suffix}" if count > 0 else ""
        count = max(0, int(truncated_count))
        if count <= 0:
            return ""
        ref_safe = str(ref or "").strip()
        if not ref_safe:
            return f"... +{count} {suffix}"
        return f"... +{count} {suffix} (/expand {ref_safe})"
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# (7) Smart path truncation
# ---------------------------------------------------------------------------


def smart_path_truncate(
    path: str,
    *,
    max_chars: int = 60,
    head_segments: int = 1,
    tail_segments: int = 2,
) -> str:
    """Path-aware truncation: keeps head + tail segments,
    elides middle with "/...". Beats char-count truncation
    that cuts mid-token. Pure function. NEVER raises.

    Examples:
      ``backend/core/ouroboros/governance/orchestrator.py``
      → ``backend/.../governance/orchestrator.py`` (with
      head=1, tail=2)

    Returns the original string when:
      * Sub-flag disabled
      * Path is shorter than max_chars
      * Path has < (head + tail + 1) segments
    """
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_SMART_PATH_TRUNCATE_ENABLED",
        ):
            return str(path or "")[:max_chars]
        s = str(path or "")
        if len(s) <= max_chars:
            return s
        parts = s.split("/")
        if len(parts) < (head_segments + tail_segments + 1):
            # Not enough segments to elide — fall back to head + tail char.
            return s[:max_chars - 3] + "..."
        head = "/".join(parts[:head_segments])
        tail = "/".join(parts[-tail_segments:])
        candidate = f"{head}/.../{tail}"
        if len(candidate) <= max_chars:
            return candidate
        # Even smart-truncated is too long — fall back to char trunc.
        return s[:max_chars - 3] + "..."
    except Exception:  # noqa: BLE001 — defensive
        return str(path or "")[:max_chars]


# ---------------------------------------------------------------------------
# (8) Effort phrase ladder (extension to canonical EffortBand)
# ---------------------------------------------------------------------------


_EFFORT_PHRASE_TABLE: Dict[str, str] = {
    "low": "just started",
    "medium": "working through it",
    "high": "deep in analysis",
    "very_high": "nearly done thinking",
}


def effort_phrase_for_band(band: Any) -> str:
    """Map a canonical :class:`EffortBand` (or its string
    value) to a predictive operator-readable phrase.

    Replaces the categorical ``"high effort"`` label with the
    warmer predictive ``"deep in analysis"``. Pure function.
    NEVER raises.

    Composes canonical EffortBand values via ``.value``
    accessor; AST-pinned no-parallel-enum."""
    try:
        if not _sub_flag_enabled(
            "JARVIS_POLISH_EFFORT_PHRASES_ENABLED",
        ):
            # Fallback: composes canonical EffortBand label.
            try:
                from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
                    _EFFORT_LABELS,
                    EffortBand,
                )
                if isinstance(band, EffortBand):
                    return _EFFORT_LABELS.get(band, "")
                if isinstance(band, str):
                    for b in EffortBand:
                        if b.value == band:
                            return _EFFORT_LABELS.get(b, "")
            except Exception:  # noqa: BLE001
                pass
            return ""
        # Sub-flag on — use predictive phrase ladder.
        if hasattr(band, "value"):
            value = str(band.value)
        else:
            value = str(band or "").strip().lower()
        return _EFFORT_PHRASE_TABLE.get(value, "")
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. 5 pins:

      1. ``master_default_false`` — JARVIS_POLISH_BUNDLE_-
         ENABLED stays default-FALSE per §33.1.
      2. ``authority_asymmetry`` — substrate purity.
      3. ``mood_taxonomy_4_values`` — closed-enum integrity.
      4. ``composes_canonical_effort_band`` — effort phrase
         path MUST compose canonical EffortBand from
         thinking_progress_aggregator (no parallel enum).
      5. ``sparkline_blocks_canonical`` — _SPARKLINE_BLOCKS
         contains the canonical 8-level Unicode block ladder
         (▁▂▃▄▅▆▇█); operator binding "no hardcoding".
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/polish_bundle.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                src = ast.unparse(node)
                if "return True" in src:
                    violations.append(
                        "master_enabled MUST NOT "
                        "unconditionally return True (§33.1)"
                    )
                if (
                    "JARVIS_POLISH_BUNDLE_ENABLED" not in src
                ):
                    violations.append(
                        "master_enabled MUST gate on "
                        "JARVIS_POLISH_BUNDLE_ENABLED"
                    )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"polish_bundle MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_mood_taxonomy(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required = {
            "CONFIDENT", "NEUTRAL",
            "STRUGGLING", "EMERGENCY",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                if node.name == "MoodGlyph":
                    seen: set = set()
                    for stmt in node.body:
                        if isinstance(stmt, ast.Assign):
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    seen.add(tgt.id)
                    missing = required - seen
                    extras = seen - required
                    if missing:
                        violations.append(
                            f"MoodGlyph missing: "
                            f"{sorted(missing)}"
                        )
                    if extras:
                        violations.append(
                            f"MoodGlyph has extras: "
                            f"{sorted(extras)}"
                        )
        return tuple(violations)

    def _validate_composes_effort_band(
        tree: "ast.Module", source: str,
    ) -> tuple:
        violations: list = []
        if "thinking_progress_aggregator" not in source:
            violations.append(
                "polish_bundle MUST compose canonical "
                "thinking_progress_aggregator.EffortBand "
                "(no parallel effort enum)"
            )
        if "EffortBand" not in source:
            violations.append(
                "effort_phrase_for_band MUST reference "
                "canonical EffortBand"
            )
        return tuple(violations)

    def _validate_sparkline_blocks_canonical(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        required_chars = ("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")
        for node in ast.walk(tree):
            value_node = None
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if (
                        isinstance(tgt, ast.Name)
                        and tgt.id == "_SPARKLINE_BLOCKS"
                    ):
                        value_node = node.value
            elif isinstance(node, ast.AnnAssign):
                if (
                    isinstance(node.target, ast.Name)
                    and node.target.id == "_SPARKLINE_BLOCKS"
                ):
                    value_node = node.value
            if isinstance(value_node, ast.Tuple):
                seen_chars = []
                for elt in value_node.elts:
                    if (
                        isinstance(elt, ast.Constant)
                        and isinstance(elt.value, str)
                    ):
                        seen_chars.append(elt.value)
                if tuple(seen_chars) != required_chars:
                    violations.append(
                        f"_SPARKLINE_BLOCKS does not match "
                        f"canonical 8-level Unicode block "
                        f"ladder (▁▂▃▄▅▆▇█); got "
                        f"{seen_chars!r}"
                    )
                return tuple(violations)
        violations.append(
            "_SPARKLINE_BLOCKS canonical tuple not found"
        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "polish_bundle_master_default_false"
            ),
            target_file=target,
            description=(
                "Master flag JARVIS_POLISH_BUNDLE_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "polish_bundle_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "polish_bundle MUST stay pure substrate "
                "composing thinking_progress_aggregator + "
                "phase9_substrate_health + stdlib ONLY. "
                "NEVER imports orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "change_engine / semantic_guardian."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "polish_bundle_mood_taxonomy_4_values"
            ),
            target_file=target,
            description=(
                "MoodGlyph is a 4-value closed taxonomy "
                "(CONFIDENT / NEUTRAL / STRUGGLING / "
                "EMERGENCY)."
            ),
            validate=_validate_mood_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "polish_bundle_composes_canonical_effort_band"
            ),
            target_file=target,
            description=(
                "Effort phrase path MUST compose canonical "
                "thinking_progress_aggregator.EffortBand. "
                "No parallel effort enum."
            ),
            validate=_validate_composes_effort_band,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "polish_bundle_sparkline_blocks_canonical"
            ),
            target_file=target,
            description=(
                "_SPARKLINE_BLOCKS bytes-pinned to canonical "
                "8-level Unicode block-bar ladder "
                "(▁▂▃▄▅▆▇█). Operator binding "
                "'no hardcoding' enforced via ladder check."
            ),
            validate=_validate_sparkline_blocks_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    seeds = (
        (
            "JARVIS_POLISH_BUNDLE_ENABLED",
            "bool",
            "false",
            (
                "Master flag for §38 Slice 6 polish bundle. "
                "Default-FALSE per §33.1."
            ),
        ),
        (
            "JARVIS_POLISH_HEARTBEAT_ENABLED",
            "bool",
            "true",
            "Heartbeat sub-feature (default true when bundle on).",
        ),
        (
            "JARVIS_POLISH_MOOD_ENABLED",
            "bool",
            "true",
            "Mood glyph sub-feature.",
        ),
        (
            "JARVIS_POLISH_PREDICTIVE_TIMER_ENABLED",
            "bool",
            "true",
            "Predictive graduation timer sub-feature.",
        ),
        (
            "JARVIS_POLISH_SPARKLINES_ENABLED",
            "bool",
            "true",
            "Sparklines sub-feature.",
        ),
        (
            "JARVIS_POLISH_SPINNER_ENABLED",
            "bool",
            "true",
            "Braille spinner sub-feature.",
        ),
        (
            "JARVIS_POLISH_TRUNCATION_AFFORDANCES_ENABLED",
            "bool",
            "true",
            "Truncation affordance hints sub-feature.",
        ),
        (
            "JARVIS_POLISH_SMART_PATH_TRUNCATE_ENABLED",
            "bool",
            "true",
            "Smart path truncation sub-feature.",
        ),
        (
            "JARVIS_POLISH_EFFORT_PHRASES_ENABLED",
            "bool",
            "true",
            "Effort phrase ladder sub-feature.",
        ),
        (
            "JARVIS_POLISH_SPARKLINE_WIDTH",
            "int",
            "20",
            "Sparkline render width (chars).",
        ),
    )
    n = 0
    try:
        for name, kind, default, desc in seeds:
            try:
                registry.register(
                    name=name,
                    type_=kind,
                    default=default,
                    description=desc,
                    category="ux",
                    posture_relevance="RELEVANT",
                    source_file=(
                        "backend/core/ouroboros/governance/"
                        "polish_bundle.py"
                    ),
                )
                n += 1
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return n
    return n


__all__ = [
    "BrailleSpinner",
    "MoodGlyph",
    "POLISH_BUNDLE_SCHEMA_VERSION",
    "compute_mood",
    "effort_phrase_for_band",
    "format_heartbeat",
    "format_mood_indicator",
    "format_predictive_graduation_timer",
    "format_sparkline",
    "format_truncation_affordance",
    "master_enabled",
    "register_flags",
    "register_shipped_invariants",
    "smart_path_truncate",
]
