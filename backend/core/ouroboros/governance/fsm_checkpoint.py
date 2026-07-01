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

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

_SCHEMA_VERSION = 1


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
            created_at=time.time(),
            resume_reason=str(resume_reason),
        )
    except Exception:  # noqa: BLE001
        return None


def write_checkpoint(cp: FSMCheckpoint, *, base_dir: "Optional[str]" = None) -> "Optional[str]":
    """Serialize a checkpoint to ``<dir>/<op_id>.json`` (atomic tmp+rename).
    Returns the path, or None on failure. NEVER raises."""
    try:
        d = checkpoint_dir(base_dir)
        path = os.path.join(d, "%s.json" % cp.op_id)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(cp.to_json())
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001
        return None


def list_pending(*, base_dir: "Optional[str]" = None) -> List[FSMCheckpoint]:
    """All un-resumed checkpoints (oldest first). Corrupt files are skipped.
    NEVER raises."""
    out: List[FSMCheckpoint] = []
    try:
        d = checkpoint_dir(base_dir)
        names = [n for n in os.listdir(d) if n.endswith(".json") and not n.endswith(".tmp")]
        for n in sorted(names):
            try:
                with open(os.path.join(d, n), "r", encoding="utf-8") as fh:
                    out.append(FSMCheckpoint.from_json(fh.read()))
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda c: c.created_at)
    except Exception:  # noqa: BLE001
        pass
    return out


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
