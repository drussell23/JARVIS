#!/usr/bin/env python3
"""Empirical-closure verdict for the ClusterIntelligence-CrossSession arc.

Reads a single battle-test session's artifacts (``summary.json`` +
``debug.log``) and the cross-session ``.jarvis/domain_map/`` directory,
then computes a structured verdict over four contracts the arc was
supposed to deliver:

  Contract 1 — SemanticIndex substrate is OPERATIONAL
              (corpus_n > 0 AND cluster_count > 0)
  Contract 2 — Adaptive embedder is FUNCTIONAL
              (either fastembed loaded OR the stdlib fallback engaged)
  Contract 3 — cluster_coverage envelopes FIRED at least once
              (the sensor path the arc unblocked is reachable in production)
  Contract 4 — Cross-session DomainMap is POPULATED
              (DomainMapMemory persisted at least one centroid_hash8.json
              entry; future sessions will read it on boot)

Optional Contract 5 — doc_staleness:exploration RATIO inverted vs the
v3 baseline. Treated as advisory because v3's per-soak signal volume
varied with cost cap + wall-clock cap; not a structural pass/fail.

Exit codes:
    0 = all four primary contracts PASSED
    1 = at least one primary contract FAILED
    2 = session artifacts missing / unparseable

Usage:
    python3 scripts/empirical_closure_verdict.py [SESSION_ID]

If SESSION_ID is omitted, the script picks the most recent session
under ``.ouroboros/sessions/``. NEVER mutates state.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = REPO_ROOT / ".ouroboros" / "sessions"
DOMAIN_MAP_DIR = REPO_ROOT / ".jarvis" / "domain_map"


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SessionArtifacts:
    session_id: str
    session_dir: Path
    summary: Dict[str, object]
    debug_log_text: str
    domain_map_entries: Tuple[Path, ...]


def _resolve_session(arg: Optional[str]) -> Optional[Path]:
    if arg:
        candidate = SESSIONS_DIR / arg
        return candidate if candidate.is_dir() else None
    if not SESSIONS_DIR.is_dir():
        return None
    sessions = sorted(
        (p for p in SESSIONS_DIR.iterdir() if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    return sessions[0] if sessions else None


def _load_artifacts(session_dir: Path) -> Optional[SessionArtifacts]:
    debug_path = session_dir / "debug.log"
    if not debug_path.is_file():
        return None
    summary_path = session_dir / "summary.json"
    summary: Dict[str, object] = {}
    if summary_path.is_file():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception:
            summary = {"_parse_error": "summary.json corrupt"}
    else:
        summary = {
            "_status": "missing -- session terminated before summary write",
        }
    try:
        debug_log_text = debug_path.read_text(
            encoding="utf-8", errors="replace",
        )
    except Exception:
        debug_log_text = ""
    if DOMAIN_MAP_DIR.is_dir():
        entries = tuple(sorted(DOMAIN_MAP_DIR.glob("*.json")))
    else:
        entries = ()
    return SessionArtifacts(
        session_id=session_dir.name,
        session_dir=session_dir,
        summary=summary,
        debug_log_text=debug_log_text,
        domain_map_entries=entries,
    )


# ---------------------------------------------------------------------------
# Contract evaluators
# ---------------------------------------------------------------------------


_RE_SEMINDEX = re.compile(
    r"\[SemanticIndex\] op=\S+ corpus_n=(\d+) centroid_hash8="
)
# Build-completion line emitted by SemanticIndex.build() once per
# refresh -- carries corpus_n authoritatively even when no per-op
# CONTEXT_EXPANSION inject has fired yet.
_RE_SEMINDEX_BUILT = re.compile(
    r"\[SemanticIndex\] built_at=\d+ corpus_n=(\d+)"
)
_RE_CLUSTER_BUILT = re.compile(
    r"cluster_mode=(\w+) cluster_count=(\d+)"
)
_RE_FALLBACK = re.compile(
    r"\[SemanticIndex\] embedder fallback activated:"
)
_RE_FASTEMBED_LOADED = re.compile(
    r"\[SemanticIndex\] fastembed loaded:"
)
_RE_CLUSTER_COVERAGE_EMIT = re.compile(
    r"\[ExplorationSensor\] Cluster-coverage emit "
    r"cluster=(\d+) kind=(\w+) size=(\d+) hash=(\S+)"
)
_RE_DOC_STALENESS_EMIT = re.compile(
    r"doc_staleness", re.IGNORECASE,
)


def _eval_substrate(art: SessionArtifacts) -> ContractVerdict:
    inject_matches = _RE_SEMINDEX.findall(art.debug_log_text)
    build_matches = _RE_SEMINDEX_BUILT.findall(art.debug_log_text)
    cluster_matches = _RE_CLUSTER_BUILT.findall(art.debug_log_text)
    max_corpus_n = max(
        (int(n) for n in (*inject_matches, *build_matches)),
        default=0,
    )
    cluster_modes = [m[0] for m in cluster_matches]
    max_cluster_count = max(
        (int(m[1]) for m in cluster_matches), default=0,
    )
    passed = max_corpus_n > 0 and max_cluster_count > 0
    return ContractVerdict(
        name="C1 SemanticIndex substrate operational",
        passed=passed,
        evidence=(
            f"build_lines={len(build_matches)} "
            f"inject_lines={len(inject_matches)} "
            f"max_corpus_n={max_corpus_n} "
            f"max_cluster_count={max_cluster_count} "
            f"observed_cluster_modes={sorted(set(cluster_modes))}"
        ),
        details={
            "max_corpus_n": max_corpus_n,
            "max_cluster_count": max_cluster_count,
            "observed_cluster_modes": sorted(set(cluster_modes)),
        },
    )


def _eval_embedder(art: SessionArtifacts) -> ContractVerdict:
    fallback = bool(_RE_FALLBACK.search(art.debug_log_text))
    fastembed_ok = bool(
        _RE_FASTEMBED_LOADED.search(art.debug_log_text),
    )
    passed = fallback or fastembed_ok
    if fastembed_ok and not fallback:
        chosen = "fastembed (primary)"
    elif fallback:
        chosen = "stdlib (fallback)"
    else:
        chosen = "NONE"
    return ContractVerdict(
        name="C2 Adaptive embedder functional",
        passed=passed,
        evidence=(
            f"path={chosen} fastembed_loaded={fastembed_ok} "
            f"fallback_activated={fallback}"
        ),
        details={
            "embedder_path": chosen,
            "fastembed_loaded": fastembed_ok,
            "fallback_activated": fallback,
        },
    )


def _eval_cluster_coverage(art: SessionArtifacts) -> ContractVerdict:
    emits = _RE_CLUSTER_COVERAGE_EMIT.findall(art.debug_log_text)
    passed = len(emits) > 0
    distinct_hashes = sorted({h for *_, h in emits})
    return ContractVerdict(
        name="C3 cluster_coverage envelopes fired",
        passed=passed,
        evidence=(
            f"emit_count={len(emits)} "
            f"distinct_clusters={len(distinct_hashes)}"
        ),
        details={
            "emit_count": len(emits),
            "distinct_cluster_hashes": distinct_hashes,
        },
    )


def _eval_domain_map(art: SessionArtifacts) -> ContractVerdict:
    n = len(art.domain_map_entries)
    passed = n > 0
    sample: List[str] = []
    for entry_path in art.domain_map_entries[:3]:
        try:
            doc = json.loads(entry_path.read_text(encoding="utf-8"))
            sample.append(
                f"{entry_path.stem}: theme={doc.get('theme_label', '?')!r}"
                f" files={len(doc.get('discovered_files', []))}"
            )
        except Exception:
            sample.append(f"{entry_path.stem}: <unparseable>")
    return ContractVerdict(
        name="C4 DomainMap cross-session populated",
        passed=passed,
        evidence=(
            f"persisted_entries={n} "
            f"sample={'; '.join(sample) if sample else 'none'}"
        ),
        details={
            "persisted_entries": n,
            "entry_files": [p.name for p in art.domain_map_entries],
        },
    )


def _eval_ratio_advisory(art: SessionArtifacts) -> ContractVerdict:
    """Advisory contract -- doc_staleness:exploration ratio.

    v3 baseline: ~30:3 (10:1 fixation). Improvement = lower ratio.
    """
    docstale = sum(
        1 for _ in _RE_DOC_STALENESS_EMIT.finditer(
            art.debug_log_text,
        )
    )
    cluster_emits = len(
        _RE_CLUSTER_COVERAGE_EMIT.findall(art.debug_log_text),
    )
    proactive_emits = len(re.findall(
        r"proactive_exploration", art.debug_log_text,
        flags=re.IGNORECASE,
    ))
    exploration_signal = max(cluster_emits, proactive_emits)
    if exploration_signal == 0:
        ratio_str = "inf"
        improved = False
    else:
        ratio = docstale / exploration_signal
        ratio_str = f"{ratio:.2f}"
        improved = ratio < 10.0
    return ContractVerdict(
        name="C5 doc_staleness:exploration ratio (advisory)",
        passed=improved,
        evidence=(
            f"doc_staleness={docstale} "
            f"exploration_signals={exploration_signal} "
            f"(cluster_coverage={cluster_emits}, "
            f"proactive_total={proactive_emits}) "
            f"ratio={ratio_str} v3_baseline=10.0"
        ),
        details={
            "doc_staleness_count": docstale,
            "exploration_signal_count": exploration_signal,
            "ratio_to_baseline": ratio_str,
        },
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Tuple[str, ...]) -> int:
    arg = argv[1] if len(argv) > 1 else None
    session_dir = _resolve_session(arg)
    if session_dir is None:
        print(
            "FATAL: no session found "
            f"(arg={arg!r}, dir={SESSIONS_DIR})",
            file=sys.stderr,
        )
        return 2
    art = _load_artifacts(session_dir)
    if art is None:
        print(
            f"FATAL: artifacts missing/unparseable "
            f"in {session_dir}",
            file=sys.stderr,
        )
        return 2
    print(f"Empirical-closure verdict for session: {art.session_id}")
    print(f"  session_dir : {art.session_dir}")
    print(f"  domain_map  : {DOMAIN_MAP_DIR} "
          f"({len(art.domain_map_entries)} entry/entries)")
    summary = art.summary
    duration = summary.get('duration_s', 0) or 0
    cost = summary.get('cost_total', 0) or 0
    outcome = summary.get('session_outcome') or summary.get('_status', '?')
    stop_reason = summary.get('stop_reason') or '?'
    try:
        duration_f = float(duration)
        cost_f = float(cost)
        print(f"  outcome     : {outcome} / {stop_reason} "
              f"after {duration_f:.0f}s cost=${cost_f:.4f}")
    except (TypeError, ValueError):
        print(f"  outcome     : {outcome} / {stop_reason}")
    print()
    primary_verdicts = [
        _eval_substrate(art),
        _eval_embedder(art),
        _eval_cluster_coverage(art),
        _eval_domain_map(art),
    ]
    advisory_verdict = _eval_ratio_advisory(art)
    for v in primary_verdicts:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    mark = "PASS" if advisory_verdict.passed else "INFO"
    print(f"  [{mark}] {advisory_verdict.name}")
    print(f"         {advisory_verdict.evidence}")
    print()
    all_primary_passed = all(v.passed for v in primary_verdicts)
    if all_primary_passed:
        print("VERDICT: ARC EMPIRICALLY CLOSED -- all four primary "
              "contracts PASSED.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- arc not "
          "yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main(tuple(sys.argv)))
