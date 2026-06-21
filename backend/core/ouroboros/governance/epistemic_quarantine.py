# backend/core/ouroboros/governance/epistemic_quarantine.py
"""Session-bound cross-process quarantine ledger + atomic Truth-Guard hash.

Sovereign Epistemological Context Matrix (2026-06-21), spec section 5.3.1, LR2.

The quarantine ledger is the load-bearing CROSS-PROCESS barrier that stops a
sibling ProcessPoolExecutor worker from ingesting a memory node a peer just
found stale. An in-memory set cannot cross process boundaries; an append-only
on-disk JSONL (atomic temp+rename) consulted by every worker can.

Discipline: pure stdlib, fail-open-to-fresh-read (a ledger error must NEVER
block a legitimate live read), session_id-scoped (no infinite TTL).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Tuple

logger = logging.getLogger(__name__)


def sha256_of_file(path: str) -> str:
    """Full sha256 hex of a file's bytes, or "" if unreadable. Never raises.
    Matches the convention in state_drift.py so memory + drift agree."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (OSError, IOError):
        return ""


def atomic_read_and_hash(path: str) -> Tuple[bytes, str]:
    """Read a file's bytes ONCE and hash exactly those bytes (no read->stat->
    re-read tear window). Returns (b"", "") if unreadable. Never raises.

    Atomicity guarantee: a concurrent writer either lands fully before or fully
    after this single read of the open fd; the returned digest always
    describes one coherent snapshot."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        return data, hashlib.sha256(data).hexdigest()
    except (OSError, IOError):
        return b"", ""


class QuarantineLedger:
    """Append-only, session-scoped, fail-open quarantine of stale memory nodes."""

    def __init__(self, path: str, session_id: str) -> None:
        self._path = path
        self._session_id = session_id or "unknown"

    def quarantine(self, rel_path: str, *, reason: str = "",
                   root: str = "", expected_sha: str = "") -> None:
        """Append a quarantine record for the CURRENT session. Never raises."""
        rec = {
            "session_id": self._session_id,
            "rel_path": rel_path,
            "reason": reason,
            "expected_sha": expected_sha,
            "root": root,
            "ts": time.time(),
        }
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception:  # noqa: BLE001 — quarantine is best-effort
            logger.debug("[Quarantine] append swallowed", exc_info=True)

    def _records(self) -> List[Dict]:
        """All parseable records (any session). Fail-open: returns [] on error."""
        out: List[Dict] = []
        try:
            if not os.path.exists(self._path):
                return out
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except (ValueError, TypeError):
                        continue
        except Exception:  # noqa: BLE001
            return []
        return out

    def is_quarantined(self, rel_path: str) -> bool:
        """True iff rel_path is quarantined IN THE CURRENT SESSION (LR2). A
        prior soak's quarantine is ignored. Fail-open (False) on any error."""
        for rec in self._records():
            if rec.get("session_id") == self._session_id \
                    and rec.get("rel_path") == rel_path:
                return True
        return False

    def reconcile(self, root: str) -> Dict[str, List[str]]:
        """On session terminate: re-hash each current-session quarantined node
        vs live disk. revalidated = hash now matches expected_sha (node is
        clean again); dropped = still drifted. Fail-soft. Returns the summary;
        the caller (FSM) refreshes the oracle for revalidated nodes."""
        revalidated: List[str] = []
        dropped: List[str] = []
        seen: set = set()
        for rec in self._records():
            if rec.get("session_id") != self._session_id:
                continue
            rel = rec.get("rel_path", "")
            if not rel or rel in seen:
                continue
            seen.add(rel)
            expected = rec.get("expected_sha", "")
            live = sha256_of_file(os.path.join(root, rel))
            if expected and live == expected:
                revalidated.append(rel)
            else:
                dropped.append(rel)
        return {"revalidated": revalidated, "dropped": dropped}
