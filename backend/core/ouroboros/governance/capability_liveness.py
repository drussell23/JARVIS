"""
Capability Liveness Audit — the organism's self-perception layer
================================================================

Read-only substrate that answers the question the severed-merkle-driver bug
proved O+V could NOT answer about itself: **of all the capabilities I declare
I have, which are actually reachable — and which are silently severed?**

The archetype (bt-2026-07-16-031323, 5 quiet cycles all full scans): the
Merkle Cartographer was flag-enabled and its consumers were wired, but the
DRIVER (`update_full`, the hydration entry point) had ZERO production callers
— so every consumer fail-safed to legacy scans, silently, forever. The
fail-safe HID the deadness. A system that cannot see its own inert
capabilities cannot be trusted to notice what to improve (Gap 2 of the
autonomy pivot).

What this measures (Step 1 — static reachability, always-available):
  For each ``JARVIS_*_ENABLED`` capability the FlagRegistry declares, resolve
  its ``source_file``'s public entry symbols and ask, from git-visible ground
  truth, whether each has a PRODUCTION caller (a call site in ``backend/``
  outside the definition file and outside tests). A public symbol nobody
  calls is a *severance candidate* — the merkle class.

  Verdicts (deliberately NOT named "DARK" — ``capability_constellation``
  already uses DARK for *declared-off*; this is the opposite, declared-ON but
  inert):
    * ALIVE       — enabled; every public entry symbol has a production caller.
    * SEVERED     — enabled; ≥1 public entry symbol has zero production callers
                    (the candidates are listed; ``fraction_severed`` reported).
    * FULLY_SEVERED — a SEVERED capability where NONE of its public symbols has
                    an external caller (highest-confidence inert subsystem).
    * UNRESOLVED  — source unparseable / registered at the seed file / no public
                    symbols; never a false alarm (insufficient data, not dark).

Composition contract (DRY — no parallel machinery, no new data store):
  * ``flag_registry.list_all()`` — the 966-flag capability inventory (364
    ``*_ENABLED`` BOOL capabilities), each carrying ``source_file``. The spine.
  * A single ``ripgrep`` caller-index pass (the always-available reachability
    primitive; the Oracle CALLS-graph is a DEGRADED-fragile precision upgrade
    we deliberately do NOT depend on — Oracle-down must not forge false
    severance, per the reachability explorer's finding).
  * ``mirror_self_spec_drift`` — the EXISTING partial self-perception layer
    (docs-vs-registry default drift, a *third* self-blindness axis). Composed
    as a parallel dimension, not overlapped.
  * ``capability_constellation.principles_for_category`` — Manifesto-principle
    attribution for any severed capability (best-effort).

Firing dimension (Step 2, deliberately deferred): "enabled but never fired at
runtime" needs a federated reader over ~40 subsystem ``.jarvis/*.jsonl``
ledgers + debug.log markers and is soak-dependent. This module exposes a
pluggable ``firing_probe`` seam so Step 2 slots in without a rewrite; Step 1
ships the always-available static half that catches the merkle class at build
time, no soak required.

Authority posture: read-only projector. Imports NO orchestrator/policy/gate
module. Runs one ``rg`` subprocess and parses Python ASTs. NEVER raises;
zero-output states return a clean snapshot.
"""
from __future__ import annotations

import ast
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

CAPABILITY_LIVENESS_SCHEMA_VERSION = "capability_liveness.1"

# Flags registered at these files point at the registry plumbing, not a real
# guarded subsystem → the capability's code location is unknown → UNRESOLVED.
_SEED_BASENAMES = frozenset({"flag_registry_seed.py", "flag_registry.py", ""})


# ---------------------------------------------------------------------------
# Env knobs — additive, clamped, read-only surface
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_CAPABILITY_LIVENESS_ENABLED`` (default true). Read-only; the
    kill switch only silences the surface, it changes no behavior."""
    raw = os.environ.get("JARVIS_CAPABILITY_LIVENESS_ENABLED", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _read_clamped_int(env_name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(env_name, "").strip() or default)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def scan_cap() -> int:
    """``JARVIS_CAPABILITY_LIVENESS_SCAN_CAP`` — max capabilities audited per
    aggregation (belt-and-braces bound). Default 2000 (well above the ~364)."""
    return _read_clamped_int("JARVIS_CAPABILITY_LIVENESS_SCAN_CAP", 2000, 1, 100000)


def _snapshot_ttl_s() -> float:
    """Reachability changes slowly (code structure); cache aggressively so the
    GET is cheap. Default 900s."""
    try:
        return max(1.0, float(
            os.environ.get("JARVIS_CAPABILITY_LIVENESS_TTL_S", "").strip() or 900.0
        ))
    except (TypeError, ValueError):
        return 900.0


# ---------------------------------------------------------------------------
# Reachability primitive — one ripgrep caller-index pass, NEVER raises
# ---------------------------------------------------------------------------


def _is_test_path(rel: str) -> bool:
    """Test-path exclusion, consistent with reverse_dep_resolver semantics."""
    r = rel.replace("\\", "/")
    parts = r.split("/")
    if any(p in ("tests", "test") for p in parts):
        return True
    base = parts[-1] if parts else r
    return base.startswith("test_") or base == "conftest.py" or base.endswith("_test.py")


def _public_symbols(source: str) -> Tuple[str, ...]:
    """Public CALLABLE entry symbols of a module: top-level public functions +
    public methods of public top-level classes.

    Callables have clean "called-or-not" semantics via ``NAME(`` / ``.NAME(``
    call sites. Classes-as-symbols are deliberately EXCLUDED as targets — a
    class legitimately appears only as a type annotation, ``except`` clause, or
    base class (no ``Name(`` call), which would flood false severance. (An
    unused class still surfaces indirectly: if its methods are all severed, the
    capability trends toward FULLY_SEVERED.) NEVER raises; unparseable → empty
    (→ UNRESOLVED downstream). The merkle archetype ``update_full`` is a method
    — caught here as a call target."""
    try:
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001
        return ()
    out: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                out.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not item.name.startswith("_"):
                        out.append(item.name)
    return tuple(dict.fromkeys(out))  # de-dupe, stable order


import re as _re

# A CALL site: NAME( that is NOT a definition. The negative lookbehinds drop
# ``def NAME(`` / ``async def NAME(`` (the ``def `` immediately precedes NAME)
# and ``class NAME(`` — so a recorded hit is a genuine invocation/instantiation,
# never the definition itself. A method/attr call ``obj.NAME(`` is preceded by
# ``.`` → correctly counted.
_CALLSITE_RE = _re.compile(r"(?<!def )(?<!class )\b(\w+)\s*\(")


def _build_caller_index(
    repo: Path,
    names: Sequence[str],
    *,
    walker: Optional[Callable[[], List[Tuple[str, str]]]] = None,
) -> Dict[str, Set[str]]:
    """Map each target ``name`` → the set of production files (relative,
    non-test) containing a genuine ``name(`` CALL site (definitions excluded).

    PORTABLE by construction: a pure-Python walk of ``backend/**/*.py``, no
    external dependency (the organism's runtime cannot be assumed to ship
    ripgrep, and Rust-regex ``rg`` lacks the lookbehind this needs). NEVER
    raises; a fault on any file is skipped. An empty caller set for a name
    means "no production call site anywhere" — the provable severance signal
    (the merkle ``update_full`` archetype: defined, never invoked).

    ``walker`` is an injectable test seam returning ``[(rel_path, source), ...]``.
    """
    index: Dict[str, Set[str]] = {n: set() for n in names}
    if not names:
        return index
    target = set(names)
    rows = walker() if walker is not None else _walk_backend_sources(repo)
    for rel, source in rows:
        if _is_test_path(rel):
            continue
        try:
            for m in _CALLSITE_RE.finditer(source):
                name = m.group(1)
                if name in target:
                    index[name].add(rel)
        except Exception:  # noqa: BLE001 — one bad file never breaks the index
            continue
    return index


def _walk_backend_sources(repo: Path) -> List[Tuple[str, str]]:
    """Yield ``(rel_path, source)`` for every ``backend/**/*.py`` (excluding
    obvious vendored/venv dirs). NEVER raises."""
    out: List[Tuple[str, str]] = []
    root = repo / "backend"
    if not root.is_dir():
        return out
    _skip = {"__pycache__", ".git", "node_modules", "venv", ".venv",
             "site-packages", "dist-packages", ".mypy_cache", ".pytest_cache"}
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _skip]
            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                fp = Path(dirpath) / fn
                try:
                    rel = str(fp.relative_to(repo)).replace("\\", "/")
                    source = fp.read_text(encoding="utf-8", errors="ignore")
                except Exception:  # noqa: BLE001
                    continue
                out.append((rel, source))
    except Exception:  # noqa: BLE001
        return out
    return out


# ---------------------------------------------------------------------------
# Verdict model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityVerdict:
    flag: str
    source_file: str
    category: str
    live_on: bool
    verdict: str                       # ALIVE|SEVERED|FULLY_SEVERED|UNRESOLVED
    public_symbols: Tuple[str, ...] = ()
    severed_symbols: Tuple[str, ...] = ()
    fraction_severed: float = 0.0
    principles: Tuple[str, ...] = ()
    reason: str = ""
    firing: str = "UNKNOWN"            # FIRING|SILENT|UNKNOWN (Step 2)
    firing_hits: Tuple[str, ...] = ()
    firing_channels: Tuple[str, ...] = ()   # derived: "ledger" (reliable) / "log"

    @property
    def ledger_backed(self) -> bool:
        """A SILENT verdict with a ``ledger`` channel is high-confidence
        dormancy (evidence-of-work channel went quiet)."""
        return "ledger" in self.firing_channels

    def to_dict(self) -> Dict[str, Any]:
        return {
            "flag": self.flag,
            "source_file": self.source_file,
            "category": self.category,
            "live_on": self.live_on,
            "verdict": self.verdict,
            "public_symbols": list(self.public_symbols),
            "severed_symbols": list(self.severed_symbols),
            "fraction_severed": round(self.fraction_severed, 4),
            "principles": list(self.principles),
            "reason": self.reason,
            "firing": self.firing,
            "firing_hits": list(self.firing_hits),
            "firing_channels": list(self.firing_channels),
            "ledger_backed": self.ledger_backed,
        }


def _verdict_for(
    *,
    flag: str,
    source_file: str,
    category: str,
    live_on: bool,
    repo: Path,
    caller_index: Dict[str, Set[str]],
    principles_fn: Optional[Callable[[str], Sequence[str]]],
    firing_probe: Optional[Callable[[str], Tuple[str, List[str], List[str]]]] = None,
) -> CapabilityVerdict:
    """Pure per-capability verdict from the caller-index (+ optional firing
    probe). Never raises."""
    _flag, _sf, _cat, _on = str(flag), str(source_file), str(category), bool(live_on)

    def _mk(verdict: str, **extra: Any) -> CapabilityVerdict:
        return CapabilityVerdict(
            flag=_flag, source_file=_sf, category=_cat, live_on=_on,
            verdict=verdict, **extra,
        )

    rel = (source_file or "").replace("\\", "/")
    if os.path.basename(rel) in _SEED_BASENAMES:
        return _mk("UNRESOLVED",
                   reason="registered at the flag-registry seed; no guarded module")
    abs_path = repo / rel
    try:
        source = abs_path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return _mk("UNRESOLVED", reason="source_file unreadable")

    # Firing dimension (Step 2) — derived from THIS source, judged over the
    # adaptive runtime evidence window. UNKNOWN when firing is off/unprovable.
    firing, fhits, fchan = ("UNKNOWN", [], [])
    if firing_probe is not None:
        firing, fhits, fchan = firing_probe(source)

    symbols = _public_symbols(source)
    if not symbols:
        return _mk("UNRESOLVED", firing=firing, firing_hits=tuple(fhits),
                   firing_channels=tuple(fchan),
                   reason="no public callable entry symbols to resolve reachability")
    severed: List[str] = []
    for sym in symbols:
        # A genuine CALL site anywhere in production (definitions are already
        # excluded from the caller-index) — same-file internal use counts as
        # ALIVE (it IS used). Zero call sites anywhere = severed (the merkle
        # ``update_full`` signature: defined, invoked by no production code).
        if not caller_index.get(sym, set()):
            severed.append(sym)
    fraction = len(severed) / len(symbols) if symbols else 0.0
    principles: Tuple[str, ...] = ()
    if severed and principles_fn is not None:
        try:
            principles = tuple(principles_fn(category) or ())
        except Exception:  # noqa: BLE001
            principles = ()
    if not severed:
        return _mk("ALIVE", public_symbols=symbols, fraction_severed=0.0,
                   firing=firing, firing_hits=tuple(fhits),
                   firing_channels=tuple(fchan))
    verdict = "FULLY_SEVERED" if fraction >= 1.0 else "SEVERED"
    return _mk(
        verdict,
        public_symbols=symbols, severed_symbols=tuple(severed),
        fraction_severed=fraction, principles=principles,
        firing=firing, firing_hits=tuple(fhits), firing_channels=tuple(fchan),
        reason=(
            "no public callable has a production call site — likely inert subsystem"
            if fraction >= 1.0 else
            "one or more public callables have no production call site (static-only; "
            "may be dynamically dispatched — a review candidate, not proven dead)"
        ),
    )


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LivenessSnapshot:
    schema_version: str = CAPABILITY_LIVENESS_SCHEMA_VERSION
    enabled: bool = True
    reason_code: str = "ok"
    generated_at_unix: float = 0.0
    capabilities_declared: int = 0
    capabilities_resolved: int = 0
    counts: Dict[str, int] = field(default_factory=dict)
    callable_reachability_ratio: Optional[float] = None
    fully_severed: List[Dict[str, Any]] = field(default_factory=list)
    severance_candidates: List[Dict[str, Any]] = field(default_factory=list)
    dormant: List[Dict[str, Any]] = field(default_factory=list)
    observability_gaps: List[Dict[str, Any]] = field(default_factory=list)
    firing_evidence: Dict[str, Any] = field(default_factory=lambda: {"implemented": False})
    spec_drift: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "enabled": self.enabled,
            "reason_code": self.reason_code,
            "generated_at_unix": self.generated_at_unix,
            "capabilities": {
                "declared_enabled": self.capabilities_declared,
                "resolved": self.capabilities_resolved,
                "counts": dict(self.counts),
                # Fraction of resolved enabled capabilities whose EVERY public
                # callable has a production call site. This is a TREND metric,
                # not an absolute health score — static analysis under-detects
                # dynamic dispatch, so the absolute value undercounts liveness.
                # Its VALUE is movement: a capability flipping ALIVE→SEVERED
                # between snapshots is a NEWLY-introduced severance (a merkle
                # regression). See interpretation note.
                "callable_reachability_ratio": self.callable_reachability_ratio,
            },
            # HIGH-CONFIDENCE alert: capabilities where NO public callable has a
            # production call site — the whole enabled subsystem is statically
            # unreferenced (very likely genuinely inert). This is the actionable
            # signal.
            "fully_severed": self.fully_severed,
            # TRIAGE data (NOT a verdict): enabled capabilities with ≥1 public
            # callable that has no static call site. Ranked most-severed first.
            # Explicitly static-only — a symbol here may be dynamically
            # dispatched; treat as a review candidate, not proven-dead.
            "severance_candidates": self.severance_candidates,
            # THE PRECISE MERKLE SIGNATURE (Step 2): capabilities that ARE
            # statically reachable (ALIVE) yet emitted NO runtime firing
            # evidence in the adaptive window — wired but dormant. This is the
            # highest-value output: the exact class the static half could not
            # distinguish, now caught by combining reachability with runtime
            # evidence.
            "dormant": self.dormant,
            # Third self-blindness axis (docs-vs-registry drift), composed from
            # the existing mirror_self_spec_drift auditor.
            "spec_drift": self.spec_drift,
            "interpretation": (
                "Self-perception combines two proofs: STATIC reachability "
                "('this callable has a production call site' — catches whole-"
                "module inertia + newly-introduced severances via trend) and "
                "RUNTIME firing ('this subsystem emitted evidence in the recent "
                "window' — markers derived from the capability's own source, "
                "judged over an adaptive session-anchored window). Their "
                "intersection — ALIVE but SILENT = 'dormant' — is the precise "
                "merkle class: reachable, wired, but not running."
            ),
            # Reachable + SILENT but only a LOG channel (no ledger) — may be
            # running silently. NOT proven dormant; an observability gap worth
            # closing (the subsystem should emit durable evidence).
            "observability_gaps": self.observability_gaps,
            # Runtime firing dimension summary (evidence window + counts +
            # dormant/gap totals). ``implemented: false`` when the firing master
            # is off (firing then UNKNOWN for all; no dormant analysis).
            "firing": self.firing_evidence,
        }


def _resolve_repo_root(explicit: Optional[Path] = None) -> Path:
    if explicit is not None:
        return explicit
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        if (parent / ".git").exists() or (parent / "backend").is_dir():
            return parent
    return Path.cwd()


def _spec_drift_dimension() -> Dict[str, Any]:
    """Compose the EXISTING docs-vs-registry drift auditor as a parallel
    self-blindness dimension. Best-effort; never raises."""
    try:
        from backend.core.ouroboros.governance.mirror_self_spec_drift import (
            detect_spec_drift, master_enabled as _drift_on,
        )
        if not _drift_on():
            return {"verdict": "DISABLED", "note": "spec-drift master off"}
        report = detect_spec_drift()
        return {
            "verdict": str(getattr(report.verdict, "value", report.verdict)),
            "claims_evaluated": int(getattr(report, "claims_evaluated", 0) or 0),
            "drifted": len([
                r for r in getattr(report, "records", ())
                if getattr(r, "drifted", False)
            ]),
        }
    except Exception:  # noqa: BLE001
        return {"verdict": "UNAVAILABLE"}


def _principles_fn() -> Optional[Callable[[str], Sequence[str]]]:
    try:
        from backend.core.ouroboros.governance.capability_constellation import (
            principles_for_category,
        )
        return principles_for_category
    except Exception:  # noqa: BLE001
        return None


def _resolve_firing_probe(
    repo: Path, now: float,
) -> Tuple[Optional[Callable[[str], Tuple[str, List[str], List[str]]]], Optional[Dict[str, Any]]]:
    """Build the Step-2 firing probe from ``capability_firing`` when its master
    is on. Returns ``(probe, evidence_summary)`` or ``(None, None)`` when firing
    is off/unavailable (→ firing stays UNKNOWN, no dormant analysis). Never
    raises."""
    try:
        from backend.core.ouroboros.governance import capability_firing as cf
        if not cf.master_enabled():
            return None, None
        probe, evidence = cf.build_firing_probe(repo, now=now)
        return probe, evidence.to_dict()
    except Exception:  # noqa: BLE001
        return None, None


def aggregate_capability_liveness(
    *,
    repo_root: Optional[Path] = None,
    now: Optional[float] = None,
    walker: Optional[Callable[[], List[Tuple[str, str]]]] = None,  # test seam
    firing_probe: Optional[Callable[[str], Tuple[str, List[str]]]] = None,  # Step 2 seam
) -> LivenessSnapshot:
    """Compose the capability-liveness snapshot from the FlagRegistry + a
    ripgrep caller-index. Read-only; NEVER raises — any fault degrades to a
    partial snapshot with a diagnostic ``reason_code``. Zero-output safe.

    ``firing_probe`` is the deferred Step-2 seam: a callable
    ``(flag_name) -> bool`` (did the subsystem fire in the window). Unused in
    Step 1; wired here so Step 2 needs no signature change.
    """
    try:
        _now = float(now) if now is not None else time.time()
        repo = _resolve_repo_root(repo_root)

        # --- enumerate declared capabilities ---
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                ensure_seeded, FlagType,
            )
            registry = ensure_seeded()
            specs = [
                s for s in registry.list_all()
                if s.name.endswith("_ENABLED") and s.type is FlagType.BOOL
            ][:scan_cap()]
        except Exception:  # noqa: BLE001
            return LivenessSnapshot(
                enabled=True, reason_code="flag_registry_unavailable",
                generated_at_unix=_now, spec_drift=_spec_drift_dimension(),
            )

        # Only audit LIVE-ON capabilities (a declared-off flag is not a
        # severance concern — that's the constellation-DARK axis, not ours).
        live_specs = []
        for s in specs:
            try:
                on = registry.get_bool(s.name)
            except Exception:  # noqa: BLE001
                on = bool(getattr(s, "default", False))
            if on:
                live_specs.append(s)

        # --- collect target symbols across all live capabilities (one pass) ---
        cap_symbols: Dict[str, Tuple[str, ...]] = {}
        for s in live_specs:
            rel = (s.source_file or "").replace("\\", "/")
            if os.path.basename(rel) in _SEED_BASENAMES:
                continue
            try:
                source = (repo / rel).read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            cap_symbols[s.name] = _public_symbols(source)

        all_names = sorted({n for syms in cap_symbols.values() for n in syms})
        caller_index = _build_caller_index(repo, all_names, walker=walker)
        reason = "ok"

        # --- firing dimension (Step 2) — one durable evidence read, amortized ---
        _firing_probe = firing_probe
        firing_evidence: Dict[str, Any] = {"implemented": False}
        if _firing_probe is None:
            _firing_probe, _fev = _resolve_firing_probe(repo, _now)
            if _fev is not None:
                firing_evidence = {"implemented": True, **_fev}
        elif _firing_probe is not None:
            firing_evidence = {"implemented": True, "source": "injected"}

        # --- per-capability verdicts ---
        principles_fn = _principles_fn()
        verdicts: List[CapabilityVerdict] = []
        for s in live_specs:
            v = _verdict_for(
                flag=s.name,
                source_file=(s.source_file or ""),
                category=str(getattr(getattr(s, "category", None), "value", getattr(s, "category", ""))),
                live_on=True,
                repo=repo,
                caller_index=caller_index,
                principles_fn=principles_fn,
                firing_probe=_firing_probe,
            )
            verdicts.append(v)

        counts: Dict[str, int] = {}
        for v in verdicts:
            counts[v.verdict] = counts.get(v.verdict, 0) + 1
        alive = counts.get("ALIVE", 0)
        severed_n = counts.get("SEVERED", 0) + counts.get("FULLY_SEVERED", 0)
        resolved = alive + severed_n
        ratio = round(alive / resolved, 4) if resolved > 0 else None

        fully = sorted(
            (v.to_dict() for v in verdicts if v.verdict == "FULLY_SEVERED"),
            key=lambda d: d["flag"],
        )
        candidates = sorted(
            (v.to_dict() for v in verdicts if v.verdict == "SEVERED"),
            key=lambda d: (-d["fraction_severed"], d["flag"]),
        )

        # --- the precise merkle signature: REACHABLE (ALIVE) but SILENT ---
        # A capability whose callables all have production call sites (so it is
        # statically wired) yet emitted NO runtime evidence in the window is
        # dormant — the exact class the static half could not distinguish.
        # HIGH-CONFIDENCE dormancy requires a LEDGER evidence-of-work channel
        # that went quiet (the merkle merkle_history.jsonl case); a log-tag-only
        # silence is downgraded to observability_gap (may be running silently).
        firing_counts: Dict[str, int] = {}
        for v in verdicts:
            firing_counts[v.firing] = firing_counts.get(v.firing, 0) + 1
        _alive_silent = [
            v for v in verdicts if v.verdict == "ALIVE" and v.firing == "SILENT"
        ]
        dormant = sorted(
            (v.to_dict() for v in _alive_silent if v.ledger_backed),
            key=lambda d: d["flag"],
        )
        observability_gaps = sorted(
            (v.to_dict() for v in _alive_silent if not v.ledger_backed),
            key=lambda d: d["flag"],
        )
        firing_evidence["counts"] = firing_counts
        firing_evidence["dormant_count"] = len(dormant)
        firing_evidence["observability_gap_count"] = len(observability_gaps)

        return LivenessSnapshot(
            enabled=True, reason_code=reason, generated_at_unix=_now,
            capabilities_declared=len(live_specs),
            capabilities_resolved=resolved,
            counts=counts,
            firing_evidence=firing_evidence,
            dormant=dormant,
            observability_gaps=observability_gaps,
            callable_reachability_ratio=ratio,
            fully_severed=fully,
            severance_candidates=candidates,
            spec_drift=_spec_drift_dimension(),
        )
    except Exception:  # noqa: BLE001 — aggregator MUST never raise
        return LivenessSnapshot(
            enabled=True, reason_code="aggregation_error",
            generated_at_unix=(now if now is not None else 0.0) or 0.0,
        )


# ---------------------------------------------------------------------------
# Cached snapshot for the observability GET
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_LAST: Optional[LivenessSnapshot] = None
_LAST_TS: float = 0.0


def snapshot(*, repo_root: Optional[Path] = None, force: bool = False) -> Dict[str, Any]:
    """Public entry for the observability GET — serializable dict, TTL-cached.
    Master-off → minimal ``enabled: false`` body (endpoint decides 403). NEVER
    raises."""
    if not master_enabled():
        return {
            "schema_version": CAPABILITY_LIVENESS_SCHEMA_VERSION,
            "enabled": False, "reason_code": "disabled",
        }
    global _LAST, _LAST_TS
    now = time.time()
    with _LOCK:
        if not force and _LAST is not None and (now - _LAST_TS) <= _snapshot_ttl_s():
            return _LAST.to_dict()
    snap = aggregate_capability_liveness(repo_root=repo_root, now=now)
    with _LOCK:
        _LAST = snap
        _LAST_TS = now
    return snap.to_dict()


def reset_cache_for_tests() -> None:
    global _LAST, _LAST_TS
    with _LOCK:
        _LAST = None
        _LAST_TS = 0.0
