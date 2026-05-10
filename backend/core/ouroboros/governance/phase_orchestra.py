"""§39 Tier-7 #20 — Phase orchestra synchronization
(PRD v2.75 to v2.76, 2026-05-09).

Maps each canonical 11-phase forward-flow phase to a
deterministic musical cue (note + intensity) and emits
audio cue events on phase transitions. Downstream
consumers (TUI / IDE / Karen voice) play the actual
audio; this substrate is the cue-event producer.

Authority asymmetry: ZERO. Producer-bridge + renderer.
NEVER calls orchestrator, NEVER changes phase, NEVER
plays audio directly (audio playback is a downstream
concern; this substrate only emits cue records).

§38.11.5a.5 single-canonical-name discipline: composes
canonical 11-phase tuple from
:func:`pipeline_progress.forward_flow_phases` + canonical
:func:`pipeline_progress.phase_index`. ZERO parallel
phase ordering. Two NEW closed taxonomies:
:class:`OrchestraNote` (8 solfège notes — one octave) +
:class:`CueIntensity` (4 dynamics levels).

§33 patterns:
- §33.1 graduation contract (master default-FALSE)
- §33.2 producer-bridge (``emit_cue(phase)`` for
  orchestrator integration; lazy-importable)
- §33.5 versioned artifact (frozen :class:`OrchestraCue`)
"""
from __future__ import annotations

import enum
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


PHASE_ORCHESTRA_SCHEMA_VERSION: str = "phase_orchestra.1"


_ENV_MASTER = "JARVIS_PHASE_ORCHESTRA_ENABLED"
_ENV_BELL_ON_CUE = "JARVIS_PHASE_ORCHESTRA_BELL_ENABLED"
_ENV_RING_SIZE = "JARVIS_PHASE_ORCHESTRA_RING_SIZE"

_DEFAULT_RING_SIZE = 64
_MIN_RING = 8
_MAX_RING = 512


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def bell_on_cue_enabled() -> bool:
    """When True + master on, render emits ASCII bell
    char (\\a) per cue. Default FALSE — opt-in audio."""
    if not master_enabled():
        return False
    return _flag(_ENV_BELL_ON_CUE, default=False)


def _read_ring_size() -> int:
    raw = os.environ.get(_ENV_RING_SIZE, "").strip()
    if not raw:
        return _DEFAULT_RING_SIZE
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RING_SIZE
    return max(_MIN_RING, min(_MAX_RING, n))


# ===========================================================================
# Closed taxonomies
# ===========================================================================


class OrchestraNote(str, enum.Enum):
    """Closed 8-value solfège vocabulary — one octave.

    Bytes-pinned ascending pitches. The 11 canonical
    forward-flow phases map to these 8 notes via modular
    arithmetic on phase_index (no hardcoded per-phase
    note assignment — operator binding "no hardcoding").
    """

    DO = "do"
    RE = "re"
    MI = "mi"
    FA = "fa"
    SOL = "sol"
    LA = "la"
    TI = "ti"
    DO2 = "do2"


# Bytes-pinned ordered tuple — index → note. AST regression
# locks both order + canonical solfège names.
_NOTE_ORDER: Tuple[OrchestraNote, ...] = (
    OrchestraNote.DO,
    OrchestraNote.RE,
    OrchestraNote.MI,
    OrchestraNote.FA,
    OrchestraNote.SOL,
    OrchestraNote.LA,
    OrchestraNote.TI,
    OrchestraNote.DO2,
)


class CueIntensity(str, enum.Enum):
    """Closed 4-value dynamics vocabulary mapped to phase
    progression. Phases near the center of the forward-
    flow are louder (the "work" phases); start + end
    phases are softer.
    """

    WHISPER = "whisper"   # phase_index 0..1 (CLASSIFY/ROUTE)
    SOFT = "soft"         # phase_index 2..3
    NORMAL = "normal"     # phase_index 4..6 (the work zone)
    FORTE = "forte"       # phase_index 7+ (terminal phases)


def _intensity_for_index(idx: int) -> CueIntensity:
    """Pure-function bucketing. NEVER raises."""
    try:
        i = int(idx)
    except (TypeError, ValueError):
        return CueIntensity.WHISPER
    if i < 0:
        return CueIntensity.WHISPER
    if i <= 1:
        return CueIntensity.WHISPER
    if i <= 3:
        return CueIntensity.SOFT
    if i <= 6:
        return CueIntensity.NORMAL
    return CueIntensity.FORTE


def _note_for_index(idx: int) -> OrchestraNote:
    """Map phase_index → solfège note via modular
    arithmetic. NEVER raises; out-of-range → DO."""
    try:
        i = int(idx)
        if i < 0:
            return OrchestraNote.DO
        return _NOTE_ORDER[i % len(_NOTE_ORDER)]
    except (TypeError, ValueError):
        return OrchestraNote.DO


# ===========================================================================
# Frozen §33.5 versioned artifact
# ===========================================================================


@dataclass(frozen=True)
class OrchestraCue:
    """One audio cue event."""

    phase_name: str
    phase_index: int
    note: OrchestraNote
    intensity: CueIntensity
    op_id: str = ""
    emitted_at_unix: float = field(default_factory=time.time)
    schema_version: str = PHASE_ORCHESTRA_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "phase_name": self.phase_name,
            "phase_index": self.phase_index,
            "note": self.note.value,
            "intensity": self.intensity.value,
            "op_id": self.op_id,
            "emitted_at_unix": self.emitted_at_unix,
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Singleton ring
# ===========================================================================


class OrchestraLedger:
    """Bounded ring of recent cues. Thread-safe."""

    def __init__(self) -> None:
        self._cues: Deque[OrchestraCue] = deque(
            maxlen=_read_ring_size(),
        )
        self._lock = threading.RLock()

    def record(self, cue: OrchestraCue) -> None:
        with self._lock:
            self._cues.append(cue)

    def recent(self, *, limit: int = 16) -> Tuple[OrchestraCue, ...]:
        try:
            n = max(1, min(int(limit), _MAX_RING))
        except (TypeError, ValueError):
            n = 16
        with self._lock:
            items = list(self._cues)
        if n >= len(items):
            return tuple(items)
        return tuple(items[-n:])

    def reset_for_tests(self) -> None:
        with self._lock:
            self._cues.clear()


_default_ledger: Optional[OrchestraLedger] = None
_singleton_lock = threading.Lock()


def get_default_ledger() -> OrchestraLedger:
    global _default_ledger
    with _singleton_lock:
        if _default_ledger is None:
            _default_ledger = OrchestraLedger()
        return _default_ledger


def reset_ledger_for_tests() -> None:
    global _default_ledger
    with _singleton_lock:
        if _default_ledger is not None:
            _default_ledger.reset_for_tests()
        _default_ledger = None


# ===========================================================================
# Producer-bridge §33.2 — emit_cue(phase)
# ===========================================================================


def emit_cue(
    *,
    phase: Any,
    op_id: str = "",
) -> Optional[OrchestraCue]:
    """Producer-bridge for orchestrator phase transitions.
    Composes canonical pipeline_progress for phase_index
    + canonical phase tuple. NEVER raises.

    Returns the recorded cue or None on master flag off /
    canonical pipeline_progress unavailable / phase not in
    forward-flow.
    """
    if not master_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.pipeline_progress import (  # noqa: E501
            forward_flow_phases, phase_index,
        )
    except Exception:  # noqa: BLE001
        return None

    try:
        idx = phase_index(phase)
        if idx is None:
            return None
        flow = forward_flow_phases()
        if not flow or idx >= len(flow):
            return None
        phase_obj = flow[idx]
        phase_name = (
            phase_obj.name
            if hasattr(phase_obj, "name")
            else str(phase_obj).upper()
        )
    except Exception:  # noqa: BLE001
        return None

    cue = OrchestraCue(
        phase_name=str(phase_name),
        phase_index=int(idx),
        note=_note_for_index(idx),
        intensity=_intensity_for_index(idx),
        op_id=str(op_id or ""),
    )
    try:
        get_default_ledger().record(cue)
    except Exception:  # noqa: BLE001
        pass
    _publish_event(cue)
    return cue


def _publish_event(cue: OrchestraCue) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_PHASE_ORCHESTRA_CUE,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is not None:
            broker.publish(
                EVENT_TYPE_PHASE_ORCHESTRA_CUE,
                cue.op_id or cue.phase_name,
                cue.to_dict(),
            )
    except Exception:  # noqa: BLE001
        logger.debug(
            "phase_orchestra: SSE failed", exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_INTENSITY_GLYPHS: Dict[CueIntensity, str] = {
    CueIntensity.WHISPER: "♩",
    CueIntensity.SOFT: "♪",
    CueIntensity.NORMAL: "♫",
    CueIntensity.FORTE: "♬",
}


_INTENSITY_TINTS: Dict[CueIntensity, str] = {
    CueIntensity.WHISPER: "dim",
    CueIntensity.SOFT: "cyan",
    CueIntensity.NORMAL: "yellow",
    CueIntensity.FORTE: "bright_yellow",
}


def format_orchestra_recent(
    *,
    limit: int = 12,
    cues: Optional[Tuple[OrchestraCue, ...]] = None,
) -> str:
    """Render recent cues as a flowing musical line."""
    if not master_enabled():
        return ""
    if cues is None:
        cues = get_default_ledger().recent(limit=limit)
    if not cues:
        return (
            "[bright_yellow]🎼 Phase orchestra:[/]\n"
            "  [dim]no recent cues[/]"
        )
    parts = ["[bright_yellow]🎼 Phase orchestra:[/]"]
    bell = "\a" if bell_on_cue_enabled() else ""
    bar = []
    for cue in cues[-limit:]:
        glyph = _INTENSITY_GLYPHS.get(cue.intensity, "♩")
        tint = _INTENSITY_TINTS.get(cue.intensity, "white")
        bar.append(
            f"[{tint}]{glyph}[/] "
            f"{cue.phase_name[:8]}={cue.note.value}"
        )
    parts.append("  " + bell + "  ".join(bar))
    return "\n".join(parts)


def format_orchestra_status() -> str:
    """Render orchestra status (counts by intensity +
    note distribution)."""
    if not master_enabled():
        return ""
    cues = get_default_ledger().recent(limit=_MAX_RING)
    if not cues:
        return ""
    by_intensity: Dict[str, int] = {
        i.value: 0 for i in CueIntensity
    }
    by_note: Dict[str, int] = {
        n.value: 0 for n in OrchestraNote
    }
    for c in cues:
        by_intensity[c.intensity.value] = (
            by_intensity.get(c.intensity.value, 0) + 1
        )
        by_note[c.note.value] = (
            by_note.get(c.note.value, 0) + 1
        )
    parts = ["[bright_yellow]🎼 Phase orchestra status:[/]"]
    parts.append(
        f"  [dim]({len(cues)} recent cues)[/]"
    )
    parts.append("  by intensity:")
    for intensity in CueIntensity:
        n = by_intensity.get(intensity.value, 0)
        if n > 0:
            tint = _INTENSITY_TINTS.get(
                intensity, "white",
            )
            parts.append(
                f"    [{tint}]{intensity.value:<8}[/] : {n}"
            )
    parts.append("  by note:")
    for note in OrchestraNote:
        n = by_note.get(note.value, 0)
        if n > 0:
            parts.append(
                f"    {note.value:<5} : {n}"
            )
    return "\n".join(parts)


# ===========================================================================
# FlagRegistry seeds + AST pins
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-7 #20 phase orchestra master switch "
            "(default FALSE per §33.1).",
            "false",
        ),
        (
            _ENV_BELL_ON_CUE, "bool",
            "Emit ASCII bell (\\a) per cue render. "
            "Default FALSE — opt-in audio.",
            "false",
        ),
        (
            _ENV_RING_SIZE, "int",
            "Bounded ring size for cue ledger "
            "(default 64; clamped 8..512).",
            "64",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name, type=typ, category="ux",
                description=desc, example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "phase_orchestra.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return []
                return ["master must default False"]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier7_20_master_default_false"
        ),
        description="§33.1 graduation contract.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_orchestra.py"
        ),
        validate=_master,
    ))

    def _note_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "OrchestraNote"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "DO", "RE", "MI", "FA",
                    "SOL", "LA", "TI", "DO2",
                }
                missing = expected - names
                if missing:
                    return [
                        f"OrchestraNote missing: "
                        f"{sorted(missing)} (canonical "
                        "8-note solfège octave)"
                    ]
                return []
        return ["OrchestraNote not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier7_20_note_taxonomy_8_values"
        ),
        description=(
            "Closed 8-value OrchestraNote solfège octave."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_orchestra.py"
        ),
        validate=_note_taxonomy,
    ))

    def _intensity_taxonomy(tree, src):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CueIntensity"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "WHISPER", "SOFT", "NORMAL", "FORTE",
                }
                missing = expected - names
                if missing:
                    return [
                        f"CueIntensity missing: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["CueIntensity not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier7_20_intensity_taxonomy_4_values"
        ),
        description="Closed 4-value CueIntensity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_orchestra.py"
        ),
        validate=_intensity_taxonomy,
    ))

    def _composes_pipeline(tree, src):
        if (
            "pipeline_progress" not in src
            or "forward_flow_phases" not in src
            or "phase_index" not in src
        ):
            return [
                "must compose canonical pipeline_progress "
                "(forward_flow_phases + phase_index) — NO "
                "parallel phase ordering"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier7_20_composes_pipeline_progress"
        ),
        description=(
            "Composes canonical pipeline_progress for "
            "11-phase tuple — NO parallel phase ordering."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_orchestra.py"
        ),
        validate=_composes_pipeline,
    ))

    def _authority(tree, src):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                m = node.module or ""
                if any(m.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden: {m}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier7_20_authority_asymmetry"
        ),
        description="Substrate purity.",
        target_file=(
            "backend/core/ouroboros/governance/"
            "phase_orchestra.py"
        ),
        validate=_authority,
    ))

    return pins


__all__ = [
    "PHASE_ORCHESTRA_SCHEMA_VERSION",
    "OrchestraNote",
    "CueIntensity",
    "OrchestraCue",
    "OrchestraLedger",
    "master_enabled",
    "bell_on_cue_enabled",
    "get_default_ledger",
    "reset_ledger_for_tests",
    "emit_cue",
    "format_orchestra_recent",
    "format_orchestra_status",
    "register_flags",
    "register_shipped_invariants",
]
