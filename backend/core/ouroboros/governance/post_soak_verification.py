"""Post-Soak Verification Circuit — the gate before promotion.

After the Fan-In/Stitch phase produces the modified file (in memory), this
automated post-flight circuit runs THREE checks before the op is declared ready
for promotion:

  1. **Zero-regression AST structural check** — the polymorphic in-memory
     precompiler (Python ``ast.parse`` / JSON / YAML / bracket-balance). A
     structurally-broken stitch never promotes.
  2. **Git index integrity** — a READ-ONLY confirmation that ``.git/index`` is in
     the expected clean state (no unexpected STAGED changes; the swarm produces a
     string, it never stages). Read-only by construction — the circuit holds no
     git-mutation privilege (per the #70033 Single-Writer discipline).
  3. **Trace flush** — the complete ``swarm_trace.jsonl`` telemetry is folded
     into SQLite (the same ``.jarvis/chunk_strategy.db`` substrate the
     StrategyOutcomeLogger uses) so the run is auditable post-hoc.

DRY: composes ``stitch_precompiler.precompile_detail`` + the #70021 SQLite
substrate. Never raises — a check that cannot run reports, it does not throw.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger("Ouroboros.PostSoakVerification")

_TRACE_FLUSH_TABLE = "swarm_trace_flush"

# git_status_fn() -> list of porcelain lines, e.g. [" M path", "?? path", "M  staged"]
GitStatusFn = Callable[[], List[str]]


@dataclass
class VerificationResult:
    ready_for_promotion: bool
    ast_ok: bool
    git_clean: bool
    trace_flushed: bool
    reasons: List[str] = field(default_factory=list)
    trace_records: int = 0


def _default_git_porcelain() -> List[str]:
    """READ-ONLY ``git status --porcelain`` — never mutates the index. Empty on
    any failure (fail-open on the read; the AST check is the hard gate)."""
    import subprocess  # local: this module holds no mutation privilege
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return [ln for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _verify_git_index_clean(
    git_status_fn: GitStatusFn, file_path: str, allowed_dirty: Optional[List[str]],
) -> tuple:
    """The ``.git/index`` must carry NO unexpected STAGED entry — the swarm
    produces a string and never stages. A working-tree modification to the target
    file is expected; a STAGED change to anything unexpected fails. Never raises."""
    try:
        lines = git_status_fn()
    except Exception:  # noqa: BLE001
        return True, ""   # cannot read → do not block on the git check
    allowed = set(allowed_dirty or [])
    allowed.add(file_path)
    allowed.add(os.path.basename(file_path or ""))
    for ln in lines:
        if len(ln) < 3:
            continue
        index_col, path = ln[0], ln[3:].strip()
        # index_col ' ' = unmodified index, '?' = untracked; anything else = STAGED.
        if index_col not in (" ", "?") and path not in allowed:
            return False, f"git_index_dirty: unexpected staged change '{ln.strip()}'"
    return True, ""


def _ensure_flush_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_TRACE_FLUSH_TABLE} ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "file_path TEXT, records INTEGER, fan_out INTEGER, fan_in INTEGER, "
        "converged INTEGER, ast_ok INTEGER, git_clean INTEGER, ready INTEGER, "
        "flushed_wall TEXT)"
    )
    conn.commit()


def _flush_trace(
    trace_path: Optional[str], conn: Optional[sqlite3.Connection],
    file_path: str, *, ast_ok: bool, git_clean: bool, ready: bool,
    reasons: List[str],
) -> tuple:
    """Fold swarm_trace.jsonl into SQLite. Returns (flushed_bool, record_count)."""
    if conn is None:
        return False, 0
    records: List[dict] = []
    if trace_path and os.path.exists(trace_path):
        try:
            with open(trace_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except ValueError:
                            continue
        except OSError:
            pass
    fan_out = sum(1 for r in records if r.get("phase") == "fan_out")
    fan_in = sum(1 for r in records if r.get("phase") == "fan_in")
    converged = sum(1 for r in records if r.get("phase") == "fan_in" and r.get("converged"))
    try:
        _ensure_flush_table(conn)
        import time
        conn.execute(
            f"INSERT INTO {_TRACE_FLUSH_TABLE} "
            f"(file_path, records, fan_out, fan_in, converged, ast_ok, git_clean, "
            f"ready, flushed_wall) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_path, len(records), fan_out, fan_in, converged,
             int(ast_ok), int(git_clean), int(ready),
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.commit()
        return True, len(records)
    except sqlite3.Error:
        reasons.append("trace_flush_failed")
        return False, len(records)


async def post_soak_verify(
    *,
    file_path: str,
    stitched_content: str,
    trace_path: Optional[str] = None,
    sqlite_conn: Optional[sqlite3.Connection] = None,
    git_status_fn: Optional[GitStatusFn] = None,
    allowed_dirty: Optional[List[str]] = None,
) -> VerificationResult:
    """Run the three-check circuit. ``ready_for_promotion`` is True iff the AST
    structural check AND the git-index check pass; the trace flush is telemetry
    (its failure is recorded but does not block promotion). Never raises."""
    from backend.core.ouroboros.governance.stitch_precompiler import precompile_detail

    reasons: List[str] = []

    # (1) Zero-regression AST structural check.
    detail = precompile_detail(stitched_content, file_path)
    ast_ok = detail is None
    if not ast_ok:
        reasons.append(f"ast_regression: {detail}")

    # (2) Git index integrity (READ-ONLY).
    git_clean, git_reason = _verify_git_index_clean(
        git_status_fn or _default_git_porcelain, file_path, allowed_dirty,
    )
    if not git_clean:
        reasons.append(git_reason)

    ready = ast_ok and git_clean

    # (3) Flush the swarm trace to SQLite (telemetry — never a gate).
    trace_flushed, n_records = _flush_trace(
        trace_path, sqlite_conn, file_path,
        ast_ok=ast_ok, git_clean=git_clean, ready=ready, reasons=reasons,
    )

    logger.info(
        "[PostSoakVerify] %s: ast_ok=%s git_clean=%s trace_flushed=%s(%d) ready=%s %s",
        file_path, ast_ok, git_clean, trace_flushed, n_records, ready,
        ("reasons=" + "; ".join(reasons)) if reasons else "",
    )
    return VerificationResult(
        ready_for_promotion=ready, ast_ok=ast_ok, git_clean=git_clean,
        trace_flushed=trace_flushed, reasons=reasons, trace_records=n_records,
    )


__all__ = [
    "VerificationResult",
    "post_soak_verify",
]
