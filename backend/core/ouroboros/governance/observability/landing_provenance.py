"""Landing provenance -- which generated candidates LANDED, proven from git.

## Why this exists (2026-10-07)

The training corpus had no positive signal. 37 of 39 landed ops had
recorder rows and NOT ONE row said so: the recorder's outcome policy spelled
the success phase by hand and never matched (fixed at the source in
``trajectory_recorder._SUCCESS_PHASES``). That fix labels rows written from
now on. This module labels HISTORY, and it labels with a stronger fact than
the pipeline's own verdict: not "the op completed" but "this exact candidate
is the content git holds on the promoted branch".

## The proof (no inference, no similarity)

For every commit on the promotion target carrying an ``Op-ID`` trailer:

1. the files it ADDED or MODIFIED come from git (``diff-tree``), never from
   the message -- the ``Files:`` trailer is truncated past five files;
2. each file's blob at that commit has THIS op's attribution block removed
   by :func:`change_engine.strip_ouroboros_signature`, the exact inverse of
   the writer that added it;
3. ``sha256`` of the result is compared with every recorder row of that op.
   ``candidate_hash`` IS ``sha256(full_content)`` (providers, step 7), so
   equality means the row's candidate is byte-for-byte what landed.

Two git facts are recorded per label, because they answer different
questions:

* ``landed``    -- the commit is an ancestor of the promotion target;
* ``surviving`` -- its change is still there: the file exists at the target
  and is not back to the content it had before the commit. A reverted
  hollow test (``c985ccaee4``) is landed and NOT surviving; training must
  not treat it as a success.

## The label ledger

Append-only JSONL at ``<recorder events dir>/provenance/landing_labels.jsonl``
-- a SUBDIRECTORY on purpose: corpus readers glob ``*.jsonl`` in the events
dir and must never read a label as a trajectory row. Rows are hash-chained
and MAC'd with the operator's roadmap secret through the SAME helpers the
goal reconciliation ledger uses (no parallel crypto). Labels are written
only when a subject's state changes, so re-running is idempotent and a
revert appends a ``surviving: false`` record rather than rewriting history.

Never raises. Every git call is bounded (the reconciliation ledger's
timeout). Commits that cannot be proven are REPORTED with their reason
(``no_rows``, ``no_hash_match``, ``ambiguous_signature``) and never labelled.

Env:
    JARVIS_LANDING_PROVENANCE_ENABLED   default true
    JARVIS_LANDING_PROVENANCE_PATH      default <events dir>/provenance/landing_labels.jsonl
    JARVIS_LANDING_PROVENANCE_REF       default: the promotion target branch
    JARVIS_LANDING_PROVENANCE_DEPTH     default: the reconciliation ledger's scan depth
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("Ouroboros.LandingProvenance")

SCHEMA_VERSION = "landing_provenance.1"
EVENT_TYPE = "landing_provenance"

_ENV_ENABLED = "JARVIS_LANDING_PROVENANCE_ENABLED"
_ENV_PATH = "JARVIS_LANDING_PROVENANCE_PATH"
_ENV_REF = "JARVIS_LANDING_PROVENANCE_REF"
_ENV_DEPTH = "JARVIS_LANDING_PROVENANCE_DEPTH"


def enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() in ("1", "true", "yes", "on")


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    from backend.core.ouroboros.governance.observability.trajectory_recorder import events_dir
    return events_dir() / "provenance" / "landing_labels.jsonl"


def scan_depth() -> int:
    raw = os.environ.get(_ENV_DEPTH, "").strip()
    try:
        if raw and int(raw) > 0:
            return int(raw)
    except ValueError:
        pass
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import scan_depth as _depth
    return _depth()


# ---------------------------------------------------------------------------
# Git truth
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LandedFile:
    path: str
    blob_sha256: str          # sha256 of the committed content minus this op's signature
    surviving: bool


@dataclass(frozen=True)
class LandedCommit:
    sha: str
    op_id: str
    files: Tuple[LandedFile, ...]
    ambiguous: Tuple[str, ...] = ()
    committed_at: float = 0.0     # git's commit time (%ct): when it landed on the record


async def _git(args: Sequence[str], cwd: Path) -> Tuple[int, str]:
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import _git as _ledger_git
    return await _ledger_git(args, cwd)


async def _git_bytes(args: Sequence[str], cwd: Path) -> Optional[bytes]:
    """Raw stdout (blobs are hashed as BYTES' utf-8 text, exactly like the
    provider hashed ``full_content.encode()``). None on failure."""
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import git_timeout_s
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=git_timeout_s())
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return None
        return out if proc.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


async def resolve_repo_and_ref() -> Tuple[Optional[Path], str]:
    """The authoritative repo and the branch landings are PROMOTED to --
    the promoter's own resolution, so "landed" means what promotion means."""
    from backend.core.ouroboros.governance.main_promoter import _manager, resolve_target_branch
    try:
        mgr = _manager()
        root = Path(mgr._repo_root)
        explicit = os.environ.get(_ENV_REF, "").strip()
        ref = explicit or await resolve_target_branch(mgr)
        return root, ref
    except Exception:  # noqa: BLE001
        logger.debug("[LandingProvenance] repo/ref resolution failed", exc_info=True)
        return None, ""


def _op_of(body: str) -> str:
    from backend.core.ouroboros.governance.auto_committer import OP_ID_TRAILER
    m = re.search(rf"^{re.escape(OP_ID_TRAILER)}:\s*(\S+)\s*$", body or "", re.M)
    return m.group(1) if m else ""


async def landed_commits(root: Path, ref: str, *, depth: Optional[int] = None,
                         only: Optional[Iterable[str]] = None) -> List[LandedCommit]:
    """Every commit reachable from ``ref`` that names an op, with its proven files."""
    from backend.core.ouroboros.governance.change_engine import strip_ouroboros_signature

    rc, log = await _git(["log", ref, "-n", str(depth or scan_depth()),
                          "--format=%H%x00%ct%x00%B%x01"], root)
    if rc != 0:
        return []
    wanted = {s.strip() for s in (only or ()) if s and s.strip()}
    out: List[LandedCommit] = []
    for block in log.split("\x01"):
        if "\x00" not in block:
            continue
        sha, ct, body = block.strip().split("\x00", 2)
        if wanted and not any(sha.startswith(w) or w.startswith(sha) for w in wanted):
            continue
        op_id = _op_of(body)
        if not op_id:
            continue
        rc, names = await _git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--root",
                                "--diff-filter=AM", sha], root)
        if rc != 0:
            continue
        files: List[LandedFile] = []
        ambiguous: List[str] = []
        for path in [p for p in names.splitlines() if p.strip()]:
            blob = await _git_bytes(["show", f"{sha}:{path}"], root)
            if blob is None:
                continue
            stripped = strip_ouroboros_signature(blob.decode("utf-8", "replace"), op_id)
            if stripped is None:
                ambiguous.append(path)
                continue
            files.append(LandedFile(
                path=path,
                blob_sha256=hashlib.sha256(stripped.encode()).hexdigest(),
                surviving=await _surviving(root, ref, sha, path),
            ))
        out.append(LandedCommit(sha=sha, op_id=op_id, files=tuple(files), ambiguous=tuple(ambiguous),
                                committed_at=float(ct or 0)))
    return out


async def _surviving(root: Path, ref: str, sha: str, path: str) -> bool:
    """Is this commit's change still present at ``ref``? Absent file, or a
    file back to its pre-commit content, means it was undone."""
    at_ref = await _git_bytes(["rev-parse", f"{ref}:{path}"], root)
    if at_ref is None:
        return False
    before = await _git_bytes(["rev-parse", f"{sha}^:{path}"], root)
    return before is None or at_ref.strip() != before.strip()


# ---------------------------------------------------------------------------
# The corpus side
# ---------------------------------------------------------------------------

def _corpus_index(events: Path, ops: Set[str]) -> Dict[Tuple[str, str], List[str]]:
    """``(op_id, candidate_hash) -> [event_id]`` for the ops in question.
    Streams line by line: the corpus is append-only and large."""
    index: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    if not ops:
        return index          # nothing to look for: never stream the corpus for it
    for f in sorted(events.glob("experience_*.jsonl")):
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"op_id"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # a torn tail of a live append; next pass sees it whole
                    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
                    op = str(meta.get("op_id", "") or "")
                    h = str(meta.get("candidate_hash", "") or "")
                    if op in ops and h and row.get("event_id"):
                        index[(op, h)].append(str(row["event_id"]))
        except OSError as exc:
            logger.warning("[LandingProvenance] unreadable corpus file %s: %s", f, exc)
    return index


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def _secret() -> Optional[str]:
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import _roadmap_secret
    return _roadmap_secret()


def read_labels(path: Optional[Path] = None) -> Tuple[Dict[str, Dict[str, Any]], str, int]:
    """``(subject_event_id -> latest VERIFIED label, chain tip, rejected count)``.
    A row whose chain link or MAC fails is ignored -- an unverifiable label
    must never promote a row to "landed"."""
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import (
        _chain_hash, _genesis, _mac_valid,
    )
    p = path or ledger_path()
    latest: Dict[str, Dict[str, Any]] = {}
    tip = _genesis()
    rejected = 0
    secret = _secret()
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest, tip, 0
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
            payload = rec["payload"]
            ok = (rec.get("prev_hash") == tip
                  and rec.get("record_hash") == _chain_hash(tip, payload)
                  and (not secret or _mac_valid(payload, rec.get("mac", ""), secret)))
        except (ValueError, KeyError, TypeError):
            ok = False
        if not ok:
            rejected += 1
            continue
        tip = rec["record_hash"]
        latest[payload["subject_event_id"]] = payload
    return latest, tip, rejected


def _append(path: Path, payload: Dict[str, Any], tip: str) -> Optional[str]:
    from backend.core.ouroboros.governance.cross_process_jsonl import flock_append_line
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import _chain_hash, _mac
    record_hash = _chain_hash(tip, payload)
    rec = {"payload": payload, "prev_hash": tip, "record_hash": record_hash,
           "mac": _mac(payload, _secret())}
    path.parent.mkdir(parents=True, exist_ok=True)
    if flock_append_line(path, json.dumps(rec, sort_keys=True, separators=(",", ":"))):
        return record_hash
    return None


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

@dataclass
class LabelReport:
    ref: str = ""
    commits: int = 0
    proven: int = 0
    labels_written: int = 0
    unchanged: int = 0
    unproven: Dict[str, str] = field(default_factory=dict)   # sha -> reason
    rejected_ledger_rows: int = 0
    error: str = ""

    def summary(self) -> str:
        return (f"ref={self.ref} commits={self.commits} proven={self.proven} "
                f"written={self.labels_written} unchanged={self.unchanged} "
                f"unproven={len(self.unproven)} rejected_rows={self.rejected_ledger_rows}"
                + (f" error={self.error}" if self.error else ""))


async def label_landings(*, only: Optional[Iterable[str]] = None,
                         dry_run: bool = False) -> LabelReport:
    """One labeling pass (all commits, or just ``only`` shas). NEVER raises."""
    report = LabelReport()
    if not enabled():
        report.error = "disabled"
        return report
    try:
        root, ref = await resolve_repo_and_ref()
        report.ref = ref
        if root is None or not ref:
            report.error = "no promotion target resolvable"
            return report
        commits = await landed_commits(root, ref, only=only)
        report.commits = len(commits)
        from backend.core.ouroboros.governance.observability.trajectory_recorder import events_dir
        index = await asyncio.to_thread(_corpus_index, events_dir(), {c.op_id for c in commits})
        path = ledger_path()
        latest, tip, report.rejected_ledger_rows = await asyncio.to_thread(read_labels, path)
        for c in commits:
            matches = [(f, eid) for f in c.files for eid in index.get((c.op_id, f.blob_sha256), [])]
            if not matches:
                ops_rows = any(k[0] == c.op_id for k in index)
                report.unproven[c.sha[:12]] = (
                    "ambiguous_signature" if c.ambiguous and not c.files
                    else ("no_hash_match" if ops_rows else "no_rows"))
                continue
            report.proven += 1
            for f, eid in matches:
                prior = latest.get(eid)
                if prior and prior.get("commit_sha") == c.sha and prior.get("surviving") == f.surviving:
                    report.unchanged += 1
                    continue
                payload = {
                    "schema_version": SCHEMA_VERSION, "event_type": EVENT_TYPE,
                    "label_id": str(uuid.uuid4()), "subject_event_id": eid,
                    "op_id": c.op_id, "candidate_hash": f.blob_sha256,
                    "commit_sha": c.sha, "file_path": f.path, "landing_ref": ref,
                    "landed": True, "surviving": f.surviving, "committed_at": c.committed_at,
                    "labeled_at": time.time(),
                }
                if dry_run:
                    report.labels_written += 1
                    continue
                new_tip = await asyncio.to_thread(_append, path, payload, tip)
                if new_tip is None:
                    report.error = f"append failed at {path}"
                    return report
                tip = new_tip
                latest[eid] = payload
                report.labels_written += 1
    except Exception as exc:  # noqa: BLE001 -- labeling must never take a caller down
        logger.warning("[LandingProvenance] pass failed", exc_info=True)
        report.error = repr(exc)
    logger.info("[LandingProvenance] %s", report.summary())
    return report


_ENV_LIVE_DEADLINE = "JARVIS_LANDING_PROVENANCE_LIVE_DEADLINE_S"
_live_tasks: Set["asyncio.Task[Any]"] = set()


async def _label_when_rows_exist(sha: str, deadline_s: float) -> LabelReport:
    """Promotion happens at COMMIT; the recorder writes the op's rows at its
    VERDICT, a moment later. Retry with backoff until the commit is proven or
    is unprovable for a reason time will not fix, or the deadline passes."""
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    delay = 2.0
    report = LabelReport()
    while True:
        report = await label_landings(only=[sha])
        why = report.unproven.get(sha[:12])
        if report.error or report.proven or why not in ("no_rows", "no_hash_match"):
            return report
        if loop.time() + delay > end:
            logger.info("[LandingProvenance] %s still unproven (%s) after %.0fs; the next "
                        "full pass will retry", sha[:12], why, deadline_s)
            return report
        await asyncio.sleep(delay)
        delay = min(delay * 2, max(2.0, deadline_s / 4))


def schedule_live_label(sha: str) -> Optional["asyncio.Task[Any]"]:
    """Label one fresh landing in the background. Returns the task so its
    OWNER (the promotion transport) can cancel it with its own lifecycle; a
    task nobody owns outlives the loop that made it. NEVER raises, never blocks."""
    if not enabled() or not sha:
        return None
    try:
        raw = os.environ.get(_ENV_LIVE_DEADLINE, "").strip()
        deadline = float(raw) if raw else 600.0
        task = asyncio.get_running_loop().create_task(_label_when_rows_exist(sha, deadline))
        _live_tasks.add(task)              # keep a strong ref until it finishes
        task.add_done_callback(_live_tasks.discard)
        return task
    except Exception:  # noqa: BLE001
        logger.debug("[LandingProvenance] live label not scheduled", exc_info=True)
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="landing_provenance",
                                 description="Label corpus rows whose candidate landed, from git truth.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--commit", action="append", default=[], help="label only these commit shas")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # The organism's configuration -- above all the roadmap secret the ledger
    # is MAC'd with. Labels written without it would be rejected by every
    # reader that has it.
    from backend.core.env_bootstrap import load_env_once
    load_env_once()
    rep = asyncio.run(label_landings(only=args.commit or None, dry_run=args.dry_run))
    print(rep.summary())
    for sha, why in sorted(rep.unproven.items()):
        print(f"  unproven {sha}: {why}")
    return 1 if rep.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
