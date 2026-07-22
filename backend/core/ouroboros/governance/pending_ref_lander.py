"""In-Memory Git Object Surgery & Quiescence Fast-Forward (2026-07-22).

Level-5 divergence resolution WITHOUT the stash hazard: when a verified
workspace commit cannot land directly on the operator tree (dirty
targets, divergence), the landing happens entirely in git's OBJECT
DATABASE — never the working directory, never the real index, never
HEAD, never the human's uncommitted WIP:

1. **In-memory 3-way merge** — ``git merge-tree --write-tree`` evaluates
   operator-HEAD x workspace-commit against their merge base fully in
   memory (git >= 2.38). Zero disk-byte mutation of the checkout.
2. **Non-destructive ref landing** — the merged tree is committed via
   ``commit-tree`` (two parents: operator HEAD + workspace commit) and
   parked on ``refs/heads/ouroboros/pending/<op>`` via ``update-ref``
   (reflogged — full auditability). The working tree remains
   byte-identical throughout.
3. **AST-aware semantic reconciliation** — on merge-tree CONFLICT, the
   (base, ours, theirs) blob payloads route to an injectable resolver
   (production default: the DoubleWord Realtime tier through
   ``cognition_lanes.rt_prompt`` — THE one RT primitive, DRY). The
   resolved blobs are written with ``hash-object -w``, woven into a
   tree via a THROWAWAY ``GIT_INDEX_FILE`` (never the real index), and
   the reconciliation commit lands strictly on the pending ref as an
   Orange-tier review object. Conflict markers never touch disk.
4. **Quiescence fast-forward** — pending refs that are clean
   fast-forwards of HEAD auto-land once the touched paths have been
   quiescent for ``JARVIS_QUIESCENCE_FF_IDLE_S`` (default 30s),
   measured by the EXISTING ``LiveWorkSensor`` with a scoped
   ``active_window_s`` — the only step that legitimately moves
   HEAD/worktree, and only when git itself proves ``--ff-only`` safety
   and the tree is clean at the touched paths.

Invariant (the whole point): the human's live state — working tree
bytes, uncommitted WIP, the real index — is NEVER written by stages
1-3. No ``git stash`` exists anywhere in this module.

All git flows through ``WorktreeManager``'s async subprocess wrappers
(``_run_git_rc``) — no duplicate wrapper logic. Everything env-tunable;
fail-soft throughout (any degradation falls back to the promoter's
existing typed refusal path).
"""

from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


_LANDING_ENABLED_ENV = "JARVIS_PENDING_REF_LANDING_ENABLED"
_RESOLVER_ENABLED_ENV = "JARVIS_SEMANTIC_MERGE_RESOLVER_ENABLED"
_QUIESCENCE_FF_ENABLED_ENV = "JARVIS_QUIESCENCE_FF_ENABLED"
_QUIESCENCE_IDLE_ENV = "JARVIS_QUIESCENCE_FF_IDLE_S"
_RESOLVER_MAX_BYTES_ENV = "JARVIS_SEMANTIC_MERGE_MAX_FILE_BYTES"

_PENDING_REF_PREFIX = "refs/heads/ouroboros/pending/"

#: Resolver contract: (rel_path, base, ours, theirs) -> merged content
#: or None (cannot resolve — the file stays unresolved and landing is
#: declined). Injectable for tests; production default is the DW-RT
#: builder below.
SemanticResolver = Callable[
    [str, str, str, str], Awaitable[Optional[str]],
]


def landing_enabled() -> bool:
    """Master — default TRUE (stages 1-3 are strictly non-destructive)."""
    raw = os.environ.get(_LANDING_ENABLED_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def resolver_enabled() -> bool:
    """Semantic reconciliation master — default FALSE (spends DW-RT
    tokens and produces Orange-review objects; operators opt in)."""
    raw = os.environ.get(_RESOLVER_ENABLED_ENV, "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def quiescence_ff_enabled() -> bool:
    """Quiescence auto-fast-forward master — default TRUE."""
    raw = os.environ.get(
        _QUIESCENCE_FF_ENABLED_ENV, "true",
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


def quiescence_idle_s() -> float:
    """``JARVIS_QUIESCENCE_FF_IDLE_S`` — default 30.0."""
    try:
        return max(0.0, float(
            os.environ.get(_QUIESCENCE_IDLE_ENV, "30").strip(),
        ))
    except (TypeError, ValueError):
        return 30.0


def _resolver_max_bytes() -> int:
    try:
        return max(1024, int(os.environ.get(
            _RESOLVER_MAX_BYTES_ENV, "262144",
        ).strip()))
    except (TypeError, ValueError):
        return 262_144


def pending_ref_name(op_id: str) -> str:
    """Deterministic, ref-safe pending ref for one op."""
    safe = "".join(
        c if (c.isalnum() or c in "-_") else "-" for c in (op_id or "op")
    )[:80]
    return _PENDING_REF_PREFIX + safe


@dataclass(frozen=True)
class PendingLandOutcome:
    """What the object-surgery landing did.

    ``state`` vocabulary (closed): ``landed_clean`` (in-memory merge was
    conflict-free), ``landed_resolved`` (semantic reconciliation wove the
    conflicts; Orange review), ``conflict_unresolved`` (resolver off/
    declined — caller falls back to the legacy typed refusal),
    ``unsupported`` (git too old / plumbing failure — legacy refusal),
    ``disabled``.
    """

    landed: bool
    state: str
    ref: str = ""
    commit: str = ""
    conflicted_paths: Tuple[str, ...] = ()
    detail: str = ""


# ---------------------------------------------------------------------------
# Plumbing helpers — every subprocess rides WorktreeManager._run_git_rc
# ---------------------------------------------------------------------------


async def _git(mgr: Any, cwd: Path, *args: str) -> Tuple[int, str, str]:
    rc, out, err = await mgr._run_git_rc(cwd, list(args))
    return rc, (out or ""), (err or "")


async def _merge_tree(
    mgr: Any, root: Path, ours: str, theirs: str,
) -> Tuple[Optional[str], List[str], str]:
    """``git merge-tree --write-tree -z ours theirs``: pure in-memory
    3-way merge. Returns ``(tree_oid, conflicted_paths, raw)``;
    ``tree_oid`` is None on plumbing failure. Conflicted merges STILL
    return the (marker-bearing) tree oid — those markers exist only in
    the object database, never on disk.
    """
    rc, out, err = await _git(
        mgr, root, "merge-tree", "--write-tree", "-z",
        "--name-only", ours, theirs,
    )
    if rc not in (0, 1) or not out:
        return None, [], err.strip()[:200]
    # -z framing (verified against git 2.52):
    #   toks[0]                       = merged tree oid
    #   toks[1 .. first empty token)  = conflicted paths (--name-only)
    #   <empty token>                 = section separator
    #   toks[...]                     = informational-message block
    # rc=0 (clean) → toks[1] is already the empty separator.
    toks = out.split("\0")
    tree_oid = toks[0].strip() if toks else ""
    conflicted: List[str] = []
    for t in toks[1:]:
        if t == "":
            break  # end of the conflicted-paths section
        p = t.strip()
        if p:
            conflicted.append(p)
    return tree_oid or None, sorted(set(conflicted)), ""


async def _stage_blob(
    mgr: Any, root: Path, content: str,
) -> Optional[str]:
    """``hash-object -w --stdin`` — content flows through the extended
    wrapper's per-spawn stdin channel; no temp files, no disk contact."""
    try:
        rc, out, _err = await mgr._run_git_rc_ex(
            root, ["hash-object", "-w", "--stdin"],
            stdin_data=content.encode("utf-8", errors="replace"),
        )
        return out.strip() if rc == 0 and out.strip() else None
    except Exception:  # noqa: BLE001 — fail-soft
        return None


async def _blob_at(
    mgr: Any, root: Path, commitish: str, rel_path: str,
) -> Optional[str]:
    """File content at ``commitish:rel_path`` (None when absent)."""
    rc, out, _err = await _git(
        mgr, root, "show", f"{commitish}:{rel_path}",
    )
    return out if rc == 0 else None


async def _weave_resolved_tree(
    mgr: Any,
    root: Path,
    base_tree: str,
    resolved: Dict[str, str],
) -> Optional[str]:
    """Replace ``resolved`` blobs inside ``base_tree`` and return the
    new tree oid — via a THROWAWAY ``GIT_INDEX_FILE`` scoped to each
    subprocess spawn (the extended wrapper's per-spawn env; the REAL
    index, ``os.environ``, and the working tree are never touched).
    """
    tmp_index = None
    try:
        fd, tmp_index = tempfile.mkstemp(suffix=".jarvis-index")
        os.close(fd)
        os.unlink(tmp_index)  # git creates it fresh
        _idx_env = {"GIT_INDEX_FILE": tmp_index}

        rc, _o, err = await mgr._run_git_rc_ex(
            root, ["read-tree", base_tree], extra_env=_idx_env,
        )
        if rc != 0:
            logger.debug(
                "[PendingRefLander] read-tree failed: %s", err[:200],
            )
            return None
        for rel, oid in resolved.items():
            rc, _o, err = await mgr._run_git_rc_ex(
                root,
                ["update-index", "--add", "--cacheinfo",
                 f"100644,{oid},{rel}"],
                extra_env=_idx_env,
            )
            if rc != 0:
                logger.debug(
                    "[PendingRefLander] update-index failed for %s: %s",
                    rel, err[:200],
                )
                return None
        rc, out, err = await mgr._run_git_rc_ex(
            root, ["write-tree"], extra_env=_idx_env,
        )
        if rc != 0 or not out.strip():
            return None
        return out.strip()
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug(
            "[PendingRefLander] tree weave failed", exc_info=True,
        )
        return None
    finally:
        if tmp_index:
            try:
                os.unlink(tmp_index)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Production semantic resolver — DoubleWord Realtime via cognition_lanes
# ---------------------------------------------------------------------------


def build_dw_semantic_resolver() -> Optional[SemanticResolver]:
    """The DW-RT resolver over ``cognition_lanes.rt_prompt`` (THE one
    RT primitive — lease/clamp/eviction/cascade come free). Returns
    None when the lane is unavailable (caller declines resolution)."""
    try:
        from backend.core.ouroboros.governance.cognition_lanes import (
            rt_prompt,
        )
    except Exception:  # noqa: BLE001 — lane unavailable
        return None

    # Model resolution rides the provider stack's own env carriers —
    # zero hardcoded model names (repo convention): the dedicated
    # override first, else the DoublewordProvider's model env. Empty →
    # decline (the resolver never guesses a model).
    _model = (
        os.environ.get("JARVIS_SEMANTIC_MERGE_MODEL", "").strip()
        or os.environ.get("DOUBLEWORD_MODEL", "").strip()
    )
    if not _model:
        return None

    async def _resolve(
        rel_path: str, base: str, ours: str, theirs: str,
    ) -> Optional[str]:
        cap = _resolver_max_bytes()
        if max(len(base), len(ours), len(theirs)) > cap:
            return None  # too large for a reliable weave — decline
        prompt = (
            "You are SemanticMergeResolver performing an AST-aware "
            "three-way merge of one source file.\n"
            f"FILE: {rel_path}\n\n"
            "Weave OURS (the operator tree's committed state) with "
            "THEIRS (the verified autonomous repair) relative to BASE. "
            "Preserve the INTENT of both sides; prefer THEIRS for the "
            "repaired logic and OURS for unrelated changes. Output "
            "ONLY the complete merged file content — no markdown "
            "fences, no commentary, and ABSOLUTELY no git conflict "
            "markers.\n\n"
            f"<BASE>\n{base}\n</BASE>\n\n"
            f"<OURS>\n{ours}\n</OURS>\n\n"
            f"<THEIRS>\n{theirs}\n</THEIRS>\n"
        )
        try:
            merged = await rt_prompt(
                prompt,
                model=_model,
                caller_id="semantic_merge_resolver",
            )
        except Exception:  # noqa: BLE001 — decline on lane failure
            logger.debug(
                "[SemanticMergeResolver] rt_prompt failed for %s",
                rel_path, exc_info=True,
            )
            return None
        if not merged or "<<<<<<<" in merged or ">>>>>>>" in merged:
            return None  # refuse marker-bearing or empty output
        return merged

    return _resolve


# ---------------------------------------------------------------------------
# Stage 1-3 — the non-destructive landing
# ---------------------------------------------------------------------------


async def land_pending_ref(
    mgr: Any,
    operator_root: Path,
    workspace_commit: str,
    op_id: str,
    *,
    resolver: Optional[SemanticResolver] = None,
) -> PendingLandOutcome:
    """Land ``workspace_commit`` onto ``ouroboros/pending/<op>`` via
    pure object surgery. NEVER touches the working tree / real index /
    HEAD. NEVER raises — every failure degrades to a non-landed
    outcome the promoter converts into its legacy typed refusal.
    """
    if not landing_enabled():
        return PendingLandOutcome(False, "disabled")
    try:
        rc, head_out, _err = await _git(
            mgr, operator_root, "rev-parse", "HEAD",
        )
        if rc != 0 or not head_out.strip():
            return PendingLandOutcome(
                False, "unsupported", detail="no HEAD",
            )
        head = head_out.strip()

        tree_oid, conflicted, err = await _merge_tree(
            mgr, operator_root, head, workspace_commit,
        )
        if tree_oid is None:
            return PendingLandOutcome(
                False, "unsupported",
                detail=f"merge-tree unavailable/failed: {err}",
            )

        state = "landed_clean"
        if conflicted:
            # Semantic reconciliation — strictly in the object DB.
            _resolver = resolver
            if _resolver is None and resolver_enabled():
                _resolver = build_dw_semantic_resolver()
            if _resolver is None:
                return PendingLandOutcome(
                    False, "conflict_unresolved",
                    conflicted_paths=tuple(conflicted),
                    detail="resolver disabled/unavailable",
                )
            # True 3-way inputs: merge-base for BASE (once for the op).
            rc_b, mb_out, _e = await _git(
                mgr, operator_root, "merge-base", head,
                workspace_commit,
            )
            merge_base = mb_out.strip() if rc_b == 0 else head
            resolved_blobs: Dict[str, str] = {}
            for rel in conflicted:
                base_c = await _blob_at(
                    mgr, operator_root, merge_base, rel,
                ) or ""
                ours_c = await _blob_at(
                    mgr, operator_root, head, rel,
                ) or ""
                theirs_c = await _blob_at(
                    mgr, operator_root, workspace_commit, rel,
                ) or ""
                merged = await _resolver(rel, base_c, ours_c, theirs_c)
                if merged is None:
                    return PendingLandOutcome(
                        False, "conflict_unresolved",
                        conflicted_paths=tuple(conflicted),
                        detail=f"resolver declined {rel}",
                    )
                blob_oid = await _stage_blob(
                    mgr, operator_root, merged,
                )
                if blob_oid is None:
                    return PendingLandOutcome(
                        False, "unsupported",
                        detail=f"hash-object failed for {rel}",
                    )
                resolved_blobs[rel] = blob_oid
            woven = await _weave_resolved_tree(
                mgr, operator_root, tree_oid, resolved_blobs,
            )
            if woven is None:
                return PendingLandOutcome(
                    False, "unsupported", detail="tree weave failed",
                )
            tree_oid = woven
            state = "landed_resolved"

        # commit-tree: a true merge commit (operator HEAD + workspace
        # commit as parents) so history is honest either way.
        _kind = (
            "semantic reconciliation (Orange review)"
            if state == "landed_resolved" else "clean in-memory merge"
        )
        rc, commit_out, err = await _git(
            mgr, operator_root, "commit-tree", tree_oid,
            "-p", head, "-p", workspace_commit,
            "-m",
            (
                f"ouroboros(pending): {_kind} of {workspace_commit[:12]} "
                f"onto {head[:12]} [op={op_id}]"
            ),
        )
        if rc != 0 or not commit_out.strip():
            return PendingLandOutcome(
                False, "unsupported",
                detail=f"commit-tree failed: {err[:200]}",
            )
        new_commit = commit_out.strip()

        ref = pending_ref_name(op_id)
        rc, _o, err = await _git(
            mgr, operator_root, "update-ref",
            "-m", f"ouroboros pending landing op={op_id}",
            "--create-reflog", ref, new_commit,
        )
        if rc != 0:
            return PendingLandOutcome(
                False, "unsupported",
                detail=f"update-ref failed: {err[:200]}",
            )

        logger.warning(
            "[PendingRefLander] op=%s %s — pending ref %s = %s "
            "(parents: HEAD %s + workspace %s); working tree untouched"
            "%s",
            op_id, state.upper(), ref, new_commit[:12], head[:12],
            workspace_commit[:12],
            "; ORANGE review required before integration"
            if state == "landed_resolved" else "",
        )
        try:
            from backend.api.hive_emitter import hive_emit, hive_flush
            hive_emit(
                actor_id="pending_ref_lander",
                subsystem="governance",
                intent="pending_ref_landed",
                summary=(
                    f"{state}: {workspace_commit[:12]} parked on {ref} "
                    f"({len(conflicted)} conflict(s) "
                    f"{'semantically resolved' if conflicted else ''})"
                ),
                severity="warn" if state == "landed_resolved" else "info",
                trace_id=op_id,
                detail={
                    "state": state,
                    "ref": ref[:120],
                    "commit": new_commit[:40],
                    "conflicts": len(conflicted),
                },
            )
            hive_flush("pending_ref_lander", "pending_ref_landed")
        except Exception:  # noqa: BLE001 — telemetry never masks state
            pass
        return PendingLandOutcome(
            True, state, ref=ref, commit=new_commit,
            conflicted_paths=tuple(conflicted),
        )
    except Exception:  # noqa: BLE001 — landing is fail-soft
        logger.debug(
            "[PendingRefLander] landing failed op=%s", op_id,
            exc_info=True,
        )
        return PendingLandOutcome(False, "unsupported", detail="internal")


# ---------------------------------------------------------------------------
# Stage 4 — Quiescence fast-forward
# ---------------------------------------------------------------------------


async def try_quiescence_fastforward(
    mgr: Any,
    operator_root: Path,
) -> List[str]:
    """Land every pending ref that git proves is a clean fast-forward
    of HEAD, once its touched paths have been quiescent for
    ``quiescence_idle_s()`` (measured by the EXISTING LiveWorkSensor
    with a scoped window) and are clean in the working tree. Returns
    the landed ref names. Fail-soft; NEVER raises.
    """
    landed: List[str] = []
    if not (landing_enabled() and quiescence_ff_enabled()):
        return landed
    try:
        rc, out, _err = await _git(
            mgr, operator_root, "for-each-ref",
            "--format=%(refname)", _PENDING_REF_PREFIX.rstrip("/"),
        )
        refs = [r.strip() for r in out.splitlines() if r.strip()]
        if rc != 0 or not refs:
            return landed

        from backend.core.ouroboros.governance.live_work_sensor import (
            LiveWorkSensor,
        )
        idle_s = quiescence_idle_s()
        sensor = LiveWorkSensor(
            operator_root, active_window_s=max(idle_s, 0.001),
        )

        for ref in refs:
            # ff-ability is git's own proof, never ours.
            rc, _o, _e = await _git(
                mgr, operator_root, "merge-base", "--is-ancestor",
                "HEAD", ref,
            )
            if rc != 0:
                continue  # diverged — stays pending (review/telemetry)
            rc, diff_out, _e = await _git(
                mgr, operator_root, "diff", "--name-only",
                "HEAD", ref,
            )
            touched = [p for p in diff_out.splitlines() if p.strip()]
            quiescent = True
            for rel in touched:
                try:
                    ev = await sensor.evaluate(rel)
                    if getattr(ev, "is_human_active", False) or getattr(
                        ev, "active", False,
                    ):
                        quiescent = False
                        break
                except Exception:  # noqa: BLE001 — conservative
                    quiescent = False
                    break
            if not quiescent:
                continue
            # Touched paths must be clean in the working tree — never
            # overwrite uncommitted bytes, even on a proven ff.
            rc, status_out, _e = await _git(
                mgr, operator_root, "status", "--porcelain", "--",
                *touched,
            )
            if rc != 0 or status_out.strip():
                continue
            rc, _o, err = await _git(
                mgr, operator_root, "merge", "--ff-only", ref,
            )
            if rc != 0:
                logger.debug(
                    "[PendingRefLander] ff-only declined for %s: %s",
                    ref, err[:200],
                )
                continue
            await _git(mgr, operator_root, "update-ref", "-d", ref)
            landed.append(ref)
            logger.warning(
                "[PendingRefLander] QUIESCENCE-FF LANDED %s onto HEAD "
                "at %s (paths quiescent >= %.0fs, tree clean)",
                ref, operator_root, quiescence_idle_s(),
            )
    except Exception:  # noqa: BLE001 — fail-soft
        logger.debug(
            "[PendingRefLander] quiescence ff sweep failed",
            exc_info=True,
        )
    return landed


__all__ = [
    "PendingLandOutcome",
    "SemanticResolver",
    "build_dw_semantic_resolver",
    "land_pending_ref",
    "landing_enabled",
    "pending_ref_name",
    "quiescence_ff_enabled",
    "quiescence_idle_s",
    "resolver_enabled",
    "try_quiescence_fastforward",
]
