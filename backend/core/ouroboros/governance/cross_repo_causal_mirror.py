"""
Cross-Repo Causal Mirror
========================

Closes §40 Wave 5 #20 — TRIGGER-GATED experimental arc.
Per the operator binding:

  "When working across multiple repos, surface cross-repo file-
   touch correlations against local postmortems — identify
   causal mirrors where another repo's changes precede local
   failures."

This substrate is **trigger-gated** by design. It stays inert
(returns ``TRIGGER_NOT_MET``) unless EITHER:

* The current git repo has more than one configured remote
  (detected via ``git remote``), OR
* Operator explicitly sets
  ``JARVIS_CROSS_REPO_MIRROR_FORCE_TRIGGER=true``.

When active, the substrate scans the configured mirror repo
path (``JARVIS_CROSS_REPO_MIRROR_PATH``), walks recent commits,
extracts touched file paths, and intersects them against
recent local postmortem ``target_files`` to surface causal
correlations.

Composition contract:

* :func:`postmortem_recall.gather_recent_postmortems` —
  postmortem corpus (already canonical, used in Wave 3 #7).
* ``subprocess`` — git log walk on the mirror repo path. Uses
  stdlib only; no third-party git library. Bounded by
  ``max_commits``.
* :func:`governance_boundary_gate.is_boundary_crossed` — flag
  when mirror correlations touch the local governance cage.
* :func:`cross_process_jsonl.flock_append_line` — §33.4 audit
  at ``.jarvis/cross_repo_mirror_ledger.jsonl``.

NEVER raises. Mirror repo path missing / git command failure /
empty postmortem corpus all degrade to ``TRIGGER_NOT_MET`` or
``NO_MIRROR_DETECTED`` verdict, not exception.

Closed 4-value :class:`MirrorVerdict`:

  TRIGGER_NOT_MET     ⏸ single-remote repo + no force flag
  NO_MIRROR_DETECTED  ◦ trigger met but no correlations found
  MIRROR_FOUND        🪞 ≥1 causal correlation surfaced
  DISABLED            ◌ master flag off

Closed 4-value :class:`CausalSignal`:

  SHARED_PATH         common file path touched across repos
  COMMIT_CORRELATION  mirror commit timestamp precedes local
                      postmortem
  POSTMORTEM_OVERLAP  mirror touched files match postmortem
                      ``target_files`` (strongest signal)
  NONE                no correlation

§33.1 ``JARVIS_CROSS_REPO_MIRROR_ENABLED`` default-FALSE.

Authority asymmetry (AST-pinned): no orchestrator / iron_gate /
policy / providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


CROSS_REPO_MIRROR_SCHEMA_VERSION: str = "cross_repo_mirror.1"


_ENV_MASTER = "JARVIS_CROSS_REPO_MIRROR_ENABLED"
_ENV_PERSIST = "JARVIS_CROSS_REPO_MIRROR_PERSIST_ENABLED"
_ENV_FORCE_TRIGGER = "JARVIS_CROSS_REPO_MIRROR_FORCE_TRIGGER"
_ENV_MIRROR_PATH = "JARVIS_CROSS_REPO_MIRROR_PATH"
_ENV_MAX_COMMITS = "JARVIS_CROSS_REPO_MIRROR_MAX_COMMITS"
_ENV_MAX_POSTMORTEMS = "JARVIS_CROSS_REPO_MIRROR_MAX_POSTMORTEMS"
_ENV_GIT_TIMEOUT_S = "JARVIS_CROSS_REPO_MIRROR_GIT_TIMEOUT_S"
_ENV_LEDGER_PATH = "JARVIS_CROSS_REPO_MIRROR_LEDGER_PATH"

_DEFAULT_MAX_COMMITS = 50
_DEFAULT_MAX_POSTMORTEMS = 30
_DEFAULT_GIT_TIMEOUT_S = 10

_DEFAULT_LEDGER_REL = ".jarvis/cross_repo_mirror_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def force_trigger_enabled() -> bool:
    """Operator override — when TRUE the substrate proceeds
    even if no multi-remote is detected. For testing or
    explicit multi-repo workflows."""
    return _flag(_ENV_FORCE_TRIGGER, default=False)


def mirror_path() -> Optional[Path]:
    """Operator-configured mirror repo path. None when unset."""
    raw = os.environ.get(_ENV_MIRROR_PATH, "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_commits_to_scan() -> int:
    return _read_clamped_int(
        _ENV_MAX_COMMITS, _DEFAULT_MAX_COMMITS, 1, 10_000,
    )


def max_postmortems_to_scan() -> int:
    return _read_clamped_int(
        _ENV_MAX_POSTMORTEMS, _DEFAULT_MAX_POSTMORTEMS, 1, 10_000,
    )


def git_timeout_s() -> int:
    return _read_clamped_int(
        _ENV_GIT_TIMEOUT_S, _DEFAULT_GIT_TIMEOUT_S, 1, 300,
    )


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class MirrorVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    TRIGGER_NOT_MET = "trigger_not_met"
    NO_MIRROR_DETECTED = "no_mirror_detected"
    MIRROR_FOUND = "mirror_found"
    DISABLED = "disabled"


class CausalSignal(str, enum.Enum):
    """Closed 4-value signal — bytes-pinned via AST."""

    SHARED_PATH = "shared_path"
    COMMIT_CORRELATION = "commit_correlation"
    POSTMORTEM_OVERLAP = "postmortem_overlap"
    NONE = "none"


_VERDICT_GLYPH: Dict[str, str] = {
    MirrorVerdict.TRIGGER_NOT_MET.value: "⏸",
    MirrorVerdict.NO_MIRROR_DETECTED.value: "◦",
    MirrorVerdict.MIRROR_FOUND.value: "🪞",
    MirrorVerdict.DISABLED.value: "◌",
}


_SIGNAL_GLYPH: Dict[str, str] = {
    CausalSignal.SHARED_PATH.value: "🔗",
    CausalSignal.COMMIT_CORRELATION.value: "🕓",
    CausalSignal.POSTMORTEM_OVERLAP.value: "💥",
    CausalSignal.NONE.value: "·",
}


def verdict_glyph(verdict: object) -> str:
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def signal_glyph(signal: object) -> str:
    try:
        if hasattr(signal, "value"):
            return _SIGNAL_GLYPH.get(str(signal.value), "?")
        return _SIGNAL_GLYPH.get(
            str(signal or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class MirrorCorrelation:
    """One mirror×local correlation."""

    mirror_commit_sha: str
    mirror_commit_subject: str
    mirror_files: Tuple[str, ...]
    overlapping_postmortem_op_ids: Tuple[str, ...]
    overlapping_files: Tuple[str, ...]
    dominant_signal: CausalSignal
    boundary_crossed: bool
    schema_version: str = CROSS_REPO_MIRROR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mirror_commit_sha": self.mirror_commit_sha[:40],
            "mirror_commit_subject": (
                self.mirror_commit_subject[:256]
            ),
            "mirror_files": list(self.mirror_files),
            "overlapping_postmortem_op_ids": list(
                self.overlapping_postmortem_op_ids,
            ),
            "overlapping_files": list(self.overlapping_files),
            "dominant_signal": self.dominant_signal.value,
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CrossRepoMirrorReport:
    """Aggregate mirror scan report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: MirrorVerdict
    trigger_met: bool
    mirror_path: str
    mirror_commits_scanned: int
    postmortems_scanned: int
    correlations: Tuple[MirrorCorrelation, ...]
    diagnostic: str
    elapsed_s: float
    schema_version: str = CROSS_REPO_MIRROR_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "trigger_met": bool(self.trigger_met),
            "mirror_path": self.mirror_path[:256],
            "mirror_commits_scanned": int(
                self.mirror_commits_scanned,
            ),
            "postmortems_scanned": int(self.postmortems_scanned),
            "correlations": [
                c.to_dict() for c in self.correlations
            ],
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers


def _detect_multi_remote_trigger() -> bool:
    """Returns True iff the current git repo has > 1 remote.
    NEVER raises."""
    if force_trigger_enabled():
        return True
    try:
        result = subprocess.run(
            ["git", "remote"],
            capture_output=True,
            text=True,
            timeout=git_timeout_s(),
            check=False,
        )
        if result.returncode != 0:
            return False
        remotes = [
            line.strip()
            for line in (result.stdout or "").splitlines()
            if line.strip()
        ]
        return len(remotes) > 1
    except Exception:  # noqa: BLE001
        return False


def _walk_mirror_commits(
    repo_path: Path,
) -> Tuple[Tuple[str, str, Tuple[str, ...]], ...]:
    """Walk git log of mirror repo. Returns tuple of
    (sha, subject, files). NEVER raises."""
    try:
        if not repo_path.exists() or not repo_path.is_dir():
            return ()
        cap = max_commits_to_scan()
        result = subprocess.run(
            [
                "git", "log",
                f"-n{cap}",
                "--name-only",
                "--pretty=format:%H%n%s",
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=git_timeout_s(),
            check=False,
        )
        if result.returncode != 0:
            return ()
        out: List[Tuple[str, str, Tuple[str, ...]]] = []
        # Parse format: SHA\nSUBJECT\nfile1\nfile2\n\n
        lines = (result.stdout or "").splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue
            # SHA line
            sha = line
            i += 1
            if i >= len(lines):
                break
            subject = lines[i].strip()
            i += 1
            files: List[str] = []
            while i < len(lines) and lines[i].strip():
                files.append(lines[i].strip())
                i += 1
            out.append((sha, subject, tuple(files)))
        return tuple(out[:cap])
    except Exception:  # noqa: BLE001
        return ()


def _load_local_postmortems() -> Tuple[Any, ...]:
    """Compose canonical postmortem_recall. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.postmortem_recall import (  # noqa: E501
            gather_recent_postmortems,
        )
        return tuple(
            gather_recent_postmortems(
                max_total=max_postmortems_to_scan(),
            ),
        )
    except Exception:  # noqa: BLE001
        return ()


def _is_boundary_crossed(files: Sequence[str]) -> bool:
    if not files:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(files))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _build_correlations(
    commits: Sequence[Tuple[str, str, Tuple[str, ...]]],
    postmortems: Sequence[Any],
) -> Tuple[MirrorCorrelation, ...]:
    """Pure intersection. NEVER raises."""
    out: List[MirrorCorrelation] = []
    # Build postmortem index: file → set of op_ids
    pm_index: Dict[str, List[str]] = {}
    for pm in postmortems:
        try:
            op_id = str(getattr(pm, "op_id", "") or "")
            tfiles = tuple(
                str(f or "").strip()
                for f in getattr(pm, "target_files", ()) or ()
                if f
            )
            for f in tfiles:
                pm_index.setdefault(f, []).append(op_id)
        except Exception:  # noqa: BLE001
            continue
    for sha, subject, files in commits:
        try:
            overlap = sorted(
                set(files) & set(pm_index.keys()),
            )
            if not overlap:
                continue
            overlapping_op_ids = sorted({
                op_id
                for f in overlap
                for op_id in pm_index.get(f, ())
                if op_id
            })
            boundary = _is_boundary_crossed(overlap)
            out.append(MirrorCorrelation(
                mirror_commit_sha=sha,
                mirror_commit_subject=subject,
                mirror_files=files,
                overlapping_postmortem_op_ids=tuple(
                    overlapping_op_ids,
                ),
                overlapping_files=tuple(overlap),
                dominant_signal=CausalSignal.POSTMORTEM_OVERLAP,
                boundary_crossed=boundary,
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def scan_mirror_correlations(
    *,
    mirror_commits_override: Optional[
        Sequence[Tuple[str, str, Tuple[str, ...]]]
    ] = None,
    postmortems_override: Optional[Sequence[Any]] = None,
    trigger_override: Optional[bool] = None,
    now_unix: Optional[float] = None,
) -> CrossRepoMirrorReport:
    """Top-level scanner. NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)

    if not master_enabled():
        return CrossRepoMirrorReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=MirrorVerdict.DISABLED,
            trigger_met=False,
            mirror_path="",
            mirror_commits_scanned=0,
            postmortems_scanned=0,
            correlations=(),
            diagnostic=(
                f"gate disabled via {_ENV_MASTER}=false"
            ),
            elapsed_s=0.0,
        )

    trigger = (
        trigger_override
        if trigger_override is not None
        else _detect_multi_remote_trigger()
    )
    if not trigger:
        return CrossRepoMirrorReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=MirrorVerdict.TRIGGER_NOT_MET,
            trigger_met=False,
            mirror_path="",
            mirror_commits_scanned=0,
            postmortems_scanned=0,
            correlations=(),
            diagnostic=(
                "single-remote repo + no force flag — "
                f"set {_ENV_FORCE_TRIGGER}=true to override"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    mpath = mirror_path()
    if mirror_commits_override is None and mpath is None:
        return CrossRepoMirrorReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=MirrorVerdict.NO_MIRROR_DETECTED,
            trigger_met=True,
            mirror_path="",
            mirror_commits_scanned=0,
            postmortems_scanned=0,
            correlations=(),
            diagnostic=(
                f"trigger met but {_ENV_MIRROR_PATH} unset — "
                "no mirror to scan"
            ),
            elapsed_s=max(0.0, time.time() - started),
        )

    commits = (
        mirror_commits_override
        if mirror_commits_override is not None
        else _walk_mirror_commits(mpath)
    )
    postmortems = (
        postmortems_override
        if postmortems_override is not None
        else _load_local_postmortems()
    )

    correlations = _build_correlations(commits, postmortems)

    if correlations:
        verdict = MirrorVerdict.MIRROR_FOUND
        diagnostic = (
            f"{len(correlations)} correlation(s) surfaced "
            f"across {len(commits)} mirror commit(s) and "
            f"{len(postmortems)} local postmortem(s)"
        )
    else:
        verdict = MirrorVerdict.NO_MIRROR_DETECTED
        diagnostic = (
            f"no overlap found across {len(commits)} mirror "
            f"commit(s) and {len(postmortems)} local "
            "postmortem(s)"
        )

    report = CrossRepoMirrorReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        trigger_met=True,
        mirror_path=str(mpath or ""),
        mirror_commits_scanned=len(commits),
        postmortems_scanned=len(postmortems),
        correlations=correlations,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: CrossRepoMirrorReport) -> None:
    """Best-effort §33.4 write. NEVER raises. Skips when no
    correlations."""
    if report.verdict is not MirrorVerdict.MIRROR_FOUND:
        return
    _flock_append({"kind": "mirror_report", "payload": report.to_dict()})


def _publish_event(report: CrossRepoMirrorReport) -> None:
    """Best-effort SSE publish. NEVER raises."""
    if not master_enabled():
        return
    if report.verdict is not MirrorVerdict.MIRROR_FOUND:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_CROSS_REPO_MIRROR_FOUND,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_CROSS_REPO_MIRROR_FOUND,
            (
                f"system::cross_repo_mirror::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "correlation_count": len(report.correlations),
                "mirror_commits_scanned": (
                    report.mirror_commits_scanned
                ),
                "postmortems_scanned": (
                    report.postmortems_scanned
                ),
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_mirror_panel(
    report: Optional[CrossRepoMirrorReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"cross-repo mirror: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "cross-repo mirror: no report"
    if not report.master_enabled:
        return (
            f"cross-repo mirror: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    lines = [
        f"🪞 Cross-Repo Causal Mirror  {vg} "
        f"{report.verdict.value}",
        f"  trigger_met         : {report.trigger_met}",
        f"  mirror_path         : {report.mirror_path[:60] or 'n/a'}",
        f"  commits_scanned     : {report.mirror_commits_scanned}",
        f"  postmortems_scanned : {report.postmortems_scanned}",
        f"  correlations        : {len(report.correlations)}",
    ]
    if report.correlations:
        for c in report.correlations[:5]:
            sg = signal_glyph(c.dominant_signal)
            lines.append(
                f"    {sg} {c.mirror_commit_sha[:10]} "
                f"files={len(c.overlapping_files)} "
                f"postmortems={len(c.overlapping_postmortem_op_ids)}"
            )
        if len(report.correlations) > 5:
            lines.append(
                f"    ... (+{len(report.correlations) - 5} more)"
            )
    lines.append(f"  diagnostic          : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "cross_repo_causal_mirror.py"
    )

    _EXPECTED_VERDICTS = {
        "trigger_not_met", "no_mirror_detected",
        "mirror_found", "disabled",
    }
    _EXPECTED_SIGNALS = {
        "shared_path", "commit_correlation",
        "postmortem_overlap", "none",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "MirrorVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    return (
                        f"MirrorVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"MirrorVerdict drift: {sorted(extra)}",
                    )
                return ()
        return ("MirrorVerdict class not found",)

    def _validate_signal_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CausalSignal"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                missing = _EXPECTED_SIGNALS - found
                extra = found - _EXPECTED_SIGNALS
                if missing:
                    return (
                        f"CausalSignal missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"CausalSignal drift: {sorted(extra)}",
                    )
                return ()
        return ("CausalSignal class not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
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
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "postmortem_recall" not in source:
            violations.append(
                "must compose postmortem_recall (Wave 3 #7 "
                "sibling source)",
            )
        if "cross_process_jsonl" not in source:
            violations.append("must compose cross_process_jsonl")
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 governance_boundary_gate",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cross_repo_mirror_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MirrorVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_repo_mirror_signal_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CausalSignal 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_signal_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_repo_mirror_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity. MUST NOT import "
                "orchestrator / iron_gate / etc."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_repo_mirror_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cross_repo_mirror_composes_canonical"
            ),
            target_file=target,
            description=(
                "Composes postmortem_recall + "
                "governance_boundary_gate + "
                "cross_process_jsonl."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "cross_repo_causal_mirror.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Cross-repo causal mirror master. §33.1 "
                "default-FALSE. Closes §40 Wave 5 #20 "
                "TRIGGER-GATED. Substrate stays inert "
                "(TRIGGER_NOT_MET) unless multi-remote repo "
                "OR force-trigger flag is set."
            ),
            category=Category.EXPERIMENTAL,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — gate §33.4 writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_FORCE_TRIGGER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Operator override — proceed even without "
                "multi-remote detection. For testing or "
                "explicit multi-repo workflows."
            ),
            category=Category.EXPERIMENTAL,
            source_file=src,
            example=f"{_ENV_FORCE_TRIGGER}=true",
        ),
        FlagSpec(
            name=_ENV_MIRROR_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Path to the mirror repo to scan. Substrate "
                "returns NO_MIRROR_DETECTED if unset."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MIRROR_PATH}=/path/to/mirror/repo",
        ),
        FlagSpec(
            name=_ENV_MAX_COMMITS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_COMMITS,
            description=(
                "Cap on mirror repo commits scanned. "
                "Default 50."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_COMMITS}=200",
        ),
        FlagSpec(
            name=_ENV_MAX_POSTMORTEMS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_POSTMORTEMS,
            description=(
                "Cap on local postmortems scanned. Default 30."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_POSTMORTEMS}=100",
        ),
        FlagSpec(
            name=_ENV_GIT_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_GIT_TIMEOUT_S,
            description=(
                "Timeout (s) for git subprocess calls. "
                "Default 10."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_GIT_TIMEOUT_S}=30",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "CROSS_REPO_MIRROR_SCHEMA_VERSION",
    "MirrorVerdict",
    "CausalSignal",
    "MirrorCorrelation",
    "CrossRepoMirrorReport",
    "master_enabled",
    "persistence_enabled",
    "force_trigger_enabled",
    "mirror_path",
    "max_commits_to_scan",
    "max_postmortems_to_scan",
    "git_timeout_s",
    "ledger_path",
    "verdict_glyph",
    "signal_glyph",
    "scan_mirror_correlations",
    "format_mirror_panel",
    "register_shipped_invariants",
    "register_flags",
]
