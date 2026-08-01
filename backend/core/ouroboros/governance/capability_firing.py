"""
Capability Firing Dimension — the runtime half of self-perception (Gap 2 Step 2)
================================================================================

Static reachability (``capability_liveness``, Step 1) proves "this symbol has a
production call site." It CANNOT prove the thing that actually mattered for the
merkle bug: that the capability, though reachable, **never actually ran**. The
Merkle Cartographer was reachable AND its consumers were wired — but the driver
never fired, so its ledger stayed empty and its log tag never appeared. That is
a *runtime* signal, not a static one.

This module supplies the precise detector: for each enabled capability, did its
subsystem emit any runtime evidence in the recent observation window? The
combination — **reachable (ALIVE) but SILENT (no firing evidence)** — is the
generalizable merkle signature (wired, callable, dormant), computed by
``capability_liveness`` from this module's verdicts.

No hardcoding — everything is DERIVED:
  * A capability's firing MARKERS are extracted from its OWN ``source_file``:
    the bracket log tags it prints (``logger.info("[MerkleCartographer] ...")``
    → ``MerkleCartographer``) and the ``*.jsonl`` ledger stems it writes
    (``merkle_history.jsonl`` → ``merkle_history``). The code declares its own
    observable identity; we read it, we don't enumerate it.
  * The EVIDENCE window is ADAPTIVE — anchored to the most-recent battle-test
    sessions (actual observation periods), not a fixed wall-clock, so "never
    fired" means "absent across the real recent activity," never a false
    silence during idle wall-time.
  * Evidence is DURABLE ground truth: ``.jarvis/*.jsonl`` ledger file mtimes
    (a ledger written in-window ⇒ its owner fired) + the ``[Tag]`` markers
    present in the recent sessions' ``debug.log``.

Verdicts: FIRING (≥1 derived marker seen in-window) / SILENT (markers derivable
but none seen — dormant candidate) / UNKNOWN (no observable markers in the
source — firing is unprovable, never a false alarm).

Authority posture: read-only projector. Imports NO orchestrator/policy/gate
module. Reads files only. NEVER raises.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

CAPABILITY_FIRING_SCHEMA_VERSION = "capability_firing.1"

# A log-tag marker: ``[Name]`` at the START of a string literal (a log message),
# NOT ``List[Any]`` type-hint brackets (which are not preceded by a quote).
_LOG_TAG_RE = re.compile(r"""["']\s*\[([A-Z][A-Za-z0-9_.]{1,48})\]""")
# A logger identity: ``getLogger("Ouroboros.MerkleCartographer")`` → last comp.
_GETLOGGER_RE = re.compile(r"""getLogger\(\s*["']([\w.]+)["']""")
# A ledger stem the source writes/reads: ``merkle_history.jsonl`` → merkle_history.
_LEDGER_STEM_RE = re.compile(r"""([A-Za-z0-9_]{2,64})\.jsonl""")
# A bracket marker present in a debug.log line: ``[MerkleCartographer]``.
_LOGLINE_TAG_RE = re.compile(r"\[([A-Z][A-Za-z0-9_.]{1,48})\]")

# A module DECLARING evidence it writes through a collaborator:
#
#     __firing_ledgers__ = ("repair_tree",)
#     __firing_tags__ = ("Treefinement",)
#
# Marker derivation is SOURCE-LOCAL: a stem is found only if the literal
# ``name.jsonl`` appears in the capability's own text. That is right for a
# module that opens its own file and wrong for one that delegates, and
# delegating is the better design — so the observability layer was penalising
# exactly the code that separates concerns.
#
# ``repair_engine`` is the case that surfaced it. L2 persists every attempt:
# `repair_engine` → `repair_tree._archive_result` → `repair_tree_archive` →
# ``.jarvis/ouroboros/repair_tree.jsonl``, via the canonical flock append. Two
# hops, so ``repair_engine.py`` contains no ``.jsonl`` literal, so it derived
# LOG TAGS ONLY — and a log-only silence is explicitly ambiguous (see
# `firing_verdict`). L2 was therefore unprovable rather than dormant, and the
# liveness sensor scored that unprovability as a HIGH-severity severance of a
# ``safety`` capability.
#
# Following import edges instead was considered and rejected: `repair_engine`
# imports a dozen modules, most of which write ledgers of their own, and
# inheriting all of them would make every capability look FIRING — trading a
# false-dead for a false-alive, which is strictly worse for an auditor.
#
# So the module says so itself. This is the convention this codebase already
# uses for module-level metadata (``__verb_help__``, ``__aliases__``), and it
# keeps the declaration next to the code that would change if the evidence
# path did. Parsed from TEXT, not imported — derivation must stay pure and
# must never execute a capability to ask about it.
_DECLARED_LEDGERS_RE = re.compile(
    r"""__firing_ledgers__\s*=\s*\(([^)]*)\)""", re.S)
_DECLARED_TAGS_RE = re.compile(
    r"""__firing_tags__\s*=\s*\(([^)]*)\)""", re.S)
_STRING_ITEM_RE = re.compile(r"""["']([A-Za-z0-9_.\-]{1,64})["']""")


# ---------------------------------------------------------------------------
# Env knobs — additive, clamped, read-only
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_CAPABILITY_FIRING_ENABLED`` (default true). Read-only; a kill
    switch only silences the dimension."""
    raw = os.environ.get("JARVIS_CAPABILITY_FIRING_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _clamped_int(env: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(env, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def max_sessions() -> int:
    """``JARVIS_CAPABILITY_FIRING_MAX_SESSIONS`` — recent session debug.logs
    scanned for log-tag evidence. Default 3."""
    return _clamped_int("JARVIS_CAPABILITY_FIRING_MAX_SESSIONS", 3, 1, 50)


def window_fallback_s() -> float:
    """``JARVIS_CAPABILITY_FIRING_WINDOW_S`` — wall-clock ledger window used
    ONLY when no sessions are available to anchor the adaptive window.
    Default 604800 (7d)."""
    try:
        return max(60.0, float(
            os.environ.get("JARVIS_CAPABILITY_FIRING_WINDOW_S", "").strip() or 604800.0
        ))
    except (TypeError, ValueError):
        return 604800.0


def max_log_bytes() -> int:
    """``JARVIS_CAPABILITY_FIRING_MAX_LOG_BYTES`` — cap per debug.log scan so a
    giant log can't stall the reader. Default 96 MiB."""
    return _clamped_int(
        "JARVIS_CAPABILITY_FIRING_MAX_LOG_BYTES", 96 * 1024 * 1024,
        1024 * 1024, 1024 * 1024 * 1024,
    )


# ---------------------------------------------------------------------------
# Derived markers (from a capability's own source) — pure, never raises
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DerivedMarkers:
    tags: Tuple[str, ...] = ()      # log bracket tags + logger last-components
    ledgers: Tuple[str, ...] = ()   # *.jsonl stems the source references

    @property
    def empty(self) -> bool:
        return not self.tags and not self.ledgers


def derive_markers(source: str) -> DerivedMarkers:
    """Extract a capability's observable firing markers from its source. NEVER
    raises. Empty markers → firing is UNKNOWN (unprovable, never a false
    SILENT)."""
    if not source:
        return DerivedMarkers()
    tags: Set[str] = set()
    ledgers: Set[str] = set()
    try:
        for m in _LOG_TAG_RE.finditer(source):
            tags.add(m.group(1).split(".")[-1])
        for m in _GETLOGGER_RE.finditer(source):
            comp = m.group(1).split(".")[-1]
            if comp and comp[0].isupper():
                tags.add(comp)
        for m in _LEDGER_STEM_RE.finditer(source):
            ledgers.add(m.group(1))
        # Evidence written THROUGH a collaborator, declared by the module that
        # causes it. Unioned with the scanned markers rather than replacing
        # them: a module may both open its own ledger and drive another's.
        for m in _DECLARED_LEDGERS_RE.finditer(source):
            for item in _STRING_ITEM_RE.finditer(m.group(1)):
                stem = item.group(1)
                ledgers.add(stem[:-len(".jsonl")]
                            if stem.endswith(".jsonl") else stem)
        for m in _DECLARED_TAGS_RE.finditer(source):
            for item in _STRING_ITEM_RE.finditer(m.group(1)):
                tags.add(item.group(1).split(".")[-1])
    except Exception:  # noqa: BLE001
        pass
    # Drop generic/noise tags that would over-match (common Python builtins that
    # appear bracketed in type strings but slipped the quote guard).
    tags.discard("Any")
    tags.discard("Path")
    return DerivedMarkers(tuple(sorted(tags)), tuple(sorted(ledgers)))


# ---------------------------------------------------------------------------
# Runtime evidence collection — durable, adaptive window, never raises
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FiringEvidence:
    present_markers: Set[str] = field(default_factory=set)   # log tags seen in-window
    active_ledgers: Set[str] = field(default_factory=set)    # ledger stems written in-window
    window_start_unix: float = 0.0
    sessions_scanned: int = 0
    ledgers_scanned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "window_start_unix": self.window_start_unix,
            "sessions_scanned": self.sessions_scanned,
            "ledgers_scanned": self.ledgers_scanned,
            "distinct_markers_seen": len(self.present_markers),
            "active_ledgers": len(self.active_ledgers),
        }


def _session_epoch(name: str) -> Optional[float]:
    """Best-effort start epoch from a ``bt-…`` session dir name."""
    try:
        if name.startswith("bt-iso-"):
            return float(name[len("bt-iso-"):])
        parts = name.split("-")
        if len(parts) >= 5 and parts[0] == "bt":
            import calendar
            hms = parts[4]
            return float(calendar.timegm((
                int(parts[1]), int(parts[2]), int(parts[3]),
                int(hms[0:2]), int(hms[2:4]), int(hms[4:6]), 0, 0, -1,
            )))
    except Exception:  # noqa: BLE001
        return None
    return None


def collect_firing_evidence(
    repo: Path,
    *,
    now: float,
    sessions_reader: Optional[Callable[[], List[Tuple[str, str]]]] = None,
    ledger_reader: Optional[Callable[[], List[Tuple[str, float]]]] = None,
) -> FiringEvidence:
    """Collect durable firing evidence over an ADAPTIVE window anchored to the
    most-recent sessions. NEVER raises.

    ``sessions_reader`` (test seam) → ``[(session_name, debug_log_text), ...]``
    newest-first. ``ledger_reader`` (test seam) → ``[(ledger_stem, mtime), ...]``.
    """
    # --- recent sessions (log-tag evidence) + adaptive window anchor ---
    present: Set[str] = set()
    sessions_scanned = 0
    window_start = now - window_fallback_s()
    session_starts: List[float] = []
    try:
        sess_rows = (
            sessions_reader() if sessions_reader is not None
            else _read_recent_sessions(repo, max_sessions())
        )
    except Exception:  # noqa: BLE001
        sess_rows = []
    for name, log_text in sess_rows:
        sessions_scanned += 1
        ep = _session_epoch(name)
        if ep is not None:
            session_starts.append(ep)
        try:
            for m in _LOGLINE_TAG_RE.finditer(log_text):
                present.add(m.group(1).split(".")[-1])
        except Exception:  # noqa: BLE001
            continue
    if session_starts:
        # Window opens at the earliest recent session we scanned — firing is
        # judged over the ACTUAL recent observation period, not idle wall-time.
        window_start = min(session_starts)

    # --- ledgers written in-window (subsystem-activity evidence) ---
    active: Set[str] = set()
    ledgers_scanned = 0
    try:
        led_rows = (
            ledger_reader() if ledger_reader is not None
            else _read_ledger_mtimes(repo)
        )
    except Exception:  # noqa: BLE001
        led_rows = []
    for stem, mtime in led_rows:
        ledgers_scanned += 1
        if mtime >= window_start:
            active.add(stem)

    return FiringEvidence(
        present_markers=present, active_ledgers=active,
        window_start_unix=window_start,
        sessions_scanned=sessions_scanned, ledgers_scanned=ledgers_scanned,
    )


def _read_recent_sessions(repo: Path, limit: int) -> List[Tuple[str, str]]:
    """Newest-first ``[(name, debug_log_text)]`` for the recent sessions. Bounded
    read per log. NEVER raises."""
    root = repo / ".ouroboros" / "sessions"
    out: List[Tuple[str, str]] = []
    try:
        if not root.is_dir():
            return out
        dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and p.name.startswith("bt-")),
            key=lambda p: p.name, reverse=True,
        )[:limit]
    except Exception:  # noqa: BLE001
        return out
    cap = max_log_bytes()
    for d in dirs:
        log = d / "debug.log"
        text = ""
        try:
            if log.is_file():
                with open(log, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read(cap)
        except Exception:  # noqa: BLE001
            text = ""
        out.append((d.name, text))
    return out


def _read_ledger_mtimes(repo: Path) -> List[Tuple[str, float]]:
    """``[(ledger_stem, mtime_epoch)]`` for every ``.jarvis/**/*.jsonl``. NEVER
    raises."""
    out: List[Tuple[str, float]] = []
    root = repo / ".jarvis"
    try:
        if not root.is_dir():
            return out
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                try:
                    mt = (Path(dirpath) / fn).stat().st_mtime
                except Exception:  # noqa: BLE001
                    continue
                out.append((fn[:-len(".jsonl")], mt))
    except Exception:  # noqa: BLE001
        return out
    return out


# ---------------------------------------------------------------------------
# Per-capability firing verdict
# ---------------------------------------------------------------------------


def firing_verdict(
    source: str, evidence: FiringEvidence,
) -> Tuple[str, List[str], List[str]]:
    """Return ``(verdict, hits, channels)``: FIRING / SILENT / UNKNOWN plus the
    DERIVED marker channels (``"ledger"`` and/or ``"log"``). Pure; never raises.

      * derived markers ∩ (present log tags ∪ active ledger stems) non-empty
        → FIRING (hits listed).
      * markers derivable but none seen in-window → SILENT (dormant candidate).
      * no derivable markers → UNKNOWN (firing unprovable — never a false SILENT).

    ``channels`` distinguishes the CONFIDENCE of a SILENT verdict: a ``ledger``
    channel is reliable evidence-of-work (a ledger is written only when the
    subsystem does durable work → an inactive ledger is a strong dormancy
    signal, the merkle ``merkle_history.jsonl`` case), whereas a ``log``-only
    channel is ambiguous (an absent log tag may mean 'ran silently' — an
    observability gap — not proven dormancy)."""
    markers = derive_markers(source)
    if markers.empty:
        return "UNKNOWN", [], []
    channels: List[str] = []
    if markers.ledgers:
        channels.append("ledger")
    if markers.tags:
        channels.append("log")
    hits: List[str] = []
    for t in markers.tags:
        if t in evidence.present_markers:
            hits.append(f"tag:{t}")
    for lg in markers.ledgers:
        if lg in evidence.active_ledgers:
            hits.append(f"ledger:{lg}")
    return ("FIRING" if hits else "SILENT"), hits, channels


def build_firing_probe(
    repo: Path, *, now: Optional[float] = None,
    sessions_reader: Optional[Callable[[], List[Tuple[str, str]]]] = None,
    ledger_reader: Optional[Callable[[], List[Tuple[str, float]]]] = None,
) -> Tuple[Callable[[str], Tuple[str, List[str], List[str]]], FiringEvidence]:
    """Build a ``(source_text) -> (verdict, hits, channels)`` probe bound to a
    single freshly-collected evidence snapshot (one durable read amortized
    across all capabilities). Returns ``(probe, evidence)``. NEVER raises — on
    fault the probe returns UNKNOWN for everything."""
    _now = float(now) if now is not None else time.time()
    try:
        evidence = collect_firing_evidence(
            repo, now=_now,
            sessions_reader=sessions_reader, ledger_reader=ledger_reader,
        )
    except Exception:  # noqa: BLE001
        evidence = FiringEvidence(window_start_unix=_now)

    def _probe(source: str) -> Tuple[str, List[str], List[str]]:
        try:
            return firing_verdict(source, evidence)
        except Exception:  # noqa: BLE001
            return "UNKNOWN", [], []

    return _probe, evidence
