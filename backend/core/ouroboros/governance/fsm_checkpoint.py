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
from typing import Any, Dict, List, Optional

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
    created_at: float = 0.0
    resume_reason: str = ""
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
            created_at=time.time(),
            resume_reason=str(resume_reason),
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


def list_pending(*, base_dir: "Optional[str]" = None) -> List[FSMCheckpoint]:
    """All un-resumed checkpoints whose HMAC VERIFIES (oldest first). Corrupt,
    tampered, empty, or unsigned files are REJECTED (fail-closed) + logged, never
    returned -- zero corrupted executions. NEVER raises."""
    out: List[FSMCheckpoint] = []
    try:
        d = checkpoint_dir(base_dir)
        key = _checkpoint_key(base_dir)
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
        for rec in get_default_registry().snapshot():
            try:
                ctx = getattr(rec, "ctx_ref", None)
                if ctx is None:
                    continue
                cp = capture_from_context(
                    ctx, phase=getattr(rec, "last_phase_name", "") or "GENERATE",
                    resume_reason=reason,
                )
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


def build_resume_envelope(cp: FSMCheckpoint) -> Dict[str, Any]:
    """Build a resume intake envelope from a verified checkpoint. Carries the
    preserved tool/exploration context (Seamless Venom Hydration) so the model
    FAST-FORWARDS -- it does not re-read files it already explored last window; the
    Iron Gate credits the preserved exploration and generation picks up where it
    left off."""
    return {
        "op_id": cp.op_id,
        "description": cp.goal_description,
        "target_files": list(cp.target_files),
        "source": "fsm_resume",
        "resume": True,
        "resume_phase": cp.phase,
        "tool_history": list(cp.tool_history),
        "exploration_records": list(cp.exploration_records),
        "intake_evidence_json": cp.intake_evidence_json,
        "provider_route": cp.provider_route,
        "partial_completion": cp.partial_completion,
    }


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
