"""Proof of work — a tamper-evident paper trail that O+V genuinely worked.

"Is it actually working?" had no single answer: flags say what is ON, logs say
what was SAID, and neither says what HAPPENED. Every fact needed already
exists, recorded by machinery built for other reasons; this module composes
them into one record per session and chains the records so a missing or
altered one is detectable. It decides nothing and enforces nothing.

A record answers four questions, each from its own ground truth:

* **Did the organism's capabilities run?** ``capability_liveness.snapshot``:
  static reachability x runtime firing -- FIRING (left evidence in the recent
  sessions), dormant (wired but SILENT), UNKNOWN (emits nothing observable).
* **Did it produce anything real?** Commits on the promotion target whose
  ``Session:`` trailer names this session (the trailer the promoter itself
  reads), each verified present in git -- not a counter that could drift.
* **Did it learn?** The failure-mode store: lessons, how often they were
  injected, and how often an injected lesson was followed by a VALIDATE pass
  (``resolutions`` -- memory measurably changing an outcome).
* **Did it train?** The session's ``learning_loop`` row from summary.json.

Chain: ``<repo>/.jarvis/proof/chain.jsonl``; each line carries the record,
the previous line's hash and its own ``sha256(prev_hash + canonical(record))``.
:func:`verify_chain` recomputes every link. NEVER raises.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ProofOfWork")

PROOF_SCHEMA_VERSION = "proof_of_work.1"
_ENV_CHAIN = "JARVIS_PROOF_CHAIN_PATH"
_ENV_DORMANT_SHOWN = "JARVIS_PROOF_DORMANT_SHOWN"
_GENESIS = "0" * 64


def chain_path(repo_root: Any) -> Path:
    raw = (os.environ.get(_ENV_CHAIN, "") or "").strip()
    return Path(raw) if raw else Path(repo_root) / ".jarvis" / "proof" / "chain.jsonl"


def _dormant_shown() -> int:
    try:
        return max(0, int(os.environ.get(_ENV_DORMANT_SHOWN, "") or 12))
    except ValueError:
        return 12


# ---------------------------------------------------------------- evidence

#: capability -> outcome -> count, for THIS process (one organism session).
_EVIDENCE: Dict[str, Dict[str, int]] = {}
EVIDENCE_EVENT = "capability_evidence"


def emit_evidence(capability: str, outcome: str, *, op_id: str = "", detail: str = "") -> None:
    """A capability reports that it ran and what came of it.

    One call, three destinations, so no feature needs its own surface: the
    cockpit (a ``capability_evidence`` breadcrumb on the existing stream),
    the session log (one uniform ``[Evidence]`` line), and this session's
    proof record (the tally :func:`build_proof` includes). Cheap and
    synchronous; NEVER raises."""
    try:
        cap, out = str(capability), str(outcome)
        bucket = _EVIDENCE.setdefault(cap, {})
        bucket[out] = bucket.get(out, 0) + 1
        logger.info("[Evidence] %s: %s op=%s %s", cap, out, (op_id or "-")[:16], str(detail)[:200])
        from backend.core.ouroboros.governance.ide_observability_stream import publish_task_event
        publish_task_event(EVIDENCE_EVENT, op_id or cap, {
            "capability": cap, "outcome": out, "detail": str(detail)[:300],
        })
    except Exception:  # noqa: BLE001
        pass


def evidence_tally() -> Dict[str, Dict[str, int]]:
    return {cap: dict(outcomes) for cap, outcomes in _EVIDENCE.items()}


# ---------------------------------------------------------------- sections


def _capabilities(repo_root: Path) -> Dict[str, Any]:
    try:
        from backend.core.ouroboros.governance import capability_liveness as cl
        snap = cl.snapshot(repo_root=repo_root)
        firing = dict(snap.get("firing") or {})
        dormant = [d.get("flag") for d in (snap.get("dormant") or []) if isinstance(d, dict)]
        return {
            "enabled": bool(snap.get("enabled", True)),
            "firing_counts": dict(firing.get("counts") or {}),
            "sessions_scanned": firing.get("sessions_scanned", 0),
            "reachability": dict(snap.get("capabilities") or {}),
            "dormant": [f for f in dormant if f],
            "observability_gaps": len(snap.get("observability_gaps") or []),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


async def _landings(repo_root: Path, session_id: str) -> Dict[str, Any]:
    """Commits on the promotion target whose Session trailer is this
    session, each verified to exist. Also counts this session's commits that
    are NOT yet on the target (landed on the session branch, unpromoted)."""
    try:
        from backend.core.ouroboros.governance.accumulation_promotion_gate import commit_session
        from backend.core.ouroboros.governance.main_promoter import _manager, resolve_target_branch
        mgr = _manager(repo_root)
        target = await resolve_target_branch(mgr)
        out: Dict[str, Any] = {"target": target or "", "verified": [], "unpromoted": 0}
        if not target or not session_id:
            out["note"] = "no promotion target or session id"
            return out
        sep, end = "\x1f", "\x1e"
        rc, log, _ = await mgr._run_git_rc(
            mgr._repo_root,
            ["log", target, f"--since={_session_start_iso(session_id)}",
             f"--format=%H{sep}%ct{sep}%s{sep}%B{end}"],
        )
        if rc != 0:
            out["note"] = "git log failed"
            return out
        for chunk in log.split(end):
            parts = chunk.strip("\n").split(sep)
            if len(parts) < 4 or commit_session(parts[3]) != session_id:
                continue
            sha = parts[0].strip()
            ok_rc, _o, _e = await mgr._run_git_rc(mgr._repo_root, ["cat-file", "-e", f"{sha}^{{commit}}"])
            out["verified"].append({
                "sha": sha[:12], "committed_unix": int(parts[1] or 0),
                "subject": parts[2][:120], "exists": ok_rc == 0,
            })
        rc2, branches, _ = await mgr._run_git_rc(
            mgr._repo_root, ["branch", "--list", f"*{session_id}*", "--format=%(refname:short)"],
        )
        for branch in (branches.split() if rc2 == 0 else []):
            rc3, count, _ = await mgr._run_git_rc(
                mgr._repo_root, ["rev-list", "--count", f"{target}..{branch}"],
            )
            if rc3 == 0 and count.strip().isdigit():
                out["unpromoted"] += int(count.strip())
        return out
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200], "verified": []}


def _session_start_iso(session_id: str) -> str:
    try:
        from backend.core.ouroboros.governance.capability_firing import _session_epoch
        epoch = _session_epoch(session_id)
    except Exception:  # noqa: BLE001
        epoch = None
    if epoch is None:
        return "1970-01-01T00:00:00Z"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _memory(since_unix: float) -> Dict[str, Any]:
    try:
        from backend.core.ouroboros.governance.failure_mode_memory import read_failure_mode_history
        records = read_failure_mode_history()
        recent = [r for r in records if float(getattr(r, "observed_at_unix", 0.0)) >= since_unix]
        return {
            "lessons_total": len(records),
            "lessons_this_session": len(recent),
            "injections_total": sum(int(getattr(r, "injections", 0)) for r in records),
            "resolutions_total": sum(int(getattr(r, "resolutions", 0)) for r in records),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def _learning(session_dir: Optional[Path]) -> Dict[str, Any]:
    if session_dir is None:
        return {}
    try:
        summary = json.loads((session_dir / "summary.json").read_text(encoding="utf-8"))
        return {
            "learning_loop": summary.get("learning_loop"),
            "stop_reason": summary.get("stop_reason"),
            "session_outcome": summary.get("session_outcome"),
        }
    except (OSError, ValueError):
        return {"note": "no summary.json yet"}


def verdict(record: Dict[str, Any]) -> str:
    """One word for the session, from its evidence:
    ``productive`` (verified commits on the target), ``landing`` (commits on
    the session branch, not yet promoted), ``active`` (capabilities fired,
    nothing landed), ``dark`` (no evidence of running)."""
    landings = record.get("landings") or {}
    if any(c.get("exists") for c in landings.get("verified") or []):
        return "productive"
    if int(landings.get("unpromoted") or 0) > 0:
        return "landing"
    if int(((record.get("capabilities") or {}).get("firing_counts") or {}).get("FIRING", 0)) > 0:
        return "active"
    return "dark"


async def build_proof(repo_root: Any, session_dir: Optional[Any] = None) -> Dict[str, Any]:
    """The record for *session_dir* (or the repository now, without one)."""
    root = Path(repo_root)
    sdir = Path(session_dir) if session_dir else None
    session_id = sdir.name if sdir else ""
    try:
        from backend.core.ouroboros.governance.capability_firing import _session_epoch
        since = _session_epoch(session_id) or 0.0
    except Exception:  # noqa: BLE001
        since = 0.0
    capabilities = await asyncio.to_thread(_capabilities, root)
    landings = await _landings(root, session_id)
    memory = await asyncio.to_thread(_memory, since)
    record = {
        "schema_version": PROOF_SCHEMA_VERSION,
        "session_id": session_id,
        "built_at_unix": round(time.time(), 3),
        "capabilities": capabilities,
        "landings": landings,
        "memory": memory,
        "learning": _learning(sdir),
        # What each capability reported THIS process: the features that ran
        # and their outcomes, from the features themselves.
        "evidence": evidence_tally(),
    }
    record["verdict"] = verdict(record)
    return record


# ---------------------------------------------------------------- the chain


def _canonical(record: Dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


def _link_hash(prev_hash: str, record: Dict[str, Any]) -> str:
    return hashlib.sha256((prev_hash + _canonical(record)).encode("utf-8")).hexdigest()


def _read_chain(path: Path) -> List[Dict[str, Any]]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, ValueError):
        return []


def append_to_chain(repo_root: Any, record: Dict[str, Any]) -> Optional[str]:
    """Append *record* as the next link; returns its hash."""
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import flock_critical_section
        path = chain_path(repo_root)
        with flock_critical_section(path) as locked:
            if not locked:
                return None
            links = _read_chain(path)
            prev = links[-1]["hash"] if links else _GENESIS
            link_hash = _link_hash(prev, record)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"prev_hash": prev, "hash": link_hash, "record": record},
                                    sort_keys=True, default=str) + "\n")
            return link_hash
    except Exception:  # noqa: BLE001
        logger.debug("[ProofOfWork] chain append degraded", exc_info=True)
        return None


def verify_chain(repo_root: Any) -> Tuple[bool, int, Optional[int]]:
    """``(intact, links, first_broken_index)`` -- every hash recomputed."""
    links = _read_chain(chain_path(repo_root))
    prev = _GENESIS
    for i, link in enumerate(links):
        if link.get("prev_hash") != prev or link.get("hash") != _link_hash(prev, link.get("record") or {}):
            return False, len(links), i
        prev = link["hash"]
    return True, len(links), None


def history(repo_root: Any, n: int) -> List[Dict[str, Any]]:
    return [link.get("record") or {} for link in _read_chain(chain_path(repo_root))[-max(0, n):]]


# ---------------------------------------------------------------- rendering


def render(record: Dict[str, Any]) -> str:
    """The operator's view of one record (cockpit, session proof.md)."""
    cap = record.get("capabilities") or {}
    counts = cap.get("firing_counts") or {}
    land = record.get("landings") or {}
    mem = record.get("memory") or {}
    learn = (record.get("learning") or {}).get("learning_loop") or {}
    verified = [c for c in land.get("verified") or [] if c.get("exists")]
    lines = [
        # A record that carries no verdict (an older chain link, another
        # writer) is judged by the same rule rather than shown as "?".
        f"Proof of work — {record.get('session_id') or 'live'}: "
        f"{(record.get('verdict') or verdict(record)).upper()}",
        f"  capabilities: {counts.get('FIRING', 0)} firing, {counts.get('SILENT', 0)} silent, "
        f"{counts.get('UNKNOWN', 0)} unobservable (over {cap.get('sessions_scanned', 0)} session(s))",
        f"  landed on {land.get('target') or '?'}: {len(verified)} verified commit(s); "
        f"{land.get('unpromoted', 0)} on the session branch awaiting promotion",
    ]
    lines += [f"    {c['sha']} {c['subject']}" for c in verified]
    lines.append(
        f"  memory: {mem.get('lessons_total', 0)} lessons ({mem.get('lessons_this_session', 0)} new), "
        f"injected {mem.get('injections_total', 0)}x, followed by a pass {mem.get('resolutions_total', 0)}x"
    )
    if learn:
        lines.append(f"  learning loop: {learn}")
    evidence = record.get("evidence") or {}
    if evidence:
        lines.append("  ran this session:")
        for cap in sorted(evidence):
            outcomes = ", ".join(f"{o}={n}" for o, n in sorted(evidence[cap].items()))
            lines.append(f"    {cap}: {outcomes}")
    dormant = cap.get("dormant") or []
    if dormant:
        shown = dormant[: _dormant_shown()]
        lines.append(f"  dormant (wired, not firing): {', '.join(shown)}"
                     + (f" … +{len(dormant) - len(shown)}" if len(dormant) > len(shown) else ""))
    for section in ("capabilities", "landings", "memory"):
        err = (record.get(section) or {}).get("error")
        if err:
            lines.append(f"  ! {section} unreadable: {err}")
    return "\n".join(lines)


async def write_session_proof(repo_root: Any, session_dir: Any) -> Optional[Dict[str, Any]]:
    """Build, persist (session ``proof.json`` + ``proof.md``) and chain the
    session's record. Called once at session end. NEVER raises."""
    try:
        record = await build_proof(repo_root, session_dir)
        sdir = Path(session_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "proof.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        text = render(record)
        (sdir / "proof.md").write_text(text + "\n", encoding="utf-8")
        link = await asyncio.to_thread(append_to_chain, repo_root, record)
        logger.warning("[ProofOfWork] %s (chain link %s)", text.replace("\n", " | "), (link or "unwritten")[:12])
        return record
    except Exception:  # noqa: BLE001
        logger.debug("[ProofOfWork] session proof degraded", exc_info=True)
        return None


__all__ = [
    "EVIDENCE_EVENT", "PROOF_SCHEMA_VERSION", "append_to_chain", "build_proof", "chain_path",
    "emit_evidence", "evidence_tally",
    "history", "render", "verdict", "verify_chain", "write_session_proof",
]
