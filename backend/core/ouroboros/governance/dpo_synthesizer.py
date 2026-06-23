"""dpo_synthesizer.py -- Phase 4a: Autonomous DPO Pair Synthesizer (Body, $0).

Turns O+V's own epistemic-repair trajectories into token-dense DPO preference
pairs and exports them async/fire-and-forget to reactor-core's ingestor. Pure
Body-side logic -- no GPU, no network in the hot path.

The flywheel
------------
EVERY converged L2 repair produces a ``(failed_candidate, stderr,
repaired_candidate, resolved?)`` trajectory. The synthesizer turns each
*failed->passing* transition into one DPO pair (REJECTED = the failed symbol
body, CHOSEN = the repaired symbol body) and appends it to a bounded local
JSONL ring (and, gated, a best-effort GCS/bus publish).

The three load-bearing gates (purity over volume)
--------------------------------------------------
1. **Epistemic Purity** (``classify_rejection``): only train on OUR cognitive
   failures (pytest AssertionError, SyntaxError, NameError/TypeError, Iron-Gate
   logical severance). DROP anything caused by DoubleWord/Aegis/transport INFRA
   (fsm_exhausted, 5xx, timeouts, lane-collapse). Reuses ``dw_fault_taxonomy``
   + ``FailureSource`` names. Fail-safe: ambiguous -> infra -> DROP.
2. **Golden ratio** (no half-pairs): require BOTH a failed candidate AND a
   test-VERIFIED repaired candidate. A YIELDED trajectory (UNRESOLVABLE PATH /
   pivot / no resolved chosen) -> drop the whole pair.
3. **AST-node isolation**: extract ONLY the symbol that changed between failed
   and repaired (REJECTED = failed symbol body, CHOSEN = repaired symbol body);
   strip irrelevant imports/boilerplate/unrelated symbols. Density-cap each
   side via ``estimate_body_chars`` <= ``JARVIS_DPO_PAIR_MAX_CHARS``. Isolation
   failure / irreducible over-cap -> DROP (no raw dumps).

Reuse-first
-----------
- ``dw_fault_taxonomy`` + ``topology_sentinel.FailureSource`` (the infra
  classifiers; NOT forked).
- ``ast_symbol_scoper.slice_is_valid`` (the AST integrity gate primitive).
- ``dw_egress_interceptor.estimate_body_chars`` (the density estimate).
- ``outage_ledger.emit_outage_event`` (the async fire-and-forget Trinity bridge
  pattern; MIRRORED here, not imported).

Env gates
---------
- ``JARVIS_DPO_SYNTHESIS_ENABLED``    default "true"  (OFF -> no-op)
- ``JARVIS_DPO_PAIR_MAX_CHARS``       default 2048    (per-side density cap)
- ``JARVIS_DPO_DATASET_PATH``         default ".jarvis/dpo_dataset.jsonl"
- ``JARVIS_DPO_DATASET_MAX``          default 5000    (bounded ring)
- ``JARVIS_DPO_GCS_EXPORT_ENABLED``   default "false" (best-effort GCS/bus pub)

Fail-soft ABSOLUTE: NEVER raises into the repair loop or the O+V DAG.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import textwrap
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Module-level strong refs for in-flight asyncio tasks (prevent GC reap) --
# mirrors outage_ledger._INFLIGHT_TASKS.
_INFLIGHT_TASKS: set = set()


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off"}


def synthesis_enabled() -> bool:
    return _env_bool("JARVIS_DPO_SYNTHESIS_ENABLED", "true")


def gcs_export_enabled() -> bool:
    return _env_bool("JARVIS_DPO_GCS_EXPORT_ENABLED", "false")


def _max_chars() -> int:
    try:
        return int(os.environ.get("JARVIS_DPO_PAIR_MAX_CHARS", "2048"))
    except (ValueError, TypeError):
        return 2048


def _dataset_path() -> str:
    return os.environ.get(
        "JARVIS_DPO_DATASET_PATH",
        os.path.join(".jarvis", "dpo_dataset.jsonl"),
    )


def _dataset_max() -> int:
    try:
        return int(os.environ.get("JARVIS_DPO_DATASET_MAX", "5000"))
    except (ValueError, TypeError):
        return 5000


# ---------------------------------------------------------------------------
# Epistemic Purity gate (the load-bearing correctness gate)
# ---------------------------------------------------------------------------

# COGNITIVE markers -- OUR-side reasoning failures worth training away. These
# are the failure shapes a better generator would have avoided. Matched on the
# rejected payload's stderr/source.
_COGNITIVE_MARKERS = (
    "assertionerror",
    "assert ",
    "syntaxerror",
    "indentationerror",
    "nameerror",
    "typeerror",
    "attributeerror",
    "keyerror",
    "indexerror",
    "valueerror",
    "unboundlocalerror",
    "importerror",
    "modulenotfounderror",
    "zerodivisionerror",
    "recursionerror",
    "notimplementederror",
    "failed",            # pytest "1 failed", "test ... FAILED"
    "iron gate",         # Iron-Gate logical severance
    "exploration_insufficient",
)

# INFRA markers -- DoubleWord / Aegis / transport / our-budget faults. These are
# NOT cognitive: a smarter model would not have fixed them. DROP. Mirrors the
# FailureSource names + dw_fault_taxonomy message shapes.
_INFRA_MARKERS = (
    "fsm_exhausted",
    "all_providers_exhausted",
    "no_fallback_configured",
    "fallback_skipped",
    "generation_timeout",
    "tool_loop_deadline",
    "tool_loop_max_rounds",
    "tool_loop_round_budget",
    "tool_loop_starved",
    "local_egress_overweight",
    "live_transport",
    "live_http_5xx",
    "live_http_429",
    "live_stream_stall",
    "live_parse_error",
    "lane collapse",
    "lane_collapse",
    "quarantine",
    "upstream quarantine",
    "timeouterror",
    "timed out",
    "timeout",
    "clientconnectorerror",
    "connection",
    "aegis",
    "503",
    "502",
    "504",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "probe_fail",
    "probe_timeout",
)


def _infra_failure_source(failure_source: Optional[str]) -> bool:
    """True iff ``failure_source`` is one of the weight-0 / transport
    FailureSource names. Reuses the canonical enum values when importable;
    falls back to a literal-name match (this module stays import-light + the
    classifier must work even if topology_sentinel can't be loaded)."""
    if not failure_source:
        return False
    try:
        fs = str(failure_source).strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not fs:
        return False
    # Reuse the authoritative enum value set when available.
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (  # noqa: PLC0415
            FailureSource,
        )
        names = {m.value.lower() for m in FailureSource}
        # COGNITIVE carve-out: nothing in FailureSource is cognitive (it is a
        # *transport/probe* taxonomy), so any member match is infra.
        if fs in names:
            return True
    except Exception:  # noqa: BLE001 -- import-light; fall through to literal match
        pass
    return any(m in fs for m in _INFRA_MARKERS)


def classify_rejection(
    *,
    stderr: str,
    failure_source: Optional[str],
    fsm_state: Optional[str],
) -> str:
    """Classify the REJECTED payload as ``"cognitive"`` (KEEP) or ``"infra"``
    (DROP). Fail-safe: if it cannot CONFIDENTLY classify as cognitive, returns
    ``"infra"`` (purity over volume -- never pollute the dataset). NEVER raises.
    """
    try:
        # 1. A transport/budget FailureSource or fsm_state is decisive -> infra.
        if _infra_failure_source(failure_source):
            return "infra"
        if _infra_failure_source(fsm_state):
            return "infra"

        blob = str(stderr or "").lower()

        # 2. Any infra marker in the stderr -> infra (drop -- DW's problem, not
        #    a reasoning error). Checked BEFORE cognitive so a timeout that
        #    happens to contain the word "failed" is still dropped.
        if any(m in blob for m in _INFRA_MARKERS):
            return "infra"

        # 3. A clear cognitive marker -> keep.
        if any(m in blob for m in _COGNITIVE_MARKERS):
            return "cognitive"

        # 4. Ambiguous / opaque -> fail-safe to infra (DROP).
        return "infra"
    except Exception:  # noqa: BLE001 -- the gate must never itself throw
        return "infra"


# ---------------------------------------------------------------------------
# DPOPair (matches reactor-core dpo_pair_generator.DPOPair.to_dict shape)
# ---------------------------------------------------------------------------

@dataclass
class DPOPair:
    """A single DPO preference pair -- schema-compatible with reactor-core's
    ``training.dpo_pair_generator.DPOPair`` so Reactor ingests it natively
    (no new schema on the Nerves side)."""

    prompt: str
    chosen: str
    rejected: str
    chosen_model: Optional[str] = None
    rejected_model: Optional[str] = None
    chosen_score: float = 1.0
    rejected_score: float = 0.0
    task_type: Optional[str] = None
    generation_method: str = "outcome_diff"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt": self.prompt,
            "chosen": self.chosen,
            "rejected": self.rejected,
            "chosen_model": self.chosen_model,
            "rejected_model": self.rejected_model,
            "chosen_score": round(self.chosen_score, 4),
            "rejected_score": round(self.rejected_score, 4),
            "task_type": self.task_type,
            "generation_method": self.generation_method,
            "metadata": self.metadata,
        }

    def content_hash(self) -> str:
        """Stable content hash over (prompt, chosen, rejected) for dedup."""
        try:
            blob = (self.prompt + "\x00" + self.chosen + "\x00" + self.rejected).encode(
                "utf-8", errors="replace"
            )
            return hashlib.sha256(blob).hexdigest()
        except Exception:  # noqa: BLE001
            return hashlib.sha256(repr(self).encode("utf-8", errors="replace")).hexdigest()


# ---------------------------------------------------------------------------
# RepairTrajectory (the synthesizer input; decoupled for testability)
# ---------------------------------------------------------------------------

@dataclass
class RepairTrajectory:
    """The source trajectory for one DPO pair: a failed candidate and its
    test-verified repaired counterpart. Decoupled from ctx/result objects so
    it is trivially testable; use :meth:`from_repair` to extract from the live
    orchestrator objects at the L2_CONVERGED seam."""

    prompt: str
    failed_candidate_src: str
    repaired_candidate_src: Optional[str]
    resolved: bool
    stderr: str = ""
    failure_source: Optional[str] = None
    fsm_state: Optional[str] = None
    failure_signature_hash: str = ""
    task_type: Optional[str] = "l2_repair"
    changed_symbol_hint: str = ""
    provider: str = ""
    file_path: str = ""

    @classmethod
    def from_repair(cls, ctx: Any, result: Any) -> Optional["RepairTrajectory"]:
        """Extract a trajectory from the live ``(ctx, RepairResult)`` at the
        orchestrator L2 seam. Returns ``None`` unless this is a genuine
        L2_CONVERGED with both code states present. NEVER raises -> None."""
        try:
            if getattr(result, "terminal", "") != "L2_CONVERGED":
                return None
            chosen_cand = getattr(result, "candidate", None)
            repaired_src = _candidate_src(chosen_cand)
            if not repaired_src:
                return None

            failed_src = ""
            gen = getattr(ctx, "generation", None)
            cands = getattr(gen, "candidates", None) if gen is not None else None
            if cands:
                failed_src = _candidate_src(cands[0])
            if not failed_src:
                return None

            file_path = ""
            if isinstance(chosen_cand, dict):
                file_path = str(chosen_cand.get("file_path", "") or "")

            # Provider attribution: the converged iteration's provider_name.
            provider = ""
            try:
                for rec in reversed(getattr(result, "iterations", ()) or ()):
                    p = getattr(rec, "provider_name", "") or ""
                    if p:
                        provider = p
                        break
            except Exception:  # noqa: BLE001
                provider = ""

            # The prompt = the sub-goal / task description.
            sig = getattr(ctx, "signal", None)
            prompt = ""
            if sig is not None:
                prompt = str(getattr(sig, "description", "") or "")
            if not prompt:
                prompt = f"Repair the failing candidate for {file_path or 'the target'}."

            return cls(
                prompt=prompt,
                failed_candidate_src=failed_src,
                repaired_candidate_src=repaired_src,
                resolved=True,
                stderr=str(getattr(result, "stderr_tail", "") or ""),
                failure_source=None,
                fsm_state=None,
                failure_signature_hash=str(getattr(result, "failure_signature_hash", "") or ""),
                task_type="l2_repair",
                provider=provider,
                file_path=file_path,
            )
        except Exception:  # noqa: BLE001 -- extraction must never break the pipeline
            return None


def _candidate_src(candidate: Any) -> str:
    """Best-effort full source of a candidate dict (full_content, else first
    multi-file entry). Mirrors repair_trajectory_emitter._content."""
    try:
        if not isinstance(candidate, dict):
            return ""
        fc = candidate.get("full_content")
        if isinstance(fc, str) and fc:
            return fc
        files = candidate.get("files")
        if isinstance(files, list) and files and isinstance(files[0], dict):
            return str(files[0].get("full_content", "") or "")
        return ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# AST-node isolation
# ---------------------------------------------------------------------------

def _top_level_symbols(source: str) -> Dict[str, ast.AST]:
    """Map top-level def/class name -> AST node. {} on parse failure."""
    try:
        tree = ast.parse(source)
    except Exception:  # noqa: BLE001 -- syntax error / unparseable -> no symbols
        return {}
    out: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = node
    return out


def _segment(source: str, node: ast.AST) -> str:
    try:
        seg = ast.get_source_segment(source, node)
        return seg or ""
    except Exception:  # noqa: BLE001
        return ""


def _isolate_changed_symbol(
    failed_src: str,
    repaired_src: str,
    hint: str = "",
) -> Optional[tuple]:
    """Return ``(rejected_body, chosen_body)`` for the ONE symbol that changed
    between the failed and repaired source, with irrelevant imports/boilerplate/
    unrelated symbols stripped (AST keep-list = the symbol itself).

    Returns ``None`` if isolation fails (unparseable, no single changed symbol,
    or either slice fails the integrity gate) -- never a raw dump.
    """
    try:
        failed_syms = _top_level_symbols(failed_src)
        repaired_syms = _top_level_symbols(repaired_src)
        if not failed_syms or not repaired_syms:
            return None

        # Find symbols present in BOTH whose source segment differs.
        changed: List[str] = []
        for name, fnode in failed_syms.items():
            rnode = repaired_syms.get(name)
            if rnode is None:
                continue
            f_seg = _segment(failed_src, fnode)
            r_seg = _segment(repaired_src, rnode)
            if f_seg and r_seg and f_seg.strip() != r_seg.strip():
                changed.append(name)

        target: Optional[str] = None
        if len(changed) == 1:
            target = changed[0]
        elif len(changed) > 1 and hint and hint in changed:
            # Disambiguate via the changed-symbol hint when several differ.
            target = hint
        elif not changed:
            # No common symbol differs -- maybe a symbol was added/removed.
            # Fall back to the hint if it names a symbol in both.
            if hint and hint in failed_syms and hint in repaired_syms:
                target = hint
            else:
                return None
        else:
            return None

        rejected = _segment(failed_src, failed_syms[target])
        chosen = _segment(repaired_src, repaired_syms[target])
        if not rejected or not chosen:
            return None

        # Integrity gate (reuse the ast_symbol_scoper primitive).
        try:
            from backend.core.ouroboros.governance.ast_symbol_scoper import (  # noqa: PLC0415
                slice_is_valid,
            )
            if not slice_is_valid(rejected) or not slice_is_valid(chosen):
                return None
        except Exception:  # noqa: BLE001 -- fall back to a local parse check
            for seg in (rejected, chosen):
                try:
                    ast.parse(textwrap.dedent(seg))
                except Exception:  # noqa: BLE001
                    return None

        return (textwrap.dedent(rejected).strip("\n"), textwrap.dedent(chosen).strip("\n"))
    except Exception:  # noqa: BLE001 -- isolation must never raise
        return None


def _within_cap(text: str, cap: int) -> bool:
    """True iff ``text`` is within the density cap, measured via the egress
    interceptor's estimate_body_chars (reused, not forked)."""
    try:
        from backend.core.ouroboros.governance.dw_egress_interceptor import (  # noqa: PLC0415
            estimate_body_chars,
        )
        # Wrap as a single-message body so estimate_body_chars sums it.
        size = estimate_body_chars({"messages": [{"content": text}]})
    except Exception:  # noqa: BLE001 -- fall back to raw length
        size = len(text or "")
    return size <= cap


# ---------------------------------------------------------------------------
# synthesize_pair -- the gates
# ---------------------------------------------------------------------------

def synthesize_pair(trajectory: Any) -> Optional[DPOPair]:
    """Turn a :class:`RepairTrajectory` into a :class:`DPOPair`, or ``None`` if
    any gate fails (golden-ratio / epistemic-purity / AST-isolation / density).
    NEVER raises -> None (fail-soft)."""
    try:
        traj = trajectory
        # --- Golden ratio: both proven states required -------------------
        failed_src = str(getattr(traj, "failed_candidate_src", "") or "")
        repaired_src = getattr(traj, "repaired_candidate_src", None)
        resolved = bool(getattr(traj, "resolved", False))
        if not resolved or not repaired_src or not failed_src:
            return None
        repaired_src = str(repaired_src)
        if failed_src.strip() == repaired_src.strip():
            return None  # nothing changed -- nothing to learn

        # --- Epistemic purity: drop infra-caused rejections --------------
        verdict = classify_rejection(
            stderr=str(getattr(traj, "stderr", "") or ""),
            failure_source=getattr(traj, "failure_source", None),
            fsm_state=getattr(traj, "fsm_state", None),
        )
        if verdict != "cognitive":
            logger.debug("[DPOSynth] dropped pair: rejection classified infra")
            return None

        # --- AST-node isolation ------------------------------------------
        hint = str(getattr(traj, "changed_symbol_hint", "") or "")
        isolated = _isolate_changed_symbol(failed_src, repaired_src, hint)
        if isolated is None:
            logger.debug("[DPOSynth] dropped pair: AST isolation failed")
            return None
        rejected, chosen = isolated
        if rejected.strip() == chosen.strip():
            return None

        # --- Density cap (per side) --------------------------------------
        cap = _max_chars()
        if not _within_cap(rejected, cap) or not _within_cap(chosen, cap):
            logger.debug("[DPOSynth] dropped pair: density cap exceeded (irreducible)")
            return None

        provider = str(getattr(traj, "provider", "") or "") or None
        return DPOPair(
            prompt=str(getattr(traj, "prompt", "") or ""),
            chosen=chosen,
            rejected=rejected,
            chosen_model=provider,
            rejected_model=provider,
            chosen_score=1.0,
            rejected_score=0.0,
            task_type=str(getattr(traj, "task_type", "") or "") or None,
            generation_method="outcome_diff",
            metadata={
                "source": "ouroboros_epistemic_repair",
                "signature": str(getattr(traj, "failure_signature_hash", "") or ""),
                "task_type": str(getattr(traj, "task_type", "") or "") or None,
                "file_path": str(getattr(traj, "file_path", "") or ""),
            },
        )
    except Exception:  # noqa: BLE001 -- synthesis must never raise
        logger.debug("[DPOSynth] synthesize_pair failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Bounded JSONL ring (local dataset) + content-hash dedup
# ---------------------------------------------------------------------------

def _append_to_ring(pair: DPOPair) -> bool:
    """Append ``pair`` to the bounded JSONL ring with content-hash dedup.
    Returns True if a NEW record was written, False on dedup or any error.
    NEVER raises."""
    path = _dataset_path()
    try:
        h = pair.content_hash()
        records: List[Dict[str, Any]] = []
        seen: set = set()
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        d = json.loads(raw)
                    except Exception:  # noqa: BLE001 -- skip corrupt line
                        continue
                    records.append(d)
                    ch = (d.get("metadata") or {}).get("_content_hash")
                    if ch:
                        seen.add(ch)
        except FileNotFoundError:
            pass

        if h in seen:
            return False  # dedup

        rec = pair.to_dict()
        rec.setdefault("metadata", {})
        rec["metadata"]["_content_hash"] = h
        records.append(rec)

        # Bound the ring (trim oldest first).
        trimmed = records[-_dataset_max():]

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        dir_name = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for r in trimmed:
                    fh.write(json.dumps(r, default=str) + "\n")
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DPOSynth] ring append failed path=%s err=%r", path, exc)
        return False


# ---------------------------------------------------------------------------
# Async fire-and-forget export (MIRRORS outage_ledger.emit_outage_event)
# ---------------------------------------------------------------------------

async def _publish_pair(pair: DPOPair) -> None:
    """Async coroutine: append to the local ring, then (gated) best-effort
    GCS/Trinity-bus publish. NEVER raises."""
    try:
        wrote = _append_to_ring(pair)
        if wrote:
            logger.debug(
                "[DPOSynth] pair appended sig=%s task=%s",
                (pair.metadata or {}).get("signature"),
                pair.task_type,
            )
        if not gcs_export_enabled():
            return
        # Best-effort bus publish (reuse the Trinity bus when running). Any
        # failure is swallowed -- the local ring is the durable record.
        try:
            from backend.core.trinity_event_bus import (  # noqa: PLC0415
                get_event_bus_if_exists,
                TrinityEvent,
                EventPriority,
                RepoType,
            )
            bus = get_event_bus_if_exists()
            if bus is not None and getattr(bus, "_running", False):
                event = TrinityEvent(
                    topic="rsi.dpo_pair",
                    source=RepoType.JARVIS,
                    priority=EventPriority.LOW,
                    payload=pair.to_dict(),
                )
                await bus.publish(event, persist=True)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DPOSynth] bus publish skipped err=%r", exc)
    except Exception as exc:  # noqa: BLE001 -- the coroutine must never raise
        logger.warning("[DPOSynth] _publish_pair failed err=%r", exc)


def emit_dpo_pair(pair: DPOPair) -> None:
    """Synchronous fire-and-forget export. Schedules ``_publish_pair`` as an
    asyncio task; strong refs kept in ``_INFLIGHT_TASKS`` (GC cannot reap).
    No-op (no exception) if there is no running event loop -- mirrors
    ``outage_ledger.emit_outage_event``. NEVER blocks or raises into the DAG."""
    if pair is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no running loop -- silence (batch / startup context)
    try:
        task = loop.create_task(_publish_pair(pair))
        _INFLIGHT_TASKS.add(task)
        task.add_done_callback(_INFLIGHT_TASKS.discard)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[DPOSynth] emit schedule failed err=%r", exc)


# ---------------------------------------------------------------------------
# Convenience: synthesize + emit (the wiring entry point)
# ---------------------------------------------------------------------------

def synthesize_and_emit(trajectory: Any) -> bool:
    """Synthesize a pair from ``trajectory`` and emit it fire-and-forget.
    Returns whether a pair was emitted. Gated by ``JARVIS_DPO_SYNTHESIS_ENABLED``
    (OFF -> no-op, returns False). NEVER raises into the caller."""
    try:
        if not synthesis_enabled():
            return False
        pair = synthesize_pair(trajectory)
        if pair is None:
            return False
        emit_dpo_pair(pair)
        return True
    except Exception:  # noqa: BLE001 -- absolute fail-soft into the repair loop
        logger.debug("[DPOSynth] synthesize_and_emit failed", exc_info=True)
        return False


__all__ = [
    "DPOPair",
    "RepairTrajectory",
    "classify_rejection",
    "synthesize_pair",
    "emit_dpo_pair",
    "synthesize_and_emit",
    "synthesis_enabled",
    "gcs_export_enabled",
]
