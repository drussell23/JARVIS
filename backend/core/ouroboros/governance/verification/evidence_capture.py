"""Priority F2 + F3 — Evidence capture at PLAN/APPLY phase boundaries.

The empirical loop-closer. Pre-F2/F3, the F1 evidence collectors had
to fall back to self-gather paths that produced INSUFFICIENT_EVIDENCE
verdicts on most ops because:

  * test_files_pre cannot be reconstructed post-APPLY (the original
    test inventory is gone)
  * diff_text cannot be reconstructed post-APPLY without a pre-APPLY
    snapshot
  * target_files_post can self-gather but loses pre/post pairing

This module ships pure-stdlib snapshotters that the PLAN runner
(test_files_pre) and the APPLY runner (target_files_pre →
target_files_post → diff_text) call to stamp ctx with rich evidence
BEFORE the F1 gatherers read it.

Functions
---------

  * ``capture_test_files_inventory(target_dir, *, pattern)``
      — Pure: glob ``tests/**/*.py`` (configurable pattern), return
        sorted tuple of relative paths.
  * ``stamp_test_files_pre(ctx, *, target_dir)``
      — Stamps ``ctx.test_files_pre`` via object.__setattr__ on the
        frozen OperationContext. Idempotent; respects existing
        stamping (caller's pre-stamp takes priority).
  * ``snapshot_target_files(target_files)``
      — Pure: read each file's current content, return tuple of
        ``{path, content}`` dicts.
  * ``stamp_target_files_pre(ctx)`` / ``stamp_target_files_post(ctx)``
      — Snapshot + stamp. Pre is called BEFORE change_engine.execute;
        post AFTER.
  * ``compute_unified_diff(pre_snapshot, post_snapshot)``
      — Pure: stdlib ``difflib.unified_diff`` between the two
        snapshots, joined into a single str.
  * ``stamp_diff_text(ctx, *, pre_snapshot, post_snapshot)``
      — Computes unified diff and stamps ``ctx.diff_text``.
  * ``stamp_test_files_post(ctx, *, target_dir)``
      — Captures the post-state test inventory at APPLY-success.

Master flag ``JARVIS_EVIDENCE_CAPTURE_ENABLED`` (default ``true``).
When off, all stamping functions are no-ops; the F1 gatherers fall
back to their self-gather paths (which produce honest INSUFFICIENT
for the kinds that can't self-gather).

Authority invariants (AST-pinned by tests):
  * No imports of orchestrator / phase_runner / candidate_generator /
    iron_gate / change_engine / policy / semantic_guardian.
  * Pure stdlib + verification.* (own slice family).
  * NEVER raises out of any public method.
  * Read-only over filesystem on snapshot reads; writes only via
    ``object.__setattr__`` on the supplied ctx.
"""
from __future__ import annotations

import difflib
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


EVIDENCE_CAPTURE_SCHEMA_VERSION: str = "evidence_capture.1"


# Cap on per-file content captured. Defends against accidentally
# slurping a 100MB binary into ctx. 256 KiB is generous for source
# code (largest legit Python files in the repo are ~50 KiB).
MAX_FILE_BYTES_DEFAULT: int = 256 * 1024


# Cap on tests/ glob result count. Defends against pathological
# test trees with thousands of files.
MAX_TEST_FILES_DEFAULT: int = 4096


# Cap on unified-diff text length. Mirrors the conservative 256 KiB
# cap for individual file content; diffs larger than this are clipped
# with a tail marker.
MAX_DIFF_BYTES_DEFAULT: int = 256 * 1024


def evidence_capture_enabled() -> bool:
    """``JARVIS_EVIDENCE_CAPTURE_ENABLED`` (default ``true``).

    When off, all stamping functions are no-ops (return without
    mutating ctx). Hot-revert: a single env knob."""
    raw = os.environ.get(
        "JARVIS_EVIDENCE_CAPTURE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in ("1", "true", "yes", "on")


def _max_file_bytes() -> int:
    raw = os.environ.get(
        "JARVIS_EVIDENCE_MAX_FILE_BYTES",
        str(MAX_FILE_BYTES_DEFAULT),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_FILE_BYTES_DEFAULT


def _max_test_files() -> int:
    raw = os.environ.get(
        "JARVIS_EVIDENCE_MAX_TEST_FILES",
        str(MAX_TEST_FILES_DEFAULT),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_TEST_FILES_DEFAULT


def _max_diff_bytes() -> int:
    raw = os.environ.get(
        "JARVIS_EVIDENCE_MAX_DIFF_BYTES",
        str(MAX_DIFF_BYTES_DEFAULT),
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MAX_DIFF_BYTES_DEFAULT


# ---------------------------------------------------------------------------
# Test inventory capture (F3 — PLAN-time)
# ---------------------------------------------------------------------------


def capture_test_files_inventory(
    target_dir: Optional[str] = None,
    *,
    pattern: str = "tests/**/*.py",
) -> Tuple[str, ...]:
    """Pure-stdlib glob over ``target_dir/tests/**/*.py``. Returns a
    sorted tuple of paths relative to ``target_dir`` (or absolute if
    target_dir is None / unresolvable). NEVER raises.

    Capped at ``JARVIS_EVIDENCE_MAX_TEST_FILES`` (default 4096) to
    defend against pathological test trees."""
    try:
        base = Path(str(target_dir or ".")).resolve()
        if not base.exists() or not base.is_dir():
            return ()
        cap = _max_test_files()
        out: List[str] = []
        for p in base.glob(pattern):
            try:
                if not p.is_file():
                    continue
                # Compute relative path defensively
                try:
                    rel = p.relative_to(base)
                    out.append(str(rel))
                except ValueError:
                    out.append(str(p))
                if len(out) >= cap:
                    break
            except OSError:
                continue
        out.sort()
        return tuple(out)
    except Exception:  # noqa: BLE001 — defensive
        return ()


def stamp_test_files_pre(
    ctx: Any, *, target_dir: Optional[str] = None,
) -> int:
    """Stamp ``ctx.test_files_pre`` with the current test inventory.
    Returns the count of files captured. NEVER raises.

    Idempotent: if ``ctx.test_files_pre`` is already set on the
    context, this is a no-op (caller's pre-stamp wins).

    Master-flag-gated: returns 0 without touching ctx when the
    master flag is off."""
    if not evidence_capture_enabled():
        return 0
    if ctx is None:
        return 0
    try:
        # Respect existing stamping
        existing = getattr(ctx, "test_files_pre", None)
        if existing is not None:
            return len(existing) if hasattr(existing, "__len__") else 0
        inventory = capture_test_files_inventory(target_dir)
        try:
            object.__setattr__(ctx, "test_files_pre", inventory)
        except (AttributeError, TypeError):
            # Frozen-with-slots ctx without __setattr__ override —
            # silently skip (the F1 gatherer will return
            # INSUFFICIENT correctly).
            return 0
        return len(inventory)
    except Exception:  # noqa: BLE001
        return 0


def stamp_test_files_post(
    ctx: Any, *, target_dir: Optional[str] = None,
) -> int:
    """Stamp ``ctx.test_files_post`` with the post-APPLY test
    inventory. Returns count. NEVER raises.

    Unlike pre-stamping, post-stamping ALWAYS overwrites — the
    post-state is what we want to compare against pre."""
    if not evidence_capture_enabled():
        return 0
    if ctx is None:
        return 0
    try:
        inventory = capture_test_files_inventory(target_dir)
        try:
            object.__setattr__(ctx, "test_files_post", inventory)
        except (AttributeError, TypeError):
            return 0
        return len(inventory)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Target file content capture (F2 — APPLY-time)
# ---------------------------------------------------------------------------


def snapshot_target_files(
    target_files: Any,
) -> Tuple[Dict[str, Any], ...]:
    """Read each file in ``target_files`` and return a tuple of
    ``{path, content, exists}`` dicts. NEVER raises.

    Per-file content cap: ``JARVIS_EVIDENCE_MAX_FILE_BYTES`` (default
    256 KiB). Files exceeding the cap have their content truncated
    with a clipping marker.

    Missing files are recorded with ``exists=False`` and empty
    content (the F1 evaluator can detect their absence)."""
    if not target_files:
        return ()
    try:
        cap = _max_file_bytes()
        out: List[Dict[str, Any]] = []
        for raw in target_files:
            try:
                p = Path(str(raw))
                if not p.exists():
                    out.append({
                        "path": str(p),
                        "content": "",
                        "exists": False,
                    })
                    continue
                if not p.is_file():
                    continue
                try:
                    raw_bytes = p.read_bytes()
                except OSError:
                    out.append({
                        "path": str(p),
                        "content": "",
                        "exists": True,
                    })
                    continue
                if len(raw_bytes) > cap:
                    truncated = raw_bytes[:cap].decode(
                        "utf-8", errors="replace",
                    )
                    truncated_text = (
                        truncated + "\n... (clipped at "
                        + str(cap) + " bytes)\n"
                    )
                    out.append({
                        "path": str(p),
                        "content": truncated_text,
                        "exists": True,
                    })
                else:
                    text = raw_bytes.decode(
                        "utf-8", errors="replace",
                    )
                    out.append({
                        "path": str(p),
                        "content": text,
                        "exists": True,
                    })
            except OSError:
                continue
            except Exception:  # noqa: BLE001
                continue
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


def stamp_target_files_pre(ctx: Any) -> int:
    """Snapshot ctx.target_files content and stamp ctx.target_files_pre.
    Called BEFORE change_engine.execute. Returns file count. NEVER
    raises."""
    if not evidence_capture_enabled():
        return 0
    if ctx is None:
        return 0
    try:
        targets = getattr(ctx, "target_files", None)
        if not targets:
            return 0
        snapshot = snapshot_target_files(targets)
        try:
            object.__setattr__(ctx, "target_files_pre", snapshot)
        except (AttributeError, TypeError):
            return 0
        return len(snapshot)
    except Exception:  # noqa: BLE001
        return 0


def stamp_target_files_post(ctx: Any) -> int:
    """Snapshot ctx.target_files content and stamp ctx.target_files_post.
    Called AFTER change_engine.execute (success path).

    The combination of target_files_pre + target_files_post enables
    the diff_text computation. NEVER raises."""
    if not evidence_capture_enabled():
        return 0
    if ctx is None:
        return 0
    try:
        targets = getattr(ctx, "target_files", None)
        if not targets:
            return 0
        snapshot = snapshot_target_files(targets)
        try:
            object.__setattr__(ctx, "target_files_post", snapshot)
        except (AttributeError, TypeError):
            return 0
        return len(snapshot)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Unified diff computation (F2 — APPLY-time)
# ---------------------------------------------------------------------------


def compute_unified_diff(
    pre_snapshot: Any, post_snapshot: Any,
) -> str:
    """Compute the unified diff between pre and post target-file
    snapshots. Pure-stdlib via ``difflib.unified_diff``. NEVER raises.

    Each snapshot is a sequence of ``{path, content}`` dicts. The
    diff is keyed by path so renames / deletes / additions surface
    correctly:

      * File present in pre + post with different content → diff hunk
      * File only in post → addition diff (content vs empty)
      * File only in pre → deletion diff (empty vs content)

    Returns a single str (multi-file unified diff). Capped at
    ``JARVIS_EVIDENCE_MAX_DIFF_BYTES`` with a clipping marker."""
    try:
        pre_map = _snapshot_to_path_map(pre_snapshot)
        post_map = _snapshot_to_path_map(post_snapshot)
        all_paths = sorted(set(pre_map.keys()) | set(post_map.keys()))
        out_chunks: List[str] = []
        cap = _max_diff_bytes()
        running_size = 0
        for path in all_paths:
            pre_text = pre_map.get(path, "")
            post_text = post_map.get(path, "")
            if pre_text == post_text:
                continue  # no change for this file
            from_label = path + " (pre)"
            to_label = path + " (post)"
            try:
                hunk_lines = list(difflib.unified_diff(
                    pre_text.splitlines(keepends=True),
                    post_text.splitlines(keepends=True),
                    fromfile=from_label,
                    tofile=to_label,
                    n=3,
                ))
            except Exception:  # noqa: BLE001
                continue
            chunk = "".join(hunk_lines)
            if running_size + len(chunk) > cap:
                # Clip at this point with a marker
                remaining = max(0, cap - running_size)
                if remaining > 0:
                    out_chunks.append(chunk[:remaining])
                out_chunks.append(
                    "\n... (diff clipped at " + str(cap) + " bytes)\n"
                )
                break
            out_chunks.append(chunk)
            running_size += len(chunk)
        return "".join(out_chunks)
    except Exception:  # noqa: BLE001
        return ""


def _snapshot_to_path_map(snapshot: Any) -> Dict[str, str]:
    """Helper — convert a snapshot tuple to a {path: content} dict.
    Defensive on malformed entries."""
    out: Dict[str, str] = {}
    if not snapshot:
        return out
    try:
        for entry in snapshot:
            try:
                path = str(entry.get("path", "") or "")
                content = entry.get("content", "")
                if not path:
                    continue
                if not isinstance(content, str):
                    content = str(content)
                out[path] = content
            except (AttributeError, TypeError):
                continue
    except (TypeError, ValueError):
        return out
    return out


def stamp_diff_text(
    ctx: Any,
    *,
    pre_snapshot: Optional[Any] = None,
    post_snapshot: Optional[Any] = None,
) -> int:
    """Compute unified diff and stamp ``ctx.diff_text``. NEVER raises.

    If snapshots aren't supplied, reads them from ctx.target_files_pre
    and ctx.target_files_post. Returns the diff_text byte length on
    success, 0 if neither input is available."""
    if not evidence_capture_enabled():
        return 0
    if ctx is None:
        return 0
    try:
        if pre_snapshot is None:
            pre_snapshot = getattr(ctx, "target_files_pre", None)
        if post_snapshot is None:
            post_snapshot = getattr(ctx, "target_files_post", None)
        if pre_snapshot is None and post_snapshot is None:
            return 0
        diff = compute_unified_diff(
            pre_snapshot or (), post_snapshot or (),
        )
        try:
            object.__setattr__(ctx, "diff_text", diff)
        except (AttributeError, TypeError):
            return 0
        return len(diff)
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# Convenience composite — capture everything for an APPLY-success path
# ---------------------------------------------------------------------------


def stamp_apply_evidence_post(
    ctx: Any, *, target_dir: Optional[str] = None,
) -> Dict[str, int]:
    """One-stop helper called from APPLY-success: stamps
    target_files_post + test_files_post + diff_text on ctx.

    Assumes target_files_pre + test_files_pre have been stamped
    previously (PLAN-time for tests, APPLY-pre for files). When
    a pre-stamp is missing, the corresponding post stamp still
    fires (the F1 gatherer will return INSUFFICIENT for the missing
    half — honest semantics).

    Returns a diagnostic dict with per-stamp counts. NEVER raises."""
    if not evidence_capture_enabled():
        return {"enabled": 0}
    return {
        "enabled": 1,
        "target_files_post": stamp_target_files_post(ctx),
        "test_files_post": stamp_test_files_post(
            ctx, target_dir=target_dir,
        ),
        "diff_text_bytes": stamp_diff_text(ctx),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVIDENCE_CAPTURE_SCHEMA_VERSION",
    "MAX_DIFF_BYTES_DEFAULT",
    "MAX_FILE_BYTES_DEFAULT",
    "MAX_TEST_FILES_DEFAULT",
    "capture_test_files_inventory",
    "compute_unified_diff",
    "evidence_capture_enabled",
    "snapshot_target_files",
    "stamp_apply_evidence_post",
    "stamp_diff_text",
    "stamp_target_files_post",
    "stamp_target_files_pre",
    "stamp_test_files_post",
    "stamp_test_files_pre",
]
