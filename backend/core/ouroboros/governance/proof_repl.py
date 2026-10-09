"""``/proof`` — is O+V genuinely working? Ask the evidence.

Auto-discovered by the REPL dispatch registry (``dispatch_proof_command``),
so it works in the ``ov`` cockpit and on an attached session alike.

  /proof           the live record: capabilities firing / dormant, commits
                   verified on the promotion target this session, lessons
                   that changed an outcome, the learning loop
  /proof history   the last N chained session records (``JARVIS_PROOF_HISTORY_N``)
  /proof verify    recompute every link of the tamper-evident chain
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProofDispatchResult:
    ok: bool
    text: str


def _repo_root() -> Path:
    try:
        from backend.core.ouroboros.governance.execution_context import authoritative_repo_root
        return Path(authoritative_repo_root(os.environ.get("JARVIS_PROJECT_ROOT") or os.getcwd()))
    except Exception:  # noqa: BLE001
        return Path(os.environ.get("JARVIS_PROJECT_ROOT") or os.getcwd())


def _session_dir(root: Path):
    try:
        sessions = sorted(
            (p for p in (root / ".ouroboros" / "sessions").iterdir()
             if p.is_dir() and p.name.startswith("bt-")),
            key=lambda p: p.name,
        )
        return sessions[-1] if sessions else None
    except OSError:
        return None


def _history_n() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_PROOF_HISTORY_N", "") or 5))
    except ValueError:
        return 5


async def dispatch_proof_command(line: str) -> ProofDispatchResult:
    from backend.core.ouroboros.governance import proof_of_work as pow_
    parts = str(line or "").strip().lstrip("/").split()
    sub = parts[1].lower() if len(parts) > 1 else ""
    root = _repo_root()
    try:
        if sub == "verify":
            intact, n, broken = pow_.verify_chain(root)
            return ProofDispatchResult(True, (
                f"proof chain intact: {n} session record(s), every link recomputed"
                if intact else f"proof chain BROKEN at link {broken} of {n} — a record was altered or removed"
            ))
        if sub == "history":
            records = pow_.history(root, _history_n())
            if not records:
                return ProofDispatchResult(True, "no session proof recorded yet (written at session end)")
            return ProofDispatchResult(True, "\n\n".join(pow_.render(r) for r in records))
        record = await pow_.build_proof(root, _session_dir(root))
        return ProofDispatchResult(True, pow_.render(record))
    except Exception as exc:  # noqa: BLE001
        return ProofDispatchResult(False, f"/proof failed: {type(exc).__name__}: {exc}")
