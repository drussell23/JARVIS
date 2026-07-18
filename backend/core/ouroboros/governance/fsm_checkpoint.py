"""Autonomous FSM Checkpointing -- Suspend & Resume state hydrator.

Makes the Ouroboros cognitive loop invincible to time limits AND cloud Spot
preemption WITHOUT a trickable wall: when the blind wall-clock cap (or a SIGTERM
preemption) fires, the in-flight op's FSM phase + goal + accumulated tool/exploration
history are serialized to the ``.ouroboros/checkpoints`` ledger and the process exits
gracefully. On the next ignition the intake re-injects each pending checkpoint WITH
its preserved exploration context, so the DAG resumes where it left off instead of
re-paying the explore-from-scratch cost.

Pure data layer + fail-soft I/O -- no orchestrator/policy imports (authority-free,
like the other observability ledgers).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Cryptographic State Verification (anti-corruption, fail-closed)
# ---------------------------------------------------------------------------

def _checkpoint_key(base_dir: "Optional[str]" = None) -> bytes:
    """Locally-derived HMAC session key. ``JARVIS_CHECKPOINT_HMAC_SECRET`` if set
    (driver-provisioned, like the signed roadmap), else a persisted local key
    (generated ONCE via os.urandom at ``.ouroboros/checkpoint_key``, 0600). A resume
    across ignitions on the SAME host verifies against the same persisted key.
    NEVER raises (falls back to a process-stable default)."""
    env = os.environ.get("JARVIS_CHECKPOINT_HMAC_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    try:
        d = checkpoint_dir(base_dir)
        key_path = os.path.join(os.path.dirname(d), "checkpoint_key")
        if os.path.isfile(key_path):
            with open(key_path, "rb") as fh:
                k = fh.read().strip()
                if k:
                    return k
        k = hashlib.sha256(os.urandom(32)).hexdigest().encode("ascii")
        tmp = key_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(k)
        try:
            os.chmod(tmp, 0o600)
        except Exception:  # noqa: BLE001
            pass
        os.replace(tmp, key_path)
        return k
    except Exception:  # noqa: BLE001
        return b"jarvis-checkpoint-fallback-key"


def _sign(payload_json: str, key: bytes) -> str:
    return hmac.new(key, payload_json.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify(payload_json: str, signature: str, key: bytes) -> bool:
    """Constant-time HMAC verify. Fail-closed on any defect. NEVER raises."""
    try:
        if not payload_json or not signature:
            return False
        return hmac.compare_digest(_sign(payload_json, key), str(signature))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Atomic Hydration Handshake (strict cross-window observability)
# ---------------------------------------------------------------------------
# The window-1 (suspend) -> window-2 (resume) transition must be cryptographically
# LEGIBLE, never silent. The handshake is forced to BOTH the module logger (debug.log,
# greppable) AND stdout (the operator console) -- mirroring the streaming token
# emitter's stdout discipline. Two facets: the crypto-verify handshake (proving the
# checkpoint is authentic) and the prefill-inject handshake (proving the exact partial
# thought that re-enters the LLM). Observability only -- authority-free, never raises.

def emit_handshake(line: str) -> None:
    """Emit one Atomic Hydration Handshake line to BOTH the logger and stdout.
    Best-effort on every leg -- observability never breaks suspend/resume."""
    try:
        logger.info(line)
    except Exception:  # noqa: BLE001
        pass
    try:
        import sys  # noqa: PLC0415
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass


def _handshake_snippet(text: str, *, head: int = 48, tail: int = 24) -> str:
    """A bounded but EXACT (repr) snippet of the partial -- full repr when short, an
    unambiguous head…tail elision when long (repr preserves newlines/escapes so the
    operator sees the true bytes, not a lossy print)."""
    text = text or ""
    if len(text) <= head + tail + 1:
        return repr(text)
    return "%s … %s" % (repr(text[:head]), repr(text[-tail:]))


def format_verified_handshake(op_id: str, phase: str, signature: str) -> str:
    """Strict cryptographic handshake: the checkpoint's HMAC-SHA256 signature VERIFIED
    (fail-closed gate passed). Carries a digest prefix so two windows can be correlated."""
    return (
        "[HYDRATION-HANDSHAKE] HMAC-SHA256 VERIFIED op=%s phase=%s digest=%s… "
        "-> checkpoint AUTHENTIC (fail-closed crypto gate passed)"
        % (op_id, phase, str(signature)[:16])
    )


def format_prefill_handshake(op_id: str, partial: str) -> str:
    """Strict prefill-injection handshake: the EXACT byte-length + char-length + repr
    snippet of the partial_completion being injected into the LLM as the assistant
    prefill (the thought the 32B resumes typing from)."""
    partial = partial or ""
    n_bytes = len(partial.encode("utf-8"))
    return (
        "[HYDRATION-HANDSHAKE] PREFILL-INJECT op=%s partial_bytes=%d partial_chars=%d "
        "snippet=%s -> injected as assistant prefill (32B continues the thought)"
        % (op_id, n_bytes, len(partial), _handshake_snippet(partial))
    )


def checkpoint_dir(base_dir: "Optional[str]" = None) -> str:
    """Resolve the checkpoint ledger dir (env ``JARVIS_CHECKPOINT_DIR`` or
    ``<base>/.ouroboros/checkpoints``). Created on demand. NEVER raises."""
    try:
        if base_dir:
            d = os.path.join(base_dir, ".ouroboros", "checkpoints")
        else:
            d = os.environ.get(
                "JARVIS_CHECKPOINT_DIR",
                os.path.join(".ouroboros", "checkpoints"),
            )
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001
        return os.path.join(".ouroboros", "checkpoints")


@dataclass
class FSMCheckpoint:
    """A serialized suspend-point of one in-flight op."""
    op_id: str
    phase: str
    goal_description: str = ""
    target_files: List[str] = field(default_factory=list)
    tool_history: List[Dict[str, Any]] = field(default_factory=list)
    exploration_records: List[Dict[str, Any]] = field(default_factory=list)
    intake_evidence_json: str = ""
    provider_route: str = ""
    # Partial-Thought Buffer: the exact raw string the LLM had emitted when the
    # stream was cooperatively interrupted (may be half a tool call / code block).
    # Window-2 resume prefills this so the model continues from the interrupted char.
    partial_completion: str = ""
    # Distributed Trace Lineage -- the op's span identity across windows:
    # origin_goal_id (the window-1 causal_id), origin_source (roadmap/sensor/...),
    # emit_source + emit_wall_ts (the original emit hop evidence), resume_count.
    # Rides INSIDE the HMAC-signed payload => tamper-evident nametag.
    trace_lineage: Dict[str, Any] = field(default_factory=dict)
    # Persisted creation timestamp (epoch seconds) -- the TTL expiry keys off
    # THIS field. Real checkpoints always get an explicit time.time() from
    # capture_from_context(); a default_factory (not a static 0.0) means a
    # test/caller that omits it still gets a sane "now" rather than looking
    # instantly TTL-expired (epoch 0).
    created_at: float = field(default_factory=time.time)
    resume_reason: str = ""
    # Repository HEAD sha at suspend time. On resume, the hydrator compares this
    # against the LIVE HEAD; if the repo moved (a merge/commit landed while the op
    # was suspended), the op's stored exploration/partial-completion is stale code
    # context, so the resume is demoted to PLAN to re-plan against current source
    # rather than fast-forwarding into corruption. Empty = drift-check skipped.
    repo_sha: str = ""
    # Poison-Pill guard: how many times this checkpoint has been hydrated (a
    # resume ATTEMPTED). Incremented + re-signed on disk BEFORE each re-inject,
    # so a checkpoint that deterministically crashes the pipeline on resume can't
    # loop forever — after the attempt ceiling it is shunted to the quarantine
    # DLQ. Rides inside the HMAC-signed payload → the count is tamper-evident.
    hydration_attempt_count: int = 0
    # Atomic Workspace Checkpoint: raw ``git stash create`` SHA of the dirty
    # working-tree delta at suspend time. Bound INSIDE the HMAC payload (tamper-
    # evident). On hydration the delta is re-applied by this SHA, so an op
    # suspended MID-MUTATION resumes against its own uncommitted changes even if
    # the tree was cleaned across the boundary (rollback / worktree teardown /
    # a cloud node's fresh checkout). Empty = the tree was clean at suspend.
    workspace_stash_ref: str = ""
    schema_version: int = _SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, blob: str) -> "FSMCheckpoint":
        data = json.loads(blob)
        known = {k: data.get(k) for k in cls.__dataclass_fields__ if k in data}  # type: ignore[attr-defined]
        return cls(**known)


# Partial-Thought side-channel: the streaming layer knows the partial string but not
# the op_id; the dispatch layer knows the op_id but not the partial. The dispatch
# stashes the interrupted partial keyed by op_id here; capture_from_context (invoked
# by the harness suspend, which has the ctx/op_id) pops it into the checkpoint.
_PARTIAL_STASH: Dict[str, str] = {}


def stash_partial(op_id: str, partial: str) -> None:
    """Record the partial thought of a cooperatively-interrupted op (by op_id) so the
    suspend capture can fold it into the checkpoint. NEVER raises."""
    try:
        if op_id:
            _PARTIAL_STASH[str(op_id)] = str(partial or "")
    except Exception:  # noqa: BLE001
        pass


def pop_partial(op_id: str) -> str:
    """Consume a stashed partial (once). Returns "" if none. NEVER raises."""
    try:
        return _PARTIAL_STASH.pop(str(op_id), "")
    except Exception:  # noqa: BLE001
        return ""


def _build_trace_lineage(context: Any, op_id: str) -> Dict[str, Any]:
    """Distributed Trace Lineage: mint (or carry forward) the op's span identity.

    A RESUMED op's ORIGINAL lineage (window 1) rides in its intake evidence --
    preserve it verbatim except the resume counter, so window N+2 still points
    at window 1's identity. Otherwise mint fresh from this context + the live
    a1_trace emit ledger (the original emit evidence, wall-clock-translated so
    it stays meaningful across processes). NEVER raises."""
    try:
        prior: Dict[str, Any] = {}
        try:
            _ev = json.loads(getattr(context, "intake_evidence_json", "") or "{}")
            _cand = _ev.get("trace_lineage") or {}
            if isinstance(_cand, dict) and _cand.get("origin_goal_id"):
                prior = dict(_cand)
        except Exception:  # noqa: BLE001
            prior = {}
        if prior:
            prior["resume_count"] = int(prior.get("resume_count", 0) or 0) + 1
            return prior
        lineage: Dict[str, Any] = {
            "origin_goal_id": op_id,
            "origin_source": str(getattr(context, "signal_source", "") or ""),
            "resume_count": 1,
        }
        try:
            from backend.core.ouroboros.governance import a1_trace as _a1t  # noqa: PLC0415
            _rec = _a1t.get_emit_record(op_id)
            if _rec is not None:
                _emit_mono, _emit_src = _rec
                lineage["emit_source"] = str(_emit_src)
                # Translate the process-local monotonic stamp to wall-clock so
                # the evidence survives the process boundary.
                lineage["emit_wall_ts"] = time.time() - max(
                    0.0, time.monotonic() - float(_emit_mono))
        except Exception:  # noqa: BLE001
            pass
        return lineage
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Git-drift mitigation (2026-07-18)
#
# A checkpoint records the repo HEAD at suspend time. If a commit/merge lands
# while the op is suspended (the real-world async mutation the operator flagged),
# the op's preserved exploration + partial-completion describe CODE THAT NO
# LONGER EXISTS. Fast-forwarding straight back into GENERATE with that stale
# context is exactly the corruption to avoid. So the hydrator compares stored vs
# live HEAD and, on drift, DEMOTES the resume phase to PLAN (re-plan against
# current source) and drops the stale exploration/partial context.
# ---------------------------------------------------------------------------

# Ordered pipeline phases for the demotion comparison. Only the linear spine is
# needed (retry/terminal phases never checkpoint a resumable forward position).
_PHASE_ORDER: Dict[str, int] = {
    "CLASSIFY": 0, "ROUTE": 1, "CONTEXT_EXPANSION": 2, "PLAN": 3,
    "GENERATE": 4, "VALIDATE": 5, "GATE": 6, "APPROVE": 7, "APPLY": 8,
    "VERIFY": 9,
}
_DRIFT_DEMOTION_FLOOR = "PLAN"


def drift_demotion_enabled() -> bool:
    """``JARVIS_FSM_RESUME_DRIFT_DEMOTION_ENABLED`` (default ON). Fail-soft."""
    return os.environ.get(
        "JARVIS_FSM_RESUME_DRIFT_DEMOTION_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def current_repo_sha(repo_path: "Optional[str]" = None) -> "Optional[str]":
    """Live ``git rev-parse HEAD`` (or None on any error). The single git-HEAD
    read for the checkpoint layer — capture stamps it, hydrate compares it."""
    try:
        import subprocess  # noqa: PLC0415
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
            cwd=repo_path or os.getcwd(),
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            return sha or None
    except Exception:  # noqa: BLE001
        pass
    return None


def resolve_resume_phase(
    cp: "FSMCheckpoint", *, live_repo_sha: "Optional[str]" = None,
) -> "Tuple[str, bool]":
    """Return ``(effective_phase, drifted)`` for a resuming checkpoint.

    No stored sha, drift-demotion disabled, or live HEAD unreadable → resume at
    the stored phase unchanged (fail-soft: a missing HEAD must not corrupt a
    legitimate fast-forward). Stored sha != live HEAD AND the stored phase is
    PAST the PLAN floor → demote to PLAN so the op re-plans against current code.
    Drift at/before PLAN needs no demotion (already re-planning).
    """
    stored = str(getattr(cp, "repo_sha", "") or "")
    phase = str(getattr(cp, "phase", "") or "")
    if not drift_demotion_enabled() or not stored:
        return phase, False
    live = live_repo_sha if live_repo_sha is not None else current_repo_sha()
    if not live or live == stored:
        return phase, False
    floor = _PHASE_ORDER[_DRIFT_DEMOTION_FLOOR]
    if _PHASE_ORDER.get(phase, -1) > floor:
        return _DRIFT_DEMOTION_FLOOR, True
    return phase, True


def capture_from_context(context: Any, *, phase: str, tool_history: "Optional[List[Dict[str, Any]]]" = None,
                         exploration_records: "Optional[List[Dict[str, Any]]]" = None,
                         resume_reason: str = "wall_clock_cap") -> "Optional[FSMCheckpoint]":
    """Build a checkpoint from an op context. Fail-soft -> None if the context has
    no op_id (nothing to resume). NEVER raises."""
    try:
        op_id = (getattr(context, "op_id", "") or "").strip()
        if not op_id:
            return None
        _tf = list(getattr(context, "target_files", ()) or ())
        return FSMCheckpoint(
            op_id=op_id,
            phase=str(phase or getattr(context, "phase", "") or "GENERATE"),
            goal_description=str(getattr(context, "description", "") or ""),
            target_files=[str(f) for f in _tf],
            tool_history=list(tool_history or []),
            exploration_records=list(exploration_records or []),
            intake_evidence_json=str(getattr(context, "intake_evidence_json", "") or ""),
            provider_route=str(getattr(context, "provider_route", "") or ""),
            partial_completion=pop_partial(op_id),
            trace_lineage=_build_trace_lineage(context, op_id),
            created_at=time.time(),
            resume_reason=str(resume_reason),
            repo_sha=current_repo_sha() or "",
        )
    except Exception:  # noqa: BLE001
        return None


def write_checkpoint(cp: FSMCheckpoint, *, base_dir: "Optional[str]" = None) -> "Optional[str]":
    """Serialize + HMAC-SIGN a checkpoint to ``<dir>/<op_id>.json`` (atomic
    tmp+rename). The on-disk wrapper is ``{schema, payload, hmac}`` where ``hmac``
    binds the exact payload bytes -- any tamper invalidates it. Returns the path, or
    None on failure. NEVER raises."""
    try:
        d = checkpoint_dir(base_dir)
        payload_json = cp.to_json()
        sig = _sign(payload_json, _checkpoint_key(base_dir))
        wrapper = json.dumps({"schema": _SCHEMA_VERSION, "payload": payload_json, "hmac": sig})
        path = os.path.join(d, "%s.json" % cp.op_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(wrapper)
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Poison-Pill quarantine (2026-07-18)
#
# A checkpoint that deterministically CRASHES the pipeline on resume would loop
# forever: hydrate → crash → reboot → hydrate the same checkpoint → crash …
# This is NOT solved by a generic try/except (which would silently swallow the
# fault and re-attempt anyway) but by a DETERMINISTIC counter: each hydration
# increments hydration_attempt_count on disk (re-signed) BEFORE the re-inject, so
# the increment survives even a hard crash mid-attempt. Past the ceiling the
# checkpoint is shunted to a quarantine/ DLQ (the SAME move-to-subdir + HMAC-
# signed-envelope pattern as expired/) and the loop proceeds to the next op —
# pipeline survival over a single poisoned operation.
# ---------------------------------------------------------------------------


def hydration_max_attempts() -> int:
    """Resume attempts before a checkpoint is quarantined as a poison pill
    (env ``JARVIS_FSM_HYDRATION_MAX_ATTEMPTS``, default 3). NEVER raises."""
    try:
        raw = os.environ.get("JARVIS_FSM_HYDRATION_MAX_ATTEMPTS", "").strip()
        n = int(raw) if raw else 3
        return n if n >= 1 else 3
    except Exception:  # noqa: BLE001
        return 3


def record_hydration_attempt(
    cp: "FSMCheckpoint", *, base_dir: "Optional[str]" = None,
) -> int:
    """Increment ``hydration_attempt_count`` and PERSIST it (re-signed) before a
    resume is attempted, so a crash mid-attempt cannot reset the count. Mutates
    *cp* in place and returns the new count. On a write failure returns the
    in-memory incremented count (still monotonic within the process). NEVER
    raises."""
    try:
        cp.hydration_attempt_count = int(cp.hydration_attempt_count or 0) + 1
    except Exception:  # noqa: BLE001
        cp.hydration_attempt_count = 1
    try:
        write_checkpoint(cp, base_dir=base_dir)
    except Exception:  # noqa: BLE001
        pass
    return cp.hydration_attempt_count


def quarantine_dir(base_dir: "Optional[str]" = None) -> str:
    """The poison-pill DLQ dir (``<checkpoints>/quarantine``). Created on demand.
    NEVER raises."""
    try:
        d = os.path.join(checkpoint_dir(base_dir), "quarantine")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:  # noqa: BLE001
        return os.path.join(".ouroboros", "checkpoints", "quarantine")


def quarantine_checkpoint(
    cp: "FSMCheckpoint", *, reason: str, base_dir: "Optional[str]" = None,
) -> "Optional[str]":
    """Shunt a poison-pill checkpoint to the quarantine DLQ: re-sign it tagged
    with the quarantine reason (inside the HMAC payload) into ``quarantine/`` and
    remove it from the pending set, so it is never resumed again but stays
    auditable (§8). Returns the quarantine path, or None on failure. NEVER
    raises."""
    try:
        # Tag the state vector — carried inside the HMAC-signed payload (DRY:
        # reuses the checkpoint envelope + signing; no separate DLQ schema).
        try:
            _lin = dict(cp.trace_lineage or {})
            _lin["quarantined"] = True
            _lin["quarantine_reason"] = str(reason)
            _lin["quarantine_attempts"] = int(cp.hydration_attempt_count or 0)
            cp.trace_lineage = _lin
        except Exception:  # noqa: BLE001
            pass
        d = quarantine_dir(base_dir)
        payload_json = cp.to_json()
        sig = _sign(payload_json, _checkpoint_key(base_dir))
        wrapper = json.dumps(
            {"schema": _SCHEMA_VERSION, "payload": payload_json, "hmac": sig}
        )
        path = os.path.join(d, "%s.json" % cp.op_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(wrapper)
        os.replace(tmp, path)
        # Remove from the pending set (best-effort) so it is not re-hydrated.
        try:
            pending_path = os.path.join(checkpoint_dir(base_dir), "%s.json" % cp.op_id)
            if os.path.isfile(pending_path):
                os.remove(pending_path)
        except Exception:  # noqa: BLE001
            pass
        logger.warning(
            "[fsm_checkpoint] QUARANTINED op=%s attempts=%d reason=%s -> "
            "poison-pill DLQ (quarantine/); pipeline proceeds to the next op",
            cp.op_id, int(cp.hydration_attempt_count or 0), reason,
        )
        return path
    except Exception:  # noqa: BLE001
        logger.debug("[fsm_checkpoint] quarantine write failed", exc_info=True)
        return None


def list_quarantined(*, base_dir: "Optional[str]" = None) -> List[FSMCheckpoint]:
    """Read the quarantine DLQ (HMAC-verified). Observability only — these are
    NEVER resumed. NEVER raises."""
    out: List[FSMCheckpoint] = []
    try:
        d = quarantine_dir(base_dir)
        key = _checkpoint_key(base_dir)
        for name in sorted(os.listdir(d)):
            if not name.endswith(".json"):
                continue
            try:
                raw = json.loads(open(os.path.join(d, name), encoding="utf-8").read())
                payload = raw.get("payload", "")
                if _verify(payload, raw.get("hmac", ""), key):
                    out.append(FSMCheckpoint.from_json(payload))
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def _checkpoint_ttl_s() -> float:
    """TTL (seconds) for a pending checkpoint before it is excluded from resume
    (env ``JARVIS_CHECKPOINT_TTL_S``, default 86400 = 24h). ``0`` disables expiry
    (legacy include-all). NEVER raises."""
    try:
        raw = os.environ.get("JARVIS_CHECKPOINT_TTL_S", "").strip()
        return float(raw) if raw else 86400.0
    except Exception:  # noqa: BLE001
        return 86400.0


def _expire_checkpoint(checkpoint_dir_path: str, name: str, op_id: str, age_s: float) -> None:
    """Move a stale checkpoint file to the ``expired/`` subdir (audit trail --
    §8, never deleted) + emit one WARNING naming op_id + age. NEVER raises."""
    try:
        expired_dir = os.path.join(checkpoint_dir_path, "expired")
        os.makedirs(expired_dir, exist_ok=True)
        os.replace(
            os.path.join(checkpoint_dir_path, name),
            os.path.join(expired_dir, name),
        )
    except Exception:  # noqa: BLE001
        pass
    logger.warning(
        "[fsm_checkpoint] EXPIRED op=%s age=%.0fs (TTL exceeded) -> moved to "
        "expired/ (stale campaign backlog, NOT resumed -- was inflating boot "
        "latency and re-running every session, bt-iso-1783035104)",
        op_id, age_s,
    )


def list_pending(*, base_dir: "Optional[str]" = None) -> List[FSMCheckpoint]:
    """All un-resumed checkpoints whose HMAC VERIFIES (oldest first). Corrupt,
    tampered, empty, or unsigned files are REJECTED (fail-closed) + logged, never
    returned -- zero corrupted executions. Checkpoints older than
    ``JARVIS_CHECKPOINT_TTL_S`` (default 24h) are EXCLUDED and moved to an
    ``expired/`` subdir (audit trail, never deleted) -- stale campaigns must not
    resurrect every session and inflate boot latency (bt-iso-1783035104).
    NEVER raises."""
    out: List[FSMCheckpoint] = []
    try:
        d = checkpoint_dir(base_dir)
        key = _checkpoint_key(base_dir)
        ttl_s = _checkpoint_ttl_s()
        names = [n for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".tmp")]
        for n in sorted(names):
            try:
                with open(os.path.join(d, n), "r", encoding="utf-8") as fh:
                    wrapper = json.loads(fh.read())
                payload_json = wrapper.get("payload") if isinstance(wrapper, dict) else None
                sig = wrapper.get("hmac") if isinstance(wrapper, dict) else None
                if not isinstance(payload_json, str) or not _verify(payload_json, sig or "", key):
                    logger.warning(
                        "[fsm_checkpoint] REJECT %s -- HMAC verify failed "
                        "(corrupt/tampered/unsigned) -> clean boot for this op", n,
                    )
                    continue
                _cp = FSMCheckpoint.from_json(payload_json)
                if ttl_s > 0:
                    age_s = time.time() - float(_cp.created_at or 0.0)
                    if age_s > ttl_s:
                        _expire_checkpoint(d, n, _cp.op_id, age_s)
                        continue
                # Atomic Hydration Handshake (facet 1): positive proof the signature
                # verified -- symmetric with the REJECT log above, forced to stdout.
                emit_handshake(format_verified_handshake(_cp.op_id, _cp.phase, sig or ""))
                out.append(_cp)
            except Exception:  # noqa: BLE001
                logger.warning("[fsm_checkpoint] REJECT %s -- unreadable -> clean boot", n)
                continue
        out.sort(key=lambda c: c.created_at)
    except Exception:  # noqa: BLE001
        pass
    return out


def workspace_stash_enabled() -> bool:
    """``JARVIS_FSM_WORKSPACE_STASH_ENABLED`` (default ON). When on, a dirty
    working tree is snapshotted (``git stash create``) at suspend and re-applied
    at hydration — Atomic Workspace Checkpointing. NEVER raises."""
    return os.environ.get(
        "JARVIS_FSM_WORKSPACE_STASH_ENABLED", "true",
    ).strip().lower() not in ("0", "false", "no", "off")


def workspace_root() -> str:
    """The git working-tree root the in-flight ops mutate. Env
    ``JARVIS_FSM_WORKSPACE_ROOT`` (for tests) else the process cwd. NEVER
    raises."""
    try:
        raw = os.environ.get("JARVIS_FSM_WORKSPACE_ROOT", "").strip()
        return raw or os.getcwd()
    except Exception:  # noqa: BLE001
        return "."


def restore_workspace_stash(stash_ref: str) -> bool:
    """HYDRATE: re-apply a suspended op's dirty working-tree delta by its raw
    ``git stash`` SHA (from the checkpoint payload) via the shared sync helper
    (DRY). Returns True on a clean apply; False (fail-soft) if stashing is
    disabled, the ref is empty/invalid, or the apply conflicts (e.g. the delta
    is already present because the tree was never cleaned) — the op resumes
    regardless. NEVER raises."""
    if not workspace_stash_enabled() or not stash_ref:
        return False
    try:
        from backend.core.ouroboros.governance.workspace_checkpoint import (  # noqa: PLC0415
            apply_stash_ref,
        )
        ok = apply_stash_ref(workspace_root(), stash_ref)
        if ok:
            logger.warning(
                "[fsm_checkpoint] atomic workspace RESTORED stash=%s "
                "(dirty-tree delta re-applied — op resumes mid-mutation)",
                str(stash_ref)[:12],
            )
        return ok
    except Exception:  # noqa: BLE001
        return False


def _capture_workspace_stash() -> str:
    """Snapshot the dirty working tree ONCE per suspend via the shared sync
    stash helper (DRY — no VC logic here). Returns the raw SHA, or "" when the
    tree is clean / stashing disabled / git fails. NEVER raises."""
    if not workspace_stash_enabled():
        return ""
    try:
        from backend.core.ouroboros.governance.workspace_checkpoint import (  # noqa: PLC0415
            create_stash_ref,
        )
        ref = create_stash_ref(workspace_root()) or ""
        if ref:
            logger.info(
                "[fsm_checkpoint] atomic workspace stash created sha=%s "
                "(dirty working-tree delta snapshotted for resume)", ref[:12],
            )
        return ref
    except Exception:  # noqa: BLE001
        return ""


def capture_inflight(*, base_dir: "Optional[str]" = None, reason: str = "wall_clock_cap") -> int:
    """SUSPEND: on graceful shutdown (wall-clock cap / SIGTERM preemption), read the
    in-flight registry and serialize a signed checkpoint for each active op (from its
    ctx_ref + last_phase_name). Returns the count checkpointed. Fully fail-soft --
    NEVER raises into the shutdown path (a checkpoint miss just means that op
    restarts clean, never a crash)."""
    n = 0
    try:
        from backend.core.ouroboros.governance.in_flight_registry import (  # noqa: PLC0415
            get_default_registry,
        )
        _records = list(get_default_registry().snapshot())
        # Atomic Workspace Checkpoint: snapshot the dirty tree ONCE (session-wide,
        # not per-op) and stamp the same ref on every in-flight checkpoint. Only
        # bother if there IS something to suspend.
        _workspace_stash = _capture_workspace_stash() if _records else ""
        for rec in _records:
            try:
                ctx = getattr(rec, "ctx_ref", None)
                if ctx is None:
                    continue
                cp = capture_from_context(
                    ctx, phase=getattr(rec, "last_phase_name", "") or "GENERATE",
                    resume_reason=reason,
                )
                if cp is not None:
                    cp.workspace_stash_ref = _workspace_stash
                if cp is not None and write_checkpoint(cp, base_dir=base_dir):
                    n += 1
                    logger.info(
                        "[fsm_checkpoint] SUSPENDED op=%s phase=%s reason=%s -> signed "
                        "checkpoint (resumes next ignition)", cp.op_id, cp.phase, reason,
                    )
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    if n == 0:
        # A suspend that captures NOTHING must say so -- a silent 0 here cost a
        # full cloud window to diagnose (bt-iso-1782942507: SIGTERM landed, the
        # registry was empty because pool ops never registered, and this
        # returned 0 without a trace).
        logger.warning(
            "[fsm_checkpoint] capture_inflight captured 0 op(s) (reason=%s) -- "
            "in-flight registry empty or unavailable; nothing to suspend", reason,
        )
    return n


def build_resume_envelope(
    cp: FSMCheckpoint, *, live_repo_sha: "Optional[str]" = None,
) -> Dict[str, Any]:
    """Build a resume intake envelope from a verified checkpoint. Normally
    carries the preserved tool/exploration context (Seamless Venom Hydration) so
    the model FAST-FORWARDS — it does not re-read files it already explored last
    window; the Iron Gate credits the preserved exploration and generation picks
    up where it left off.

    GIT-DRIFT GUARD: if the repo HEAD moved since the checkpoint was taken
    (:func:`resolve_resume_phase`), the preserved exploration/partial describe
    stale code, so the op is DEMOTED to PLAN and that stale context is DROPPED —
    the op re-plans against current source instead of fast-forwarding into
    corruption. The goal, target files, intake evidence and trace lineage always
    survive (they are what the op IS, not what it explored)."""
    effective_phase, drifted = resolve_resume_phase(cp, live_repo_sha=live_repo_sha)
    env: Dict[str, Any] = {
        "op_id": cp.op_id,
        "description": cp.goal_description,
        "target_files": list(cp.target_files),
        "source": "fsm_resume",
        "resume": True,
        "resume_phase": effective_phase,
        # Stale-context-safe: on drift, the exploration/partial/tool history are
        # against code that no longer exists → dropped so the re-PLAN is clean.
        "tool_history": [] if drifted else list(cp.tool_history),
        "exploration_records": [] if drifted else list(cp.exploration_records),
        "partial_completion": "" if drifted else cp.partial_completion,
        "intake_evidence_json": cp.intake_evidence_json,
        "provider_route": cp.provider_route,
        "trace_lineage": dict(cp.trace_lineage or {}),
        "repo_sha": cp.repo_sha,
        "drifted": drifted,
    }
    return env


def hydrate_pending_checkpoints(ingest_fn: Any, *, base_dir: "Optional[str]" = None) -> int:
    """Autonomous-startup resume: read HMAC-VERIFIED pending checkpoints, re-inject
    each via *ingest_fn* (with preserved exploration context), and consume it
    (mark_resumed -> exactly once). Rejected (unverified) checkpoints are already
    filtered by list_pending (fail-closed -> clean boot). Returns the count resumed.
    NEVER raises -- a resume failure leaves that checkpoint pending for the next boot."""
    n = 0
    for cp in list_pending(base_dir=base_dir):
        try:
            ingest_fn(build_resume_envelope(cp))
            mark_resumed(cp.op_id, base_dir=base_dir)
            n += 1
            logger.info(
                "[fsm_checkpoint] RESUMED op=%s phase=%s (%d exploration records "
                "preserved -> Venom fast-forward, no re-read)",
                cp.op_id, cp.phase, len(cp.exploration_records),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                "[fsm_checkpoint] resume re-inject failed op=%s -- left pending "
                "for next boot", cp.op_id,
            )
    return n


def mark_resumed(op_id: str, *, base_dir: "Optional[str]" = None) -> bool:
    """Consume a checkpoint after re-injection (delete it) so it resumes exactly
    ONCE. Returns True if a file was removed. NEVER raises."""
    try:
        d = checkpoint_dir(base_dir)
        path = os.path.join(d, "%s.json" % op_id)
        if os.path.isfile(path):
            os.remove(path)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False
