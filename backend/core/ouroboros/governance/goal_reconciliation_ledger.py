"""GoalReconciliationLedger — binds landed git commits to cryptographic roadmap goals.

Why this exists (2026-09-07)
----------------------------
The first-order proof landed (``6b85a9f438`` / ``c66a92e093`` — a real,
passing test authored by the local 30B for the untested ``model_physics``
oracle) and then O+V kept re-dispatching the SAME signed goal: the roadmap
listed it, nothing recorded that it had been satisfied, and the only
cross-op memory in the organism (``EpisodicFailureMemory``) dies with its
op. Completed operator intent was re-executed as churn.

The root cause is not "the roadmap needs a status field". The operator's
roadmap is an HMAC-signed statement of INTENT and must stay immutable;
whether that intent has been SATISFIED is a fact about the repository, and
git already knows it. This ledger therefore never edits the roadmap and
never stores a status boolean. It records a BINDING — ``(goal_id,
goal_digest) -> commit_sha`` — and DERIVES the state at read time from git
reachability:

    SATISFIED   the bound commit is an ancestor of the landing ref
    ACTIVE      no binding, or the bound commit is no longer reachable
                (rolled back / amended / branch reset) — the ledger appends
                a ``reactivated`` event so the transition is audited, and
                the roadmap reader emits the goal again for corrective
                generation. That is the bi-directional sync: git truth
                drives the roadmap state in BOTH directions.

Bindings are proven three ways, all composed from existing substrates:

* **goal identity** — ``goal_digest`` = sha256 over the goal's canonical
  serialization (``roadmap_reader._canonical_serialize_for_signing``), so
  an operator who re-signs a CHANGED goal under the same id gets a fresh
  ACTIVE goal, never a stale satisfaction.
* **record integrity** — every ledger row is hash-chained
  (``provenance_ledger.compute_provenance_hash``, ``GENESIS_HASH``) and
  MAC'd with the operator's roadmap secret through the canonical
  ``roadmap_reader.compute_signature`` / ``verify_signature`` (constant
  time; no parallel crypto). A row that fails either check is IGNORED —
  fail-closed for suppression: an unverifiable "satisfied" never silences
  operator intent (duplicate work is recoverable; dropped intent is not).
* **git provenance** — the autonomous commit carries ``Roadmap-Goal:`` /
  ``Roadmap-Goal-Digest:`` trailers (spelled from THIS module so writer and
  reader cannot drift). When the ledger has no row for a goal, the landing
  ref's history is scanned for a trailer-bearing commit that touches the
  goal's declared target files and the binding is rebuilt: git is the
  truth, the ledger is the audit trail and cache.

Durable writes go through ``cross_process_jsonl.flock_append_line`` (the
canonical cross-process append). Every git call is an ``asyncio``
subprocess with a bounded timeout. NEVER raises; every failure degrades to
ACTIVE with a diagnostic.

Env (all optional, no hardcoded behaviour):
    JARVIS_GOAL_RECONCILIATION_ENABLED         default true
    JARVIS_GOAL_RECONCILIATION_LEDGER_PATH     default .jarvis/goal_reconciliation_ledger.jsonl
    JARVIS_GOAL_RECONCILIATION_LANDING_REF     default HEAD (the operator tree's promoted branch)
    JARVIS_GOAL_RECONCILIATION_REPO_ROOT       default: parent of the roadmap's .jarvis dir
    JARVIS_GOAL_RECONCILIATION_GIT_TIMEOUT_S   default 10
    JARVIS_GOAL_RECONCILIATION_SCAN_DEPTH      default 500 commits for trailer rebuild
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.GoalReconciliation")

_ENV_ENABLED = "JARVIS_GOAL_RECONCILIATION_ENABLED"
_ENV_LEDGER_PATH = "JARVIS_GOAL_RECONCILIATION_LEDGER_PATH"
_ENV_LANDING_REF = "JARVIS_GOAL_RECONCILIATION_LANDING_REF"
_ENV_REPO_ROOT = "JARVIS_GOAL_RECONCILIATION_REPO_ROOT"
_ENV_GIT_TIMEOUT = "JARVIS_GOAL_RECONCILIATION_GIT_TIMEOUT_S"
_ENV_SCAN_DEPTH = "JARVIS_GOAL_RECONCILIATION_SCAN_DEPTH"
_ENV_INFLIGHT_TTL = "JARVIS_GOAL_RECONCILIATION_INFLIGHT_TTL_S"
_ENV_PIPELINE_TIMEOUT = "JARVIS_PIPELINE_TIMEOUT_S"
_ENV_SESSION = "JARVIS_OUROBOROS_SESSION_ID"   # same anchor the auto-commit trailer uses

_DEFAULT_LEDGER_REL = ".jarvis/goal_reconciliation_ledger.jsonl"
_DEFAULT_LANDING_REF = "HEAD"
_DEFAULT_GIT_TIMEOUT_S = 10.0
_DEFAULT_SCAN_DEPTH = 500

#: Commit-body trailers. The auto-committer WRITES these and this module
#: READS them — one spelling, one owner.
ROADMAP_GOAL_TRAILER = "Roadmap-Goal"
ROADMAP_GOAL_DIGEST_TRAILER = "Roadmap-Goal-Digest"

SCHEMA_VERSION = "goal_reconciliation.v1"


# ---------------------------------------------------------------------------
# Env
# ---------------------------------------------------------------------------

def enabled() -> bool:
    raw = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def ledger_path() -> Path:
    """Durable ledger location. Resolved through ``workspace_resolver.
    resolve_durable_path`` — the SAME re-anchor ``flock_append_line`` applies
    on write — so reads and writes agree under a per-run durable root."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        p = Path(raw).expanduser()
    else:
        # Colocated with the roadmap reader's ledger: whoever isolates that
        # (every reader test does) isolates this one — production rows are
        # never polluted by a test that forgot an env var.
        try:
            from backend.core.ouroboros.governance.roadmap_reader import ledger_path as _rr_ledger
            p = _rr_ledger().parent / Path(_DEFAULT_LEDGER_REL).name
        except Exception:  # noqa: BLE001
            p = Path(_DEFAULT_LEDGER_REL)
    try:
        from backend.core.ouroboros.governance.workspace_resolver import resolve_durable_path
        return Path(resolve_durable_path(p))
    except Exception:  # noqa: BLE001
        return p


def landing_ref() -> str:
    raw = os.environ.get(_ENV_LANDING_REF, "").strip()
    return raw or _DEFAULT_LANDING_REF


def git_timeout_s() -> float:
    try:
        v = float(os.environ.get(_ENV_GIT_TIMEOUT, "").strip() or _DEFAULT_GIT_TIMEOUT_S)
        return v if v > 0 else _DEFAULT_GIT_TIMEOUT_S
    except ValueError:
        return _DEFAULT_GIT_TIMEOUT_S


def scan_depth() -> int:
    try:
        v = int(os.environ.get(_ENV_SCAN_DEPTH, "").strip() or _DEFAULT_SCAN_DEPTH)
        return v if v > 0 else _DEFAULT_SCAN_DEPTH
    except ValueError:
        return _DEFAULT_SCAN_DEPTH


def current_session() -> str:
    """The running organism's session id (``""`` outside a soak/cockpit)."""
    return os.environ.get(_ENV_SESSION, "").strip()


def inflight_ttl_s() -> float:
    """How long a dispatched op keeps its goal out of re-emission when no
    terminal event arrives (a crashed op must not block forever). Default:
    twice the pipeline wall (``JARVIS_PIPELINE_TIMEOUT_S``)."""
    raw = os.environ.get(_ENV_INFLIGHT_TTL, "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    try:
        return 2.0 * max(1.0, float(os.environ.get(_ENV_PIPELINE_TIMEOUT, "").strip() or 600.0))
    except ValueError:
        return 1200.0


def repo_root() -> Path:
    """The tree whose ``landing_ref`` proves satisfaction. Env override,
    else the parent of the roadmap's ``.jarvis`` directory (the roadmap
    lives at ``<repo>/.jarvis/roadmap.yaml`` by contract)."""
    raw = os.environ.get(_ENV_REPO_ROOT, "").strip()
    if raw:
        return Path(raw).expanduser()
    try:
        from backend.core.ouroboros.governance.roadmap_reader import roadmap_path
        return roadmap_path().resolve().parent.parent
    except Exception:  # noqa: BLE001
        return Path.cwd()


# ---------------------------------------------------------------------------
# Identity + crypto (composed, never reimplemented)
# ---------------------------------------------------------------------------

def _canonical(payload: Mapping[str, Any]) -> bytes:
    from backend.core.ouroboros.governance.roadmap_reader import (
        _canonical_serialize_for_signing,
    )
    return _canonical_serialize_for_signing(payload)


def goal_digest(goal: Any) -> str:
    """sha256 over the goal's canonical serialization — the cryptographic
    identity a commit binds to. Accepts a ``RoadmapGoal`` or its dict. ``""``
    on any fault."""
    try:
        data = goal.to_dict() if hasattr(goal, "to_dict") else dict(goal)
        data = {k: v for k, v in data.items() if k != "schema_version"}
        return hashlib.sha256(_canonical(data)).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


def _mac(payload: Mapping[str, Any], secret: Optional[str]) -> str:
    if not secret:
        return ""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import compute_signature
        return compute_signature(payload, secret)
    except Exception:  # noqa: BLE001
        return ""


def _mac_valid(payload: Mapping[str, Any], mac: str, secret: Optional[str]) -> bool:
    if not secret or not mac:
        return False
    try:
        from backend.core.ouroboros.governance.roadmap_reader import verify_signature
        return verify_signature(payload, mac, secret)
    except Exception:  # noqa: BLE001
        return False


def _roadmap_secret() -> Optional[str]:
    try:
        from backend.core.ouroboros.governance.roadmap_reader import hmac_secret
        return hmac_secret()
    except Exception:  # noqa: BLE001
        return None


def _signature_required() -> bool:
    try:
        from backend.core.ouroboros.governance.roadmap_reader import require_signature
        return require_signature()
    except Exception:  # noqa: BLE001
        return True


def _chain_hash(prev_hash: str, payload: Dict[str, Any]) -> str:
    from backend.core.ouroboros.governance.provenance_ledger import compute_provenance_hash
    return compute_provenance_hash(prev_hash, payload)


def _genesis() -> str:
    from backend.core.ouroboros.governance.command_node.biometric_audit_ledger import GENESIS_HASH
    return GENESIS_HASH


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

class GoalState(str, enum.Enum):
    ACTIVE = "active"
    SATISFIED = "satisfied"


class ReconciliationEvent(str, enum.Enum):
    SATISFIED = "satisfied"
    REACTIVATED = "reactivated"
    DISPATCHED = "dispatched"   # an op for this goal was emitted (op_id)
    TERMINAL = "terminal"       # that op reached a terminal state


_PAYLOAD_FIELDS = ("event", "goal_id", "goal_digest", "commit_sha", "landing_ref", "op_id", "ts")


@dataclass(frozen=True)
class ReconciliationRecord:
    event: str
    goal_id: str
    goal_digest: str
    commit_sha: str
    landing_ref: str
    op_id: str
    ts: float
    prev_hash: str
    record_hash: str
    mac: str
    schema_version: str = SCHEMA_VERSION
    #: Process session that wrote the row. NOT part of the MAC'd payload: an
    #: attacker who forges it can only make a dispatch look stale (a
    #: duplicate op), never hide a goal (fail-closed for suppression).
    session: str = ""

    def payload(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in _PAYLOAD_FIELDS}

    def to_dict(self) -> Dict[str, Any]:
        d = self.payload()
        d.update({
            "prev_hash": self.prev_hash, "record_hash": self.record_hash,
            "mac": self.mac, "schema_version": self.schema_version,
            "session": self.session,
        })
        return d


@dataclass(frozen=True)
class GoalReconciliation:
    goal_id: str
    state: GoalState
    commit_sha: str = ""
    verified: bool = False
    diagnostic: str = ""
    #: A dispatched, non-terminal op currently serving this goal — the
    #: reader must not emit a second one (duplicate ops on the same files
    #: shed each other on STATE DRIFT).
    in_flight_op: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id[:128], "state": self.state.value,
            "commit_sha": self.commit_sha[:64], "verified": bool(self.verified),
            "diagnostic": self.diagnostic[:256], "in_flight_op": self.in_flight_op[:128],
        }


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------

def _read_lines(path: Path) -> List[Dict[str, Any]]:
    try:
        if not path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
    except Exception:  # noqa: BLE001
        return []


def read_records(path: Optional[Path] = None, *, secret: Optional[str] = None) -> Tuple[ReconciliationRecord, ...]:
    """Every VALID record in chain order. A row whose chain link is broken
    or whose MAC fails (when a secret is known / required) is dropped
    together with everything after it — a chain is only as good as its
    last verified link. NEVER raises."""
    target = path or ledger_path()
    secret = secret if secret is not None else _roadmap_secret()
    require = _signature_required()
    records: List[ReconciliationRecord] = []
    prev = _genesis()
    for row in _read_lines(target):
        try:
            payload = {k: row.get(k) for k in _PAYLOAD_FIELDS}
            payload["ts"] = float(payload.get("ts") or 0.0)
            for k in _PAYLOAD_FIELDS:
                if k != "ts":
                    payload[k] = str(payload.get(k) or "")
            if str(row.get("prev_hash", "")) != prev:
                logger.warning("[GoalReconciliation] chain break at %s — ignoring tail", row.get("record_hash", "?")[:12])
                break
            expected = _chain_hash(prev, payload)
            if str(row.get("record_hash", "")) != expected:
                logger.warning("[GoalReconciliation] record hash mismatch — ignoring tail")
                break
            mac = str(row.get("mac", "") or "")
            if secret or require:
                if not _mac_valid(payload, mac, secret):
                    logger.warning("[GoalReconciliation] MAC invalid for goal=%s — ignoring tail", payload["goal_id"])
                    break
            rec = ReconciliationRecord(
                prev_hash=prev, record_hash=expected, mac=mac,
                session=str(row.get("session", "") or ""), **payload,
            )
            records.append(rec)
            prev = expected
        except Exception:  # noqa: BLE001
            break
    return tuple(records)


def _append(record: ReconciliationRecord, path: Path) -> bool:
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import flock_append_line
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(flock_append_line(path, json.dumps(record.to_dict(), sort_keys=True)))
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] append degraded", exc_info=True)
        return False


def _append_unlocked(record: ReconciliationRecord, path: Path) -> bool:
    """Append one JSONL row while the caller HOLDS the ledger flock."""
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        logger.debug("[GoalReconciliation] append (locked) degraded", exc_info=True)
        return False


def _ledger_has_rows(path: Path) -> bool:
    try:
        return path.is_file() and any(l.strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False


def append_linked(
    build: "Callable[[str], ReconciliationRecord]", path: Path, secret: Optional[str],
) -> Optional[ReconciliationRecord]:
    """Read the verified chain head, build the record ON that head and append
    it — all under the ledger's cross-process flock, so two concurrent
    writers can never both link to the same head (the chain-break class
    observed 2026-09-07). Fail-CLOSED: a non-empty ledger whose rows cannot
    be verified (wrong/missing secret, corruption) is never appended to —
    appending at genesis on top of it would break the chain for everyone.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import flock_critical_section
        path.parent.mkdir(parents=True, exist_ok=True)
        with flock_critical_section(path) as acquired:
            if not acquired:
                logger.warning("[GoalReconciliation] ledger lock busy — event not recorded")
                return None
            existing = read_records(path, secret=secret)
            if not existing and _ledger_has_rows(path):
                logger.warning("[GoalReconciliation] ledger has rows but none verify (secret/corruption) — refusing to append; run repair_chain()")
                return None
            prev = existing[-1].record_hash if existing else _genesis()
            rec = build(prev)
            # The section already holds the ledger's flock; flock_append_line
            # would try to take it again (nested acquisition times out).
            return rec if _append_unlocked(rec, path) else None
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] append_linked degraded", exc_info=True)
        return None


def repair_chain(
    path: Optional[Path] = None, *, secret: Optional[str] = None,
    keep: "Optional[Callable[[Dict[str, Any]], bool]]" = None,
) -> int:
    """Operator repair: re-link every row (optionally filtered by ``keep``)
    into a fresh chain from genesis and re-MAC with the roadmap secret,
    atomically under the flock. Returns the number of rows kept. Payload
    fields are preserved verbatim; only prev_hash/record_hash/mac change.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import flock_critical_section
        target = path or ledger_path()
        secret = secret if secret is not None else _roadmap_secret()
        with flock_critical_section(target) as acquired:
            if not acquired:
                return 0
            rows = [r for r in _read_lines(target) if keep is None or keep(r)]
            out: List[ReconciliationRecord] = []
            prev = _genesis()
            for row in rows:
                payload = {k: row.get(k) for k in _PAYLOAD_FIELDS}
                payload["ts"] = float(payload.get("ts") or 0.0)
                for k in _PAYLOAD_FIELDS:
                    if k != "ts":
                        payload[k] = str(payload.get(k) or "")
                rec = ReconciliationRecord(
                    prev_hash=prev, record_hash=_chain_hash(prev, payload), mac=_mac(payload, secret),
                    session=str(row.get("session", "") or ""), **payload,
                )
                out.append(rec)
                prev = rec.record_hash
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text("".join(json.dumps(r.to_dict(), sort_keys=True) + "\n" for r in out), encoding="utf-8")
            os.replace(tmp, target)
            logger.warning("[GoalReconciliation] chain repaired: %d row(s) re-linked at %s", len(out), target)
            return len(out)
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] repair_chain degraded", exc_info=True)
        return 0


def _make_record(
    *, event: ReconciliationEvent, goal_id: str, goal_digest_hex: str,
    commit_sha: str, op_id: str, prev_hash: str, secret: Optional[str],
    ts: Optional[float] = None,
) -> ReconciliationRecord:
    payload = {
        "event": event.value, "goal_id": goal_id[:128], "goal_digest": goal_digest_hex[:64],
        "commit_sha": commit_sha[:64], "landing_ref": landing_ref(), "op_id": op_id[:128],
        "ts": float(ts if ts is not None else time.time()),
    }
    return ReconciliationRecord(
        prev_hash=prev_hash, record_hash=_chain_hash(prev_hash, payload),
        mac=_mac(payload, secret), session=current_session(), **payload,
    )


async def record_landing(
    *, goal_id: str, goal_digest_hex: str, commit_sha: str, op_id: str,
    path: Optional[Path] = None, secret: Optional[str] = None,
) -> Optional[ReconciliationRecord]:
    """Bind a landed autonomous commit to its signed goal. Called from the
    commit path; the resulting state is still DERIVED (the sha must become
    reachable from the landing ref). NEVER raises."""
    if not enabled() or not goal_id or not commit_sha:
        return None
    try:
        target = path or ledger_path()
        secret = secret if secret is not None else _roadmap_secret()
        rec = await asyncio.to_thread(
            append_linked,
            lambda prev: _make_record(
                event=ReconciliationEvent.SATISFIED, goal_id=goal_id,
                goal_digest_hex=goal_digest_hex, commit_sha=commit_sha, op_id=op_id,
                prev_hash=prev, secret=secret,
            ),
            target, secret,
        )
        if rec:
            logger.info("[GoalReconciliation] bound goal=%s -> %s (op=%s)", goal_id, commit_sha[:12], op_id[:12])
        return rec
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] record_landing degraded", exc_info=True)
        return None


async def _append_event(
    *, event: ReconciliationEvent, goal_id: str, goal_digest_hex: str, op_id: str,
    path: Optional[Path], secret: Optional[str],
) -> Optional[ReconciliationRecord]:
    if not enabled() or not goal_id or not op_id:
        return None
    try:
        target = path or ledger_path()
        secret = secret if secret is not None else _roadmap_secret()
        return await asyncio.to_thread(
            append_linked,
            lambda prev: _make_record(
                event=event, goal_id=goal_id, goal_digest_hex=goal_digest_hex,
                commit_sha="", op_id=op_id, prev_hash=prev, secret=secret,
            ),
            target, secret,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] %s event degraded", event.value, exc_info=True)
        return None


async def record_dispatch(*, goal_id: str, goal_digest_hex: str, op_id: str, path: Optional[Path] = None, secret: Optional[str] = None) -> Optional[ReconciliationRecord]:
    """An op was emitted for this goal: hold the goal out of re-emission
    until that op is terminal or the in-flight TTL lapses. NEVER raises."""
    rec = await _append_event(event=ReconciliationEvent.DISPATCHED, goal_id=goal_id, goal_digest_hex=goal_digest_hex, op_id=op_id, path=path, secret=secret)
    if rec:
        logger.info("[GoalReconciliation] goal=%s dispatched as op=%s (in flight)", goal_id, op_id[:12])
    return rec


async def record_terminal(*, goal_id: str, op_id: str, outcome: str = "", path: Optional[Path] = None, secret: Optional[str] = None) -> Optional[ReconciliationRecord]:
    """The op serving this goal reached a terminal state (any outcome); the
    goal may be re-emitted unless a landing satisfied it. NEVER raises."""
    rec = await _append_event(event=ReconciliationEvent.TERMINAL, goal_id=goal_id, goal_digest_hex="", op_id=op_id, path=path, secret=secret)
    if rec:
        logger.info("[GoalReconciliation] goal=%s op=%s terminal (%s)", goal_id, op_id[:12], outcome or "-")
    return rec


def in_flight_op(records: Sequence[ReconciliationRecord], goal_id: str, *, now_ts: Optional[float] = None) -> str:
    """The op id of a dispatched-but-not-terminal op for *goal_id* younger
    than the in-flight TTL, else ``""``."""
    now = float(now_ts if now_ts is not None else time.time())
    ttl = inflight_ttl_s()
    session = current_session()
    terminal = {r.op_id for r in records if r.goal_id == goal_id and r.event == ReconciliationEvent.TERMINAL.value}
    for rec in reversed(records):
        if rec.goal_id != goal_id or rec.event != ReconciliationEvent.DISPATCHED.value:
            continue
        if rec.op_id in terminal:
            continue
        # A dispatch written by ANOTHER process session is dead with that
        # process (an op that survived via fsm_resume is re-dispatched in
        # this session and shows up as a newer row).
        if session and rec.session and rec.session != session:
            continue
        if now - float(rec.ts) <= ttl:
            return rec.op_id
    return ""


# ---------------------------------------------------------------------------
# Git truth
# ---------------------------------------------------------------------------

async def _git(args: Sequence[str], cwd: Path) -> Tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=git_timeout_s())
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, ""
        return int(proc.returncode or 0), out.decode(errors="replace")
    except Exception:  # noqa: BLE001
        return -1, ""


async def is_reachable(commit_sha: str, *, cwd: Path, ref: Optional[str] = None) -> Optional[bool]:
    """``True``/``False`` for ancestry, ``None`` when git could not answer
    (unknown sha, no repo, timeout)."""
    if not commit_sha:
        return None
    rc, _ = await _git(["cat-file", "-e", f"{commit_sha}^{{commit}}"], cwd)
    if rc != 0:
        return False if rc == 1 else None
    rc, _ = await _git(["merge-base", "--is-ancestor", commit_sha, ref or landing_ref()], cwd)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def parse_trailers(body: str) -> Dict[str, str]:
    """``Key: value`` trailer lines from a commit body (last wins)."""
    out: Dict[str, str] = {}
    for line in (body or "").splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip()
        if k in (ROADMAP_GOAL_TRAILER, ROADMAP_GOAL_DIGEST_TRAILER):
            out[k] = v.strip()
    return out


async def find_landed_commit(
    goal_id: str, goal_digest_hex: str, target_files: Sequence[str], *, cwd: Path,
) -> str:
    """Newest commit on the landing ref whose trailers name this goal AND
    digest and which touches one of the goal's declared target files (or
    any file when the goal declares none). ``""`` when nothing qualifies."""
    rc, out = await _git(
        ["log", landing_ref(), f"-n{scan_depth()}", "--format=%H%x00%B%x1e"], cwd,
    )
    if rc != 0:
        return ""
    wanted = {os.path.normpath(p) for p in target_files if p}
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if "\x00" not in chunk:
            continue
        sha, _, body = chunk.partition("\x00")
        sha = sha.strip()
        tr = parse_trailers(body)
        if tr.get(ROADMAP_GOAL_TRAILER) != goal_id:
            continue
        if goal_digest_hex and tr.get(ROADMAP_GOAL_DIGEST_TRAILER, "") != goal_digest_hex:
            continue
        if wanted:
            rc2, files = await _git(["show", "--pretty=format:", "--name-only", sha], cwd)
            if rc2 != 0:
                continue
            touched = {os.path.normpath(f.strip()) for f in files.splitlines() if f.strip()}
            if not (touched & wanted):
                continue
        return sha
    return ""


# ---------------------------------------------------------------------------
# Reconciliation (the derived state machine)
# ---------------------------------------------------------------------------

def _latest_binding(records: Sequence[ReconciliationRecord], goal_id: str, digest: str) -> Optional[ReconciliationRecord]:
    for rec in reversed(records):
        if rec.goal_id == goal_id and (not digest or rec.goal_digest == digest):
            return rec
    return None


async def reconcile_goal(
    goal: Any, *, cwd: Optional[Path] = None, path: Optional[Path] = None,
    secret: Optional[str] = None, records: Optional[Sequence[ReconciliationRecord]] = None,
) -> GoalReconciliation:
    """Derive one goal's state from the ledger + git. Appends a
    ``reactivated`` row when a satisfied binding is no longer reachable
    (rollback/amend), and a rebuilt ``satisfied`` row when git history
    carries a qualifying trailer the ledger lacks. NEVER raises."""
    goal_id = str(getattr(goal, "goal_id", "") or (goal.get("goal_id") if isinstance(goal, dict) else "") or "")
    if not enabled():
        return GoalReconciliation(goal_id=goal_id, state=GoalState.ACTIVE, diagnostic="reconciliation disabled")
    try:
        cwd = cwd or repo_root()
        target = path or ledger_path()
        secret = secret if secret is not None else _roadmap_secret()
        digest = goal_digest(goal)
        recs = list(records) if records is not None else list(await asyncio.to_thread(read_records, target, secret=secret))
        latest = _latest_binding(recs, goal_id, digest)

        if latest is not None and latest.event == ReconciliationEvent.SATISFIED.value:
            reach = await is_reachable(latest.commit_sha, cwd=cwd)
            if reach is True:
                return GoalReconciliation(goal_id, GoalState.SATISFIED, latest.commit_sha, True, "bound commit reachable from landing ref")
            if reach is None:
                return GoalReconciliation(goal_id, GoalState.ACTIVE, latest.commit_sha, False, "git could not verify binding — treating as active")
            # Bi-directional sync: the landed commit vanished (reset / amend /
            # branch rewind). Audit the transition, then fall through to the
            # trailer scan — an amend that KEPT the work re-binds to the new sha.
            rec = await asyncio.to_thread(
                append_linked,
                lambda prev: _make_record(
                    event=ReconciliationEvent.REACTIVATED, goal_id=goal_id, goal_digest_hex=digest,
                    commit_sha=latest.commit_sha, op_id="reconcile", prev_hash=prev, secret=secret,
                ),
                target, secret,
            )
            if rec:
                recs.append(rec)
            logger.warning("[GoalReconciliation] goal=%s REACTIVATED — %s no longer reachable from %s", goal_id, latest.commit_sha[:12], landing_ref())

        sha = await find_landed_commit(goal_id, digest, tuple(getattr(goal, "target_files", ()) or ()), cwd=cwd)
        if sha:
            rec = await asyncio.to_thread(
                append_linked,
                lambda prev: _make_record(
                    event=ReconciliationEvent.SATISFIED, goal_id=goal_id, goal_digest_hex=digest,
                    commit_sha=sha, op_id="rebuilt-from-git", prev_hash=prev, secret=secret,
                ),
                target, secret,
            )
            if rec:
                logger.info("[GoalReconciliation] goal=%s SATISFIED by trailer-bearing %s (rebuilt from git)", goal_id, sha[:12])
            return GoalReconciliation(goal_id, GoalState.SATISFIED, sha, True, "trailer-bearing commit reachable from landing ref")
        diag = "no binding" if latest is None else f"last event={latest.event}"
        flying = in_flight_op(recs, goal_id)
        if flying:
            return GoalReconciliation(goal_id, GoalState.ACTIVE, "", False, f"in flight as {flying[:12]}", in_flight_op=flying)
        return GoalReconciliation(goal_id, GoalState.ACTIVE, "", False, diag)
    except Exception as exc:  # noqa: BLE001
        return GoalReconciliation(goal_id=goal_id, state=GoalState.ACTIVE, diagnostic=f"reconcile degraded: {exc!r}"[:200])


async def reconcile(
    goals: Sequence[Any], *, cwd: Optional[Path] = None, path: Optional[Path] = None,
    secret: Optional[str] = None,
) -> Dict[str, GoalReconciliation]:
    """States for every goal, sharing one ledger read. NEVER raises."""
    out: Dict[str, GoalReconciliation] = {}
    if not goals:
        return out
    try:
        target = path or ledger_path()
        secret = secret if secret is not None else _roadmap_secret()
        recs = await asyncio.to_thread(read_records, target, secret=secret) if enabled() else ()
        for goal in goals:
            r = await reconcile_goal(goal, cwd=cwd, path=target, secret=secret, records=recs)
            out[r.goal_id] = r
            if r.state is GoalState.SATISFIED or r.diagnostic.startswith("last event"):
                # a reconcile may have appended; refresh the shared view
                recs = await asyncio.to_thread(read_records, target, secret=secret)
    except Exception:  # noqa: BLE001
        logger.debug("[GoalReconciliation] reconcile degraded", exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Envelope binding (commit path helper)
# ---------------------------------------------------------------------------

def superseding_op(goal_id: str, op_id: str, *, path: Optional[Path] = None, secret: Optional[str] = None) -> str:
    """When *goal_id* already has a LIVE op other than *op_id*, return that
    op's id — the caller (fsm_resume re-injection) must not revive a second
    op for the same goal (duplicates shed each other on STATE DRIFT).
    ``""`` otherwise. NEVER raises."""
    try:
        if not enabled() or not goal_id:
            return ""
        live = in_flight_op(read_records(path or ledger_path(), secret=secret), goal_id)
        return live if live and live != op_id else ""
    except Exception:  # noqa: BLE001
        return ""


def binding_from_evidence(evidence_json: str) -> Tuple[str, str]:
    """``(goal_id, goal_digest)`` from an op's ``intake_evidence_json`` —
    the roadmap reader stamps both into every goal envelope. ``("", "")``
    for non-roadmap ops or malformed evidence. NEVER raises."""
    try:
        ev = json.loads(evidence_json or "{}")
        if not isinstance(ev, dict):
            return "", ""
        return str(ev.get("goal_id") or "")[:128], str(ev.get("goal_digest") or "")[:64]
    except Exception:  # noqa: BLE001
        return "", ""


def trailer_lines(goal_id: str, goal_digest_hex: str) -> Tuple[str, ...]:
    """The commit-body trailers that make a landing self-describing."""
    if not goal_id:
        return ()
    lines = [f"{ROADMAP_GOAL_TRAILER}: {goal_id[:128]}"]
    if goal_digest_hex:
        lines.append(f"{ROADMAP_GOAL_DIGEST_TRAILER}: {goal_digest_hex[:64]}")
    return tuple(lines)
