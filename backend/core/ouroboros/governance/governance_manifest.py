"""
Governance Manifest — Hash-Cap on Self-Modification
====================================================

Closes §40 Wave 2 #6 — the second Wave 2 recursion-bounding arc.
Cryptographic manifest signature gate: the operator's last-
approved governance/ state is recorded as a SHA-256-per-file
manifest; any autonomous commit touching governance/ is verified
against the operator-signed baseline; drift refuses the commit.

Composition pattern
-------------------
This substrate is a **pure-function hash-cap** layered on top of
the Wave 2 #5 boundary gate:

* :mod:`governance_boundary_gate` (Wave 2 #5) — detects ops
  touching governance/ via path predicate.
* This module (Wave 2 #6) — verifies the *content* of those
  ops against the operator-signed manifest.
* AutoCommitter (Wave 2 #6 integration) — refuses commits whose
  governance/ files drift from the manifest baseline.

Together: the operator authorizes a known governance state by
signing the manifest; autonomous ops touching that state are
either routed to APPROVAL_REQUIRED (boundary gate) AND/OR refused
at commit-time (hash-cap). The hash-cap closes the residual gap
where operator-approval might be implicitly trusted at the PLAN
phase but the actual on-disk content has been silently modified.

Operator workflow
-----------------
1. Run :func:`refresh_signed_manifest` to baseline current state
   (writes ``.jarvis/governance_manifest.json``).
2. Set ``JARVIS_GOVERNANCE_MANIFEST_ENABLED=true``.
3. Future autonomous commits touching governance/ are verified.
4. After operator-approved governance change lands, re-run
   :func:`refresh_signed_manifest` to update the baseline.

§33.1 master-flag discipline
----------------------------
``JARVIS_GOVERNANCE_MANIFEST_ENABLED`` default-**FALSE** because
the substrate REQUIRES the operator to establish a baseline
first; enforcing without a manifest would either fail closed on
every commit (bad UX) or fail open (no protection). Defaulting
FALSE makes the opt-in explicit. MISSING_MANIFEST verdict is
treated as a gate-skipped condition, not a refusal — defensive.

Authority asymmetry (AST-pinned)
--------------------------------
Substrate imports stdlib + governance_boundary_gate (canonical
boundary predicate composition) ONLY. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer. The AutoCommitter integration
happens at the *consumer* side: AutoCommitter lazy-imports this
module's verifier.

Pure substrate — NEVER raises. A malformed manifest, missing
file, hashing failure, or env-lookup failure degrades to
``MISSING_MANIFEST`` or ``DISABLED``, not exception.
"""
from __future__ import annotations

import ast
import enum
import hashlib
import json
import logging
import os
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


GOVERNANCE_MANIFEST_SCHEMA_VERSION: str = "governance_manifest.1"


# ===========================================================================
# Env knobs
# ===========================================================================


_ENV_MASTER = "JARVIS_GOVERNANCE_MANIFEST_ENABLED"
_ENV_MANIFEST_PATH = "JARVIS_GOVERNANCE_MANIFEST_PATH"
_ENV_MAX_FILES_SCAN = "JARVIS_GOVERNANCE_MANIFEST_MAX_FILES"

_DEFAULT_MANIFEST_RELATIVE = ".jarvis/governance_manifest.json"
_DEFAULT_MAX_FILES = 5000
_MIN_MAX_FILES = 10
_MAX_MAX_FILES = 100_000

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 opt-in safety gate — default-**FALSE**.

    The substrate REQUIRES the operator to baseline first; until
    a manifest exists at the canonical path, the gate is dormant
    even if this flag is on (MISSING_MANIFEST verdict is gate-
    skipped, not a refusal). Defaulting FALSE makes opt-in
    explicit.
    """
    return _flag(_ENV_MASTER, default=False)


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


def max_files_scan() -> int:
    """Defensive ceiling on the number of governance/ files
    hashed per verification. Clamped to [10, 100_000]."""
    return _read_clamped_int(
        _ENV_MAX_FILES_SCAN,
        _DEFAULT_MAX_FILES,
        _MIN_MAX_FILES,
        _MAX_MAX_FILES,
    )


# ===========================================================================
# Repo + governance directory resolution (composes Wave 2 #5)
# ===========================================================================


def _resolve_repo_root() -> Optional[Path]:
    """Walk up from this module to find the .git anchor.
    NEVER raises."""
    try:
        here = Path(__file__).resolve()
        for ancestor in (here, *here.parents):
            try:
                if (ancestor / ".git").exists():
                    return ancestor
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def manifest_path() -> Path:
    """Canonical manifest location. Operator override via
    ``JARVIS_GOVERNANCE_MANIFEST_PATH``. NEVER raises — falls
    back to the repo-relative default."""
    raw = os.environ.get(_ENV_MANIFEST_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001
            pass
    root = _resolve_repo_root()
    if root is None:
        return Path(_DEFAULT_MANIFEST_RELATIVE)
    return root / _DEFAULT_MANIFEST_RELATIVE


def _governance_dir() -> Optional[Path]:
    """Compose the canonical governance directory path via the
    Wave 2 #5 boundary gate (single source of truth for the
    structural location)."""
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            canonical_governance_prefix,
        )
        prefix = canonical_governance_prefix()
    except Exception:  # noqa: BLE001
        prefix = "backend/core/ouroboros/governance/"
    root = _resolve_repo_root()
    if root is None:
        return None
    return root / prefix.rstrip("/")


# ===========================================================================
# Closed 5-value verdict taxonomy
# ===========================================================================


class ManifestVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned via AST.

    * ``MATCH`` — every governance/ file hash matches the signed
      manifest. Commit may proceed normally.
    * ``DRIFT`` — ≥1 file's current hash differs from the signed
      manifest entry. AutoCommitter refuses the commit.
    * ``MISSING_MANIFEST`` — no operator-signed manifest exists
      at the canonical path. Gate is **skipped** (not refused);
      operator workflow says "baseline first, then enable".
    * ``EMPTY_GOVERNANCE`` — the governance directory could not
      be located or is empty. Defensive — gate skipped.
    * ``DISABLED`` — master flag off. Gate skipped.
    """

    MATCH = "match"
    DRIFT = "drift"
    MISSING_MANIFEST = "missing_manifest"
    EMPTY_GOVERNANCE = "empty_governance"
    DISABLED = "disabled"


def is_refusal_verdict(verdict: object) -> bool:
    """Return True iff the verdict represents a commit refusal.

    Currently only ``DRIFT`` refuses; the other four are
    skipped/passes. Centralized so consumer-side integration
    composes a single predicate (no parallel string comparison).
    """
    try:
        if hasattr(verdict, "value"):
            return verdict.value == ManifestVerdict.DRIFT.value
        return str(verdict).strip().lower() == ManifestVerdict.DRIFT.value
    except Exception:  # noqa: BLE001
        return False


# ===========================================================================
# §33.5 frozen versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class FileSignature:
    """One governance file's hash + size, frozen."""

    relative_path: str
    sha256: str           # 64-char hex
    size_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "size_bytes": int(self.size_bytes),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Optional["FileSignature"]:
        try:
            rp = str(raw.get("relative_path", "")).strip()
            sha = str(raw.get("sha256", "")).strip().lower()
            size = int(raw.get("size_bytes", 0))
            if not rp or len(sha) != 64:
                return None
            return cls(
                relative_path=rp, sha256=sha, size_bytes=size,
            )
        except Exception:  # noqa: BLE001
            return None


@dataclass(frozen=True)
class ManifestSnapshot:
    """Operator-signed governance baseline. Persisted to
    ``.jarvis/governance_manifest.json``."""

    schema_version: str
    operator_label: str
    """Free-form label the operator sets when refreshing — e.g.,
    'pre-M10-flip' or 'manual-review-2026-05-10'. Operator-
    facing audit string."""
    signed_at_unix: float
    signatures: Tuple[FileSignature, ...]
    manifest_sha256: str
    """Aggregate sha256 of the sorted (path, sha256) tuples.
    Single-string operator signature — drift in any file
    changes this value."""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "operator_label": self.operator_label,
            "signed_at_unix": float(self.signed_at_unix),
            "signatures": [s.to_dict() for s in self.signatures],
            "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any],
    ) -> Optional["ManifestSnapshot"]:
        """Defensive parser. Returns None on any structural
        violation. NEVER raises."""
        try:
            sigs_raw = raw.get("signatures", []) or []
            sigs: List[FileSignature] = []
            for s in sigs_raw:
                if isinstance(s, Mapping):
                    parsed = FileSignature.from_dict(s)
                    if parsed is not None:
                        sigs.append(parsed)
            return cls(
                schema_version=str(raw.get(
                    "schema_version",
                    GOVERNANCE_MANIFEST_SCHEMA_VERSION,
                )),
                operator_label=str(raw.get("operator_label", "")),
                signed_at_unix=float(raw.get("signed_at_unix", 0.0)),
                signatures=tuple(sigs),
                manifest_sha256=str(
                    raw.get("manifest_sha256", ""),
                ).strip().lower(),
            )
        except Exception:  # noqa: BLE001
            return None

    def lookup(self, relative_path: str) -> Optional[FileSignature]:
        """Find a signature by relative path. NEVER raises."""
        if not relative_path:
            return None
        for s in self.signatures:
            if s.relative_path == relative_path:
                return s
        return None


@dataclass(frozen=True)
class ManifestComparison:
    """Result of comparing current state against signed manifest."""

    schema_version: str
    verdict: ManifestVerdict
    current_file_count: int
    signed_file_count: int
    drifted_paths: Tuple[str, ...]
    """Bounded at 32 entries. Non-empty only when verdict is
    DRIFT."""
    added_paths: Tuple[str, ...]
    """Files present in current but not signed (also DRIFT).
    Bounded at 32."""
    removed_paths: Tuple[str, ...]
    """Files signed but missing in current (also DRIFT). Bounded
    at 32."""
    manifest_path_str: str
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "current_file_count": int(self.current_file_count),
            "signed_file_count": int(self.signed_file_count),
            "drifted_paths": list(self.drifted_paths),
            "added_paths": list(self.added_paths),
            "removed_paths": list(self.removed_paths),
            "manifest_path_str": self.manifest_path_str,
            "detail": self.detail[:512],
        }


# ===========================================================================
# Pure functions — hash, walk, compare
# ===========================================================================


_DRIFT_PATHS_BOUND = 32
_HASH_BUFFER_SIZE = 64 * 1024  # 64 KiB
_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MiB per file (defensive)


def _hash_file(path: Path) -> Optional[str]:
    """Streaming SHA-256 of one file. Returns 64-char lowercase
    hex or None on failure. NEVER raises."""
    try:
        st = path.stat()
        if not path.is_file():
            return None
        if st.st_size > _MAX_FILE_BYTES:
            # Pathological file — operator's manifest should not
            # cover this. Skip; comparison treats as drift if it
            # appears in either side.
            return None
        h = hashlib.sha256()
        with path.open("rb") as f:
            while True:
                chunk = f.read(_HASH_BUFFER_SIZE)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _compute_manifest_signature(
    signatures: Sequence[FileSignature],
) -> str:
    """Compute the aggregate manifest signature.

    Deterministic: sorts signatures by relative_path, joins each
    ``path:sha256`` pair, then hashes. Drift in any file's hash
    OR the file set itself changes the aggregate signature.
    """
    if not signatures:
        return hashlib.sha256(b"").hexdigest()
    sorted_sigs = sorted(signatures, key=lambda s: s.relative_path)
    joined = "\n".join(
        f"{s.relative_path}:{s.sha256}" for s in sorted_sigs
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_current_signatures(
    governance_dir: Optional[Path] = None,
    *,
    max_files: Optional[int] = None,
) -> Tuple[FileSignature, ...]:
    """Walk the governance directory and hash every .py file.

    Pure function. NEVER raises. Returns an empty tuple when the
    directory is missing / unreadable / over the file-count cap.
    """
    if governance_dir is None:
        governance_dir = _governance_dir()
    if governance_dir is None or not governance_dir.exists():
        return ()
    cap = max_files if max_files is not None else max_files_scan()
    root = _resolve_repo_root()
    if root is None:
        return ()
    sigs: List[FileSignature] = []
    try:
        # Sorted for determinism.
        for path in sorted(governance_dir.rglob("*.py")):
            if len(sigs) >= cap:
                break
            try:
                if not path.is_file():
                    continue
                # Skip __pycache__ + tests/ entries inside governance
                # (tests live OUTSIDE governance/ in this repo, but
                # defensive in case a sub-tests/ ever appears).
                parts = path.parts
                if any(
                    p in ("__pycache__", ".git") for p in parts
                ):
                    continue
                sha = _hash_file(path)
                if sha is None:
                    continue
                rel = str(path.relative_to(root)).replace(
                    os.sep, "/",
                )
                size = path.stat().st_size
                sigs.append(FileSignature(
                    relative_path=rel,
                    sha256=sha,
                    size_bytes=size,
                ))
            except Exception:  # noqa: BLE001 — defensive per-file
                continue
    except Exception:  # noqa: BLE001
        return ()
    return tuple(sigs)


def compute_current_manifest(
    operator_label: str = "ephemeral",
    *,
    governance_dir: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> ManifestSnapshot:
    """Compute a current-state snapshot WITHOUT writing it.

    Pure-function — useful for comparison or for tests. The
    ``operator_label`` is informational; use
    :func:`refresh_signed_manifest` to actually persist with an
    operator-meaningful label.
    """
    sigs = compute_current_signatures(governance_dir=governance_dir)
    aggregate = _compute_manifest_signature(sigs)
    return ManifestSnapshot(
        schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
        operator_label=operator_label,
        signed_at_unix=(
            now_unix if now_unix is not None else time.time()
        ),
        signatures=sigs,
        manifest_sha256=aggregate,
    )


def load_signed_manifest(
    path: Optional[Path] = None,
) -> Optional[ManifestSnapshot]:
    """Read the operator-signed manifest. Returns None if
    missing, malformed, or unreadable. NEVER raises."""
    target = path if path is not None else manifest_path()
    try:
        if not target.exists():
            return None
        raw_text = target.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        if not isinstance(raw, dict):
            return None
        return ManifestSnapshot.from_dict(raw)
    except Exception:  # noqa: BLE001
        return None


def compare_manifests(
    current: ManifestSnapshot,
    signed: Optional[ManifestSnapshot],
    *,
    target_files: Optional[Sequence[str]] = None,
) -> ManifestComparison:
    """Pure comparison. NEVER raises.

    When ``target_files`` is supplied, the comparison restricts
    drift detection to those paths — useful for the AutoCommitter
    integration which only cares about whether the FILES IN THIS
    COMMIT have drifted. When omitted, every file in the current
    snapshot is compared.
    """
    manifest_path_str = str(manifest_path())

    if signed is None:
        return ManifestComparison(
            schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            verdict=ManifestVerdict.MISSING_MANIFEST,
            current_file_count=len(current.signatures),
            signed_file_count=0,
            drifted_paths=(),
            added_paths=(),
            removed_paths=(),
            manifest_path_str=manifest_path_str,
            detail=(
                f"no signed manifest at {manifest_path_str} — "
                "run refresh_signed_manifest() to baseline"
            ),
        )

    if not current.signatures:
        return ManifestComparison(
            schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            verdict=ManifestVerdict.EMPTY_GOVERNANCE,
            current_file_count=0,
            signed_file_count=len(signed.signatures),
            drifted_paths=(),
            added_paths=(),
            removed_paths=(),
            manifest_path_str=manifest_path_str,
            detail="governance directory empty or unreadable",
        )

    current_by_path: Dict[str, FileSignature] = {
        s.relative_path: s for s in current.signatures
    }
    signed_by_path: Dict[str, FileSignature] = {
        s.relative_path: s for s in signed.signatures
    }

    # Filter to target_files when supplied — normalize via the
    # canonical boundary gate's path normalizer so absolute paths
    # and OS separators map the same way as the manifest's
    # repo-relative strings.
    filter_set: Optional[set] = None
    if target_files:
        try:
            from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
                _normalize_path,
            )
            filter_set = set()
            for raw in target_files:
                normalized = _normalize_path(raw)
                if normalized:
                    filter_set.add(normalized)
        except Exception:  # noqa: BLE001
            filter_set = None

    drifted: List[str] = []
    added: List[str] = []
    removed: List[str] = []

    # current ∩ signed → check hash drift
    for path, cur in current_by_path.items():
        if filter_set is not None and path not in filter_set:
            continue
        signed_sig = signed_by_path.get(path)
        if signed_sig is None:
            if len(added) < _DRIFT_PATHS_BOUND:
                added.append(path)
        elif signed_sig.sha256 != cur.sha256:
            if len(drifted) < _DRIFT_PATHS_BOUND:
                drifted.append(path)

    # signed ∖ current → removed files
    for path in signed_by_path:
        if filter_set is not None and path not in filter_set:
            continue
        if path not in current_by_path:
            if len(removed) < _DRIFT_PATHS_BOUND:
                removed.append(path)

    if drifted or added or removed:
        return ManifestComparison(
            schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            verdict=ManifestVerdict.DRIFT,
            current_file_count=len(current.signatures),
            signed_file_count=len(signed.signatures),
            drifted_paths=tuple(drifted),
            added_paths=tuple(added),
            removed_paths=tuple(removed),
            manifest_path_str=manifest_path_str,
            detail=(
                f"drift: {len(drifted)} modified, "
                f"{len(added)} added, {len(removed)} removed; "
                f"commit refused — operator review required"
            ),
        )

    return ManifestComparison(
        schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
        verdict=ManifestVerdict.MATCH,
        current_file_count=len(current.signatures),
        signed_file_count=len(signed.signatures),
        drifted_paths=(),
        added_paths=(),
        removed_paths=(),
        manifest_path_str=manifest_path_str,
        detail=(
            f"governance state matches operator-signed "
            f"manifest ({len(current.signatures)} files)"
        ),
    )


# ===========================================================================
# End-to-end verifier (AutoCommitter composes this)
# ===========================================================================


def verify_governance_state(
    target_files: Optional[Sequence[str]] = None,
) -> ManifestComparison:
    """Top-level end-to-end verifier. NEVER raises.

    Composes current snapshot + signed manifest + comparison.
    When master flag is off, returns ``DISABLED`` verdict
    immediately (gate skipped).

    ``target_files`` restricts the comparison to the files
    actually changing in the calling commit — the AutoCommitter
    integration passes its `target_files` argument so unrelated
    governance/ files don't trigger drift on every commit.
    """
    if not master_enabled():
        return ManifestComparison(
            schema_version=GOVERNANCE_MANIFEST_SCHEMA_VERSION,
            verdict=ManifestVerdict.DISABLED,
            current_file_count=0,
            signed_file_count=0,
            drifted_paths=(),
            added_paths=(),
            removed_paths=(),
            manifest_path_str=str(manifest_path()),
            detail=(
                f"gate disabled via {_ENV_MASTER}=false — "
                "operator opt-in workflow"
            ),
        )
    current = compute_current_manifest()
    signed = load_signed_manifest()
    return compare_manifests(
        current, signed, target_files=target_files,
    )


# ===========================================================================
# Operator-facing write path (CLI / interactive use only)
# ===========================================================================


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of an operator-driven manifest refresh."""

    ok: bool
    manifest_path_str: str
    file_count: int
    manifest_sha256: str
    error: str = ""


def refresh_signed_manifest(
    operator_label: str,
    *,
    path: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> RefreshOutcome:
    """Write a fresh manifest from the current on-disk state.

    Operator-only entry point. Composes
    :func:`compute_current_manifest` then atomically writes to
    the canonical path (tmp + rename) for cross-process safety.

    NEVER raises — returns ``RefreshOutcome(ok=False, error=...)``
    on any failure.
    """
    if not operator_label or not operator_label.strip():
        return RefreshOutcome(
            ok=False,
            manifest_path_str=str(path or manifest_path()),
            file_count=0,
            manifest_sha256="",
            error=(
                "operator_label required — supply a meaningful "
                "audit string (e.g., 'pre-M10-flip')"
            ),
        )
    target = path if path is not None else manifest_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return RefreshOutcome(
            ok=False,
            manifest_path_str=str(target),
            file_count=0,
            manifest_sha256="",
            error=f"mkdir failed: {type(exc).__name__}",
        )

    snapshot = compute_current_manifest(
        operator_label=operator_label.strip(),
        now_unix=now_unix,
    )

    if not snapshot.signatures:
        return RefreshOutcome(
            ok=False,
            manifest_path_str=str(target),
            file_count=0,
            manifest_sha256="",
            error=(
                "governance directory empty or unreadable — "
                "cannot baseline an empty manifest"
            ),
        )

    try:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(snapshot.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(tmp), str(target))
    except Exception as exc:  # noqa: BLE001
        return RefreshOutcome(
            ok=False,
            manifest_path_str=str(target),
            file_count=len(snapshot.signatures),
            manifest_sha256=snapshot.manifest_sha256,
            error=f"write failed: {type(exc).__name__}",
        )
    return RefreshOutcome(
        ok=True,
        manifest_path_str=str(target),
        file_count=len(snapshot.signatures),
        manifest_sha256=snapshot.manifest_sha256,
    )


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    """Auto-discovered AST invariant pins."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "governance_manifest.py"
    )

    _EXPECTED_VERDICTS = {
        "match",
        "drift",
        "missing_manifest",
        "empty_governance",
        "disabled",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ManifestVerdict"
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
                        f"ManifestVerdict missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ManifestVerdict drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("ManifestVerdict class not found",)

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
        """Opt-in workflow — master_enabled MUST default-False
        so operators must explicitly baseline before enabling."""
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
                    "with default=False — opt-in workflow per "
                    "§33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_boundary_gate(
        tree: ast.AST, source: str,
    ) -> tuple:
        if "governance_boundary_gate" not in source:
            return (
                "must compose canonical "
                "governance_boundary_gate (Wave 2 #5) — no "
                "parallel canonical-governance-prefix lookup",
            )
        if "canonical_governance_prefix" not in source:
            return (
                "must reference "
                "canonical_governance_prefix accessor from the "
                "Wave 2 #5 substrate (no parallel path "
                "discovery)",
            )
        return ()

    def _validate_only_writer_is_refresh(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Only the operator-facing refresh_signed_manifest
        function may write to the manifest path. The rest of
        the substrate is pure-read."""
        write_functions: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "write_text"
                    ):
                        write_functions.append(node.name)
                    elif (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "replace"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id == "os"
                    ):
                        write_functions.append(node.name)
        # All discovered writers must be the canonical entry point
        allowed = {"refresh_signed_manifest"}
        violations = [
            f"unexpected writer: {fn}"
            for fn in set(write_functions) - allowed
        ]
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "governance_manifest_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "ManifestVerdict 5-value taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_manifest_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — manifest substrate MUST "
                "NOT import orchestrator / iron_gate / policy / "
                "providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / auto_committer / "
                "risk_tier_floor. AutoCommitter integration "
                "is consumer-side (lazy-imports this module)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_manifest_master_default_false"
            ),
            target_file=target,
            description=(
                "Opt-in workflow — master default-FALSE so "
                "operators baseline the manifest before "
                "enabling enforcement."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_manifest_composes_boundary_gate"
            ),
            target_file=target,
            description=(
                "Manifest substrate composes the Wave 2 #5 "
                "governance_boundary_gate canonical prefix "
                "accessor — no parallel path discovery."
            ),
            validate=_validate_composes_boundary_gate,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "governance_manifest_only_writer_is_refresh"
            ),
            target_file=target,
            description=(
                "Operator-binding 'pure substrate' — only "
                "refresh_signed_manifest writes to the "
                "manifest path. Read paths are byte-equivalent "
                "to side-effect-free."
            ),
            validate=_validate_only_writer_is_refresh,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Auto-discovered via §33.3."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "governance_manifest.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Governance manifest (hash-cap) gate master "
                "switch. Default-FALSE per §33.1 opt-in "
                "workflow — operator must baseline the "
                "manifest via refresh_signed_manifest() before "
                "enabling enforcement. AutoCommitter refuses "
                "commits touching governance/ when staged "
                "files drift from the operator-signed baseline."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_MANIFEST_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for the manifest path. "
                "Defaults to <repo>/.jarvis/"
                "governance_manifest.json."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=(
                f"{_ENV_MANIFEST_PATH}=/var/jarvis/manifest.json"
            ),
        ),
        FlagSpec(
            name=_ENV_MAX_FILES_SCAN,
            type=FlagType.INT,
            default=_DEFAULT_MAX_FILES,
            description=(
                "Defensive ceiling on files hashed per "
                "verification. Clamped to [10, 100_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_FILES_SCAN}=10000",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "GOVERNANCE_MANIFEST_SCHEMA_VERSION",
    "ManifestVerdict",
    "FileSignature",
    "ManifestSnapshot",
    "ManifestComparison",
    "RefreshOutcome",
    "master_enabled",
    "max_files_scan",
    "manifest_path",
    "is_refusal_verdict",
    "compute_current_signatures",
    "compute_current_manifest",
    "load_signed_manifest",
    "compare_manifests",
    "verify_governance_state",
    "refresh_signed_manifest",
    "register_shipped_invariants",
    "register_flags",
]
