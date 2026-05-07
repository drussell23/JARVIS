"""Move 6.5 Slice 5 — Multi-prior canvas + diff-fan-out
renderer.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Fan-out UX: use OpBlockBuffer (and existing §37 Tier 2 #12
   fields) for sibling prior rolls before bespoke UI state.
   Slice 5 — Canvas + diff-fan-out renderer composes /canvas
   Tier 2 #12 + diff_preview.py — Per-prior diff overlay —
   closes §36.2 priority #4."

This Slice ships:

  1. **`DispatchVerdictRing`** — bounded process-local
     in-memory ring of recent :class:`DispatchVerdict`
     instances (env-tunable, default 30). Slice 4's JSONL
     ledger only stores 256-char rationale previews; full
     per-prior diffs live in-memory only because persisting
     them would inflate the ledger 100×. When evicted,
     operators can still see metadata via /multi_prior op
     <id> but the diff-fan-out detail is gone (operator
     replays the op or waits for next).

  2. **`record_for_canvas(verdict, *, op_block_buffer=None)`**
     — caller-invoked alongside Slice 4's
     :func:`record_dispatch_outcome`. Two compositions:
       * Stores the verdict in the ring (full detail).
       * For each prior roll, calls
         :meth:`OpBlockBuffer.register_parent` so the K rolls
         appear in the canonical fan-out tracker (Tier 2 #12
         fields). **Composition over duplication** — no
         parallel parent-tracking state. AST-pinned via
         ``multi_prior_canvas_composes_op_block_buffer``.

  3. **`render_fan_out_overview(verdict)`** — pure function
     producing a plain-text K-prior fan-out summary:
     consensus signature, action recommendation, per-prior
     row with prior_id + outcome + AST signature prefix.

  4. **`render_diff_fan_out(verdict)`** — pure function
     producing per-prior diff comparison. Composes
     :class:`diff_preview.FileChange` semantics for canonical
     diff structure (no parallel diff parser; AST-pinned via
     ``multi_prior_canvas_composes_diff_preview``).

  5. **Extension to `canvas_repl.py`** (sibling edit) — adds
     ``/canvas multi_prior <op_id>`` and ``/canvas
     multi_prior_diff <op_id>`` subcommands that compose the
     above renderers.

**Authority asymmetry** (AST-pinned): no orchestrator /
iron_gate / providers / candidate_generator / change_engine /
semantic_guardian / plan_generator / urgency_router /
direction_inferrer / policy imports. Pure substrate.

**Master flag** ``JARVIS_MULTI_PRIOR_CANVAS_ENABLED``
default-FALSE per §33.1. When OFF,
:func:`record_for_canvas` is a no-op + the ring stays empty.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import (
    Any, Deque, FrozenSet, List, Optional, Tuple,
)


logger = logging.getLogger(
    "Ouroboros.MultiPriorCanvas",
)


MULTI_PRIOR_CANVAS_SCHEMA_VERSION: str = (
    "multi_prior_canvas.1"
)


_TRUTHY: FrozenSet[str] = frozenset(
    {"1", "true", "yes", "on"},
)


# Default ring size — matches DiffArchive (Gap #4) +
# session_archive defaults. 30 entries × ~4 priors each ×
# ~10KB diff ≈ 1.2 MB process-local. Operator-tunable.
_DEFAULT_RING_SIZE: int = 30


# Diff rendering bounds — same shape as diff_preview.
_DIFF_PREVIEW_MAX_LINES: int = 60
_DIFF_PREVIEW_HEAD_TAIL: int = 12


# ---------------------------------------------------------------------------
# Master flag + tunable knobs
# ---------------------------------------------------------------------------


def master_enabled() -> bool:
    """``JARVIS_MULTI_PRIOR_CANVAS_ENABLED`` master switch.
    Default-FALSE per §33.1: when OFF,
    :func:`record_for_canvas` is a no-op (zero ring churn,
    zero OpBlockBuffer interaction). Pure read; NEVER raises.
    """
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_CANVAS_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False
    return raw in _TRUTHY


def ring_size() -> int:
    """Operator-tunable ring depth. Clamped [1, 200]. NEVER
    raises."""
    raw = os.environ.get(
        "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE", "",
    ).strip()
    if not raw:
        return _DEFAULT_RING_SIZE
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RING_SIZE
    if v < 1:
        return 1
    if v > 200:
        return 200
    return v


# ---------------------------------------------------------------------------
# Bounded process-local ring buffer
# ---------------------------------------------------------------------------


class DispatchVerdictRing:
    """Thread-safe bounded ring of recent
    :class:`DispatchVerdict` instances. Process-local;
    in-memory only — full per-prior diffs do NOT round-trip
    through Slice 4's JSONL ledger.

    Eviction policy: drop-oldest when the ring is full (deque
    with maxlen). Operator-tunable cap via
    ``JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE``.
    """

    def __init__(self, *, size: Optional[int] = None) -> None:
        capped = size if size is not None else ring_size()
        if capped < 1:
            capped = 1
        if capped > 200:
            capped = 200
        self._lock = threading.Lock()
        self._ring: Deque[Any] = deque(maxlen=capped)

    def append(self, verdict: Any) -> None:
        """Append a verdict. Evicts oldest when full. NEVER
        raises."""
        if verdict is None:
            return
        try:
            with self._lock:
                self._ring.append(verdict)
        except Exception:  # noqa: BLE001 — defensive
            pass

    def find_recent(
        self, op_id: str,
    ) -> Optional[Any]:
        """Most-recent verdict for ``op_id`` in the ring.
        Returns None on miss / eviction. NEVER raises."""
        name = str(op_id or "").strip()
        if not name:
            return None
        try:
            with self._lock:
                snapshot = list(self._ring)
        except Exception:  # noqa: BLE001 — defensive
            return None
        for v in reversed(snapshot):
            try:
                if str(getattr(v, "op_id", "")) == name:
                    return v
            except Exception:  # noqa: BLE001 — defensive
                continue
        return None

    def recent(
        self, *, limit: Optional[int] = None,
    ) -> Tuple[Any, ...]:
        """Return most-recent K verdicts (newest LAST).
        NEVER raises."""
        try:
            with self._lock:
                snapshot = list(self._ring)
        except Exception:  # noqa: BLE001 — defensive
            return ()
        if limit is None or limit <= 0:
            return tuple(snapshot)
        if limit >= len(snapshot):
            return tuple(snapshot)
        return tuple(snapshot[-limit:])

    def __len__(self) -> int:
        try:
            with self._lock:
                return len(self._ring)
        except Exception:  # noqa: BLE001 — defensive
            return 0


_DEFAULT_RING: Optional[DispatchVerdictRing] = None
_DEFAULT_RING_LOCK = threading.Lock()


def get_default_ring() -> DispatchVerdictRing:
    """Singleton accessor. NEVER raises."""
    global _DEFAULT_RING
    with _DEFAULT_RING_LOCK:
        if _DEFAULT_RING is None:
            _DEFAULT_RING = DispatchVerdictRing()
        return _DEFAULT_RING


def reset_default_ring_for_test() -> None:
    """Test helper. NEVER raises."""
    global _DEFAULT_RING
    with _DEFAULT_RING_LOCK:
        _DEFAULT_RING = None


# ---------------------------------------------------------------------------
# Public recorder — caller-invoked at the orchestrator's call site
# ---------------------------------------------------------------------------


def record_for_canvas(
    dispatch_verdict: Any,
    *,
    op_block_buffer: Any = None,
) -> bool:
    """Store the verdict in the ring + register each prior
    roll as an OpBlockBuffer fan-out child of the parent op.
    Returns True iff at least the ring append succeeded.
    NEVER raises.

    Composition discipline (operator binding 2026-05-07):
      * The K rolls appear in :class:`OpBlockBuffer` as
        children of the parent op via
        :meth:`register_parent` — same fan-out fields Tier 2
        #12 ships (parent_op_id / candidate_index /
        subagent_kind). Failures here are best-effort
        (children may be evicted from the buffer; still OK to
        record in the canvas ring).
      * The verdict goes in this slice's ring for full
        per-prior detail. Slice 4's JSONL ledger persists the
        summary row separately.

    Caller-invoked from the orchestrator's eventual
    integration point. When master flag off, returns False
    immediately."""
    if not master_enabled():
        return False
    if dispatch_verdict is None:
        return False
    # Step 1: compose OpBlockBuffer fan-out tracking. Best-
    # effort — failures don't block the ring append.
    _register_priors_in_op_block_buffer(
        dispatch_verdict,
        op_block_buffer=op_block_buffer,
    )
    # Step 2: append to ring.
    try:
        get_default_ring().append(dispatch_verdict)
        return True
    except Exception:  # noqa: BLE001 — defensive
        return False


def _register_priors_in_op_block_buffer(
    dispatch_verdict: Any,
    *,
    op_block_buffer: Any,
) -> None:
    """Compose :meth:`OpBlockBuffer.register_parent` for each
    roll. NEVER raises — best-effort tracking; if the buffer
    has evicted either op (out-of-window), :meth:`register_parent`
    silently returns False per its own contract."""
    try:
        verdict_result = getattr(
            dispatch_verdict, "verdict_result", None,
        )
        if verdict_result is None:
            return
        rolls = getattr(verdict_result, "rolls", ())
        if not rolls:
            return
        op_id = str(getattr(dispatch_verdict, "op_id", ""))
        if not op_id:
            return
    except Exception:  # noqa: BLE001 — defensive
        return
    buffer = op_block_buffer
    if buffer is None:
        try:
            from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
                get_default_buffer,
            )
            buffer = get_default_buffer()
        except Exception:  # noqa: BLE001 — defensive
            return
    for idx, roll in enumerate(rolls):
        try:
            child = str(getattr(roll, "roll_id", ""))
            if not child:
                continue
            buffer.register_parent(
                child_op_id=child,
                parent_op_id=op_id,
                candidate_index=idx,
                subagent_kind="multi_prior",
            )
        except Exception:  # noqa: BLE001 — defensive
            continue


def find_recent(op_id: str) -> Optional[Any]:
    """Read API: most-recent DispatchVerdict for ``op_id`` in
    the ring. None on miss / eviction. NEVER raises."""
    if not master_enabled():
        return None
    return get_default_ring().find_recent(op_id)


def recent_verdicts(
    *, limit: Optional[int] = None,
) -> Tuple[Any, ...]:
    """Read API: most-recent K verdicts. NEVER raises."""
    if not master_enabled():
        return ()
    return get_default_ring().recent(limit=limit)


# ---------------------------------------------------------------------------
# Renderers — pure functions returning plain text
# ---------------------------------------------------------------------------


def render_fan_out_overview(
    dispatch_verdict: Any,
) -> str:
    """K-prior fan-out summary. Plain ASCII text — same shape
    as :mod:`canvas_repl`'s tree renderer.

    Output:
      header (op_id / decision / action / consensus)
      one row per prior:
        candidate-index  outcome  prior_id  sig8  cost  elapsed

    Pure function; NEVER raises. Returns empty string when
    verdict is None / lacks fan-out detail."""
    if dispatch_verdict is None:
        return ""
    try:
        op_id = str(getattr(dispatch_verdict, "op_id", ""))
        decision = _enum_value(
            getattr(dispatch_verdict, "decision", None),
        )
        action = _enum_value(
            getattr(
                dispatch_verdict,
                "action_recommendation", None,
            ),
        )
        verdict_result = getattr(
            dispatch_verdict, "verdict_result", None,
        )
        roll_to_prior = dict(
            getattr(
                dispatch_verdict, "roll_to_prior_id", {},
            ) or {},
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""
    if verdict_result is None:
        return ""
    try:
        rolls = getattr(verdict_result, "rolls", ()) or ()
        consensus = getattr(
            verdict_result, "consensus_verdict", None,
        )
        consensus_outcome = _enum_value(
            getattr(consensus, "outcome", None),
        )
        agreement = int(
            getattr(consensus, "agreement_count", 0),
        )
        total = int(
            getattr(consensus, "total_rolls", 0),
        )
        canonical_sig = (
            str(
                getattr(
                    consensus, "canonical_signature", "",
                ) or "",
            )
        )[:8]
        completed = int(
            getattr(
                verdict_result, "completed_count", 0,
            ),
        )
        cancelled = int(
            getattr(
                verdict_result, "cancelled_count", 0,
            ),
        )
        timed_out = int(
            getattr(verdict_result, "timeout_count", 0),
        )
        errored = int(
            getattr(verdict_result, "error_count", 0),
        )
        wall = float(
            getattr(verdict_result, "wall_clock_s", 0.0),
        )
    except Exception:  # noqa: BLE001 — defensive
        return ""
    out: List[str] = [
        f"/canvas multi_prior {op_id}:",
        f"  decision={decision} action={action}",
        (
            f"  consensus={consensus_outcome} "
            f"agreement={agreement}/{total} "
            f"canonical_sig={canonical_sig or '—'}"
        ),
        (
            f"  completed={completed} "
            f"cancelled={cancelled} "
            f"timeout={timed_out} error={errored} "
            f"wall_clock={wall:.3f}s"
        ),
        f"  rolls (K={len(rolls)}; newest LAST):",
    ]
    for idx, roll in enumerate(rolls):
        try:
            roll_id = str(getattr(roll, "roll_id", ""))
            outcome = _enum_value(
                getattr(roll, "outcome", None),
            )
            prior_id = str(
                roll_to_prior.get(roll_id, "")
                or getattr(roll, "prior_id", ""),
            )
            sig = (
                str(
                    getattr(roll, "ast_signature", "")
                    or "",
                )
            )[:8]
            cost = float(
                getattr(roll, "cost_estimate_usd", 0.0),
            )
            elapsed = float(
                getattr(roll, "elapsed_s", 0.0),
            )
        except Exception:  # noqa: BLE001 — defensive
            out.append(
                f"    [{idx}] <unparseable roll>"
            )
            continue
        out.append(
            f"    [{idx}] {outcome} prior_id={prior_id} "
            f"sig={sig or '—'} "
            f"cost=${cost:.4f} elapsed={elapsed:.3f}s"
        )
    return "\n".join(out)


def render_diff_fan_out(
    dispatch_verdict: Any,
) -> str:
    """Per-prior diff comparison. Composes
    :class:`diff_preview.FileChange` semantics for canonical
    diff structure (no parallel diff parser).

    Output:
      header (same as overview)
      one block per completed roll:
        --- prior_id=<id> sig=<sig8> ---
        <truncated diff (max _DIFF_PREVIEW_MAX_LINES;
         head/tail _DIFF_PREVIEW_HEAD_TAIL each on overflow)>

    Pure function; NEVER raises."""
    if dispatch_verdict is None:
        return ""
    overview = render_fan_out_overview(dispatch_verdict)
    if not overview:
        return ""
    try:
        verdict_result = getattr(
            dispatch_verdict, "verdict_result", None,
        )
        if verdict_result is None:
            return overview
        rolls = getattr(verdict_result, "rolls", ()) or ()
    except Exception:  # noqa: BLE001 — defensive
        return overview
    # Lazy-import diff_preview's FileChange for canonical
    # truncation behavior. Composition discipline AST-pinned.
    truncate_fn = _resolve_truncate_helper()
    blocks: List[str] = [overview, "", "diff fan-out:"]
    for roll in rolls:
        try:
            outcome = _enum_value(
                getattr(roll, "outcome", None),
            )
            prior_id = str(getattr(roll, "prior_id", ""))
            sig = (
                str(
                    getattr(roll, "ast_signature", "")
                    or "",
                )
            )[:8]
            diff_text = str(
                getattr(roll, "candidate_diff", "") or "",
            )
        except Exception:  # noqa: BLE001 — defensive
            blocks.append(
                "  --- <unparseable roll> ---"
            )
            continue
        blocks.append(
            f"  --- prior_id={prior_id} sig={sig or '—'} "
            f"outcome={outcome} ---"
        )
        if not diff_text:
            blocks.append("    (empty diff)")
            continue
        truncated = truncate_fn(
            diff_text,
            max_lines=_DIFF_PREVIEW_MAX_LINES,
            head_tail=_DIFF_PREVIEW_HEAD_TAIL,
        )
        for line in truncated.splitlines():
            blocks.append(f"    {line}")
    return "\n".join(blocks)


def _resolve_truncate_helper():
    """Resolve diff_preview's canonical
    :func:`_truncate_head_tail` helper. Falls back to a local
    structural-equivalent on import failure (defensive — same
    shape as the canonical helper). NEVER raises."""
    try:
        from backend.core.ouroboros.battle_test.diff_preview import (  # noqa: E501
            _truncate_head_tail,
        )

        def _wrap(
            text: str,
            *,
            max_lines: int,
            head_tail: int,
        ) -> str:
            try:
                lines = text.splitlines()
                if len(lines) <= max_lines:
                    return text
                head, tail = _truncate_head_tail(
                    lines, max_lines,
                )
                gap = (
                    f"  ... ({len(lines) - max_lines} "
                    f"line(s) elided) ..."
                )
                return "\n".join(
                    [*head, gap, *tail],
                )
            except Exception:  # noqa: BLE001 — defensive
                return _local_truncate(
                    text,
                    max_lines=max_lines,
                    head_tail=head_tail,
                )

        return _wrap
    except Exception:  # noqa: BLE001 — defensive
        return _local_truncate


def _local_truncate(
    text: str,
    *,
    max_lines: int,
    head_tail: int,
) -> str:
    """Defensive fallback when diff_preview's helper is
    unavailable. Same head/tail discipline."""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = lines[:head_tail]
    tail = lines[-head_tail:]
    gap = (
        f"  ... ({len(lines) - 2 * head_tail} "
        f"line(s) elided) ..."
    )
    return "\n".join([*head, gap, *tail])


def _enum_value(node: Any) -> str:
    """Defensive enum.value extraction. NEVER raises."""
    try:
        v = getattr(node, "value", None)
        if v is None:
            return ""
        return str(v)
    except Exception:  # noqa: BLE001 — defensive
        return ""


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> None:
    """Auto-discovered. Seeds the flags this module reads."""
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_CANVAS_ENABLED",
            type_="bool",
            default="false",
            description=(
                "Master switch for Move 6.5 Slice 5 canvas "
                "renderer. Default-FALSE per §33.1; when "
                "off, record_for_canvas is a no-op + the "
                "ring stays empty."
            ),
            category="Observability",
            posture_relevance="RELEVANT",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_canvas.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_CANVAS_ENABLED=true"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorCanvas] master-flag seeding "
            "failed (non-fatal)", exc_info=True,
        )
    try:
        registry.register(
            name="JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE",
            type_="int",
            default=str(_DEFAULT_RING_SIZE),
            description=(
                "In-memory ring depth for recent "
                "DispatchVerdict instances. Clamped "
                "[1, 200]."
            ),
            category="Observability",
            posture_relevance="IGNORED",
            source_file=(
                "backend/core/ouroboros/governance/"
                "verification/multi_prior_canvas.py"
            ),
            example=(
                "JARVIS_MULTI_PRIOR_CANVAS_RING_SIZE=30"
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[MultiPriorCanvas] ring-size seeding failed "
            "(non-fatal)", exc_info=True,
        )


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``multi_prior_canvas_master_default_false`` —
         §33.1 producer flag.
      2. ``multi_prior_canvas_authority_asymmetry`` — no
         orchestrator-tier imports.
      3. ``multi_prior_canvas_composes_op_block_buffer``
         — :func:`record_for_canvas` MUST compose
         :meth:`OpBlockBuffer.register_parent` (no parallel
         parent-tracking state). Bytes-pinned.
      4. ``multi_prior_canvas_composes_diff_preview`` —
         :func:`render_diff_fan_out` MUST compose
         :func:`diff_preview._truncate_head_tail` (no
         parallel diff parser).
      5. ``multi_prior_canvas_ring_bounded`` —
         :class:`DispatchVerdictRing` MUST construct a
         deque with ``maxlen`` set (not unbounded).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/verification/"
        "multi_prior_canvas.py"
    )

    def _validate_master_default_false(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        target_func = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append("master_enabled() missing")
            return tuple(violations)
        empty_returns_false = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.If):
                continue
            for cmp_node in ast.walk(sub.test):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not cmp_node.ops or not isinstance(
                    cmp_node.ops[0], ast.Eq,
                ):
                    continue
                operand_empty = False
                for operand in (
                    cmp_node.left, *cmp_node.comparators,
                ):
                    if (
                        isinstance(operand, ast.Constant)
                        and operand.value == ""
                    ):
                        operand_empty = True
                        break
                if not operand_empty:
                    continue
                for stmt in sub.body:
                    if isinstance(stmt, ast.Return) and (
                        isinstance(stmt.value, ast.Constant)
                        and stmt.value.value is False
                    ):
                        empty_returns_false = True
                        break
                if empty_returns_false:
                    break
            if empty_returns_false:
                break
        if not empty_returns_false:
            violations.append(
                "master_enabled() MUST return False on "
                "empty env-var string per §33.1"
            )
        return tuple(violations)

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_substring = (
            "iron_gate", "providers", "candidate_generator",
            "urgency_router", "change_engine",
            "semantic_guardian", "plan_generator",
            "direction_inferrer",
        )
        forbidden_exact = {"orchestrator", "policy"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                segments = module.split(".")
                if any(
                    "multi_prior_canvas" in s
                    for s in segments
                ):
                    continue
                for seg in segments:
                    if seg in forbidden_exact:
                        violations.append(
                            f"multi_prior_canvas.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden segment {seg!r})"
                        )
                        break
                for f in forbidden_substring:
                    if any(f in seg for seg in segments):
                        violations.append(
                            f"multi_prior_canvas.py MUST "
                            f"NOT import {module!r} "
                            f"(forbidden token {f!r})"
                        )
                        break
        return tuple(violations)

    def _validate_composes_op_block_buffer(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``_register_priors_in_op_block_buffer`` MUST
        invoke ``buffer.register_parent(...)``. The Slice 5
        substrate composes the canonical fan-out tracker; no
        parallel parent-tracking state."""
        violations: list = []
        target_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name
                == "_register_priors_in_op_block_buffer"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append(
                "_register_priors_in_op_block_buffer "
                "missing"
            )
            return tuple(violations)
        has_register_parent_call = False
        for sub in ast.walk(target_func):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "register_parent"
            ):
                has_register_parent_call = True
                break
        if not has_register_parent_call:
            violations.append(
                "composes-op-block-buffer: "
                "_register_priors_in_op_block_buffer MUST "
                "call ``register_parent`` on the buffer "
                "(canonical fan-out tracker; no parallel "
                "state)"
            )
        return tuple(violations)

    def _validate_composes_diff_preview(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """``_resolve_truncate_helper`` MUST lazy-import
        from ``diff_preview`` (composition discipline; no
        parallel diff parser)."""
        violations: list = []
        target_func: Optional[ast.FunctionDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_resolve_truncate_helper"
            ):
                target_func = node
                break
        if target_func is None:
            violations.append(
                "_resolve_truncate_helper missing"
            )
            return tuple(violations)
        composes_diff_preview = False
        for sub in ast.walk(target_func):
            if isinstance(sub, ast.ImportFrom):
                module = sub.module or ""
                if "diff_preview" in module:
                    composes_diff_preview = True
                    break
        if not composes_diff_preview:
            violations.append(
                "composes-diff-preview: "
                "_resolve_truncate_helper MUST lazy-import "
                "from diff_preview (canonical truncation; "
                "no parallel diff parser)"
            )
        return tuple(violations)

    def _validate_ring_bounded(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """:class:`DispatchVerdictRing` MUST construct its
        internal deque with ``maxlen=...`` set. AST inspect
        the ``__init__`` for a Call to ``deque`` with maxlen
        keyword."""
        violations: list = []
        target_class: Optional[ast.ClassDef] = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "DispatchVerdictRing"
            ):
                target_class = node
                break
        if target_class is None:
            violations.append(
                "DispatchVerdictRing class missing"
            )
            return tuple(violations)
        has_bounded_deque = False
        for sub in ast.walk(target_class):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if not (
                (
                    isinstance(func, ast.Name)
                    and func.id == "deque"
                )
                or (
                    isinstance(func, ast.Attribute)
                    and func.attr == "deque"
                )
            ):
                continue
            for kw in sub.keywords:
                if kw.arg == "maxlen":
                    has_bounded_deque = True
                    break
            if has_bounded_deque:
                break
        if not has_bounded_deque:
            violations.append(
                "ring-bounded: DispatchVerdictRing MUST "
                "construct deque with ``maxlen=...`` to "
                "guarantee bounded growth"
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_canvas_master_default_false"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 5 — §33.1 master flag stays "
                "default-FALSE."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_canvas_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 5 — substrate purity: no "
                "orchestrator-tier imports."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_canvas_"
                "composes_op_block_buffer"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 5 — canvas composes "
                "OpBlockBuffer.register_parent (canonical "
                "Tier 2 #12 fan-out tracker); no parallel "
                "parent-tracking state."
            ),
            validate=_validate_composes_op_block_buffer,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_canvas_composes_diff_preview"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 5 — diff renderer composes "
                "diff_preview's canonical truncation helper; "
                "no parallel diff parser."
            ),
            validate=_validate_composes_diff_preview,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "multi_prior_canvas_ring_bounded"
            ),
            target_file=target,
            description=(
                "Move 6.5 Slice 5 — DispatchVerdictRing "
                "MUST be bounded (deque with maxlen) to "
                "prevent unbounded process-local growth."
            ),
            validate=_validate_ring_bounded,
        ),
    ]


__all__ = [
    "MULTI_PRIOR_CANVAS_SCHEMA_VERSION",
    "DispatchVerdictRing",
    "find_recent",
    "get_default_ring",
    "master_enabled",
    "recent_verdicts",
    "record_for_canvas",
    "register_flags",
    "register_shipped_invariants",
    "render_diff_fan_out",
    "render_fan_out_overview",
    "reset_default_ring_for_test",
    "ring_size",
]
