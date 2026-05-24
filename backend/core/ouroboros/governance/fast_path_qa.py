"""
Fast-Path Q&A — Read-Only Knowledge Lookup (Phase 0)
=====================================================

Closes §41.3 Slice 3 #26 Phase 0 per the operator-signed
2026-05-11 design decisions (§41.3.1):

  * **D1c** — Explicit prefix `/ask <question>` (new verb)
  * **D2a** — Claude-direct backend (Phase 0; D2c hybrid lands
              in Phase 1 by composing :mod:`semantic_index`)
  * **D3a** — IMMEDIATE budget (Phase 0; D3b INFORMATIONAL
              sub-budget lands in Phase 2)
  * **D4 defaults** — ConversationBridge captures user turn +
              answer turn (composes existing
              :func:`get_default_bridge`); postmortems / attempted-
              count / long_horizon_memory NOT touched
  * **D5c** — `q-N` artifact refs in a sibling
              :class:`BoundedQAStore` ring (mirrors
              :class:`tool_render_store.BoundedBodyStore` pattern:
              monotonic seq + drop-oldest + thread-safe lock).
              ``/expand q-N`` re-runs the question through the
              full pipeline.

Per the operator binding 2026-05-11 (verbatim, "no shortcuts,
no parallel state, leverage existing files, no hardcoding"):

  * NO parallel Claude client — operator-injectable callable
    interface with a default factory that composes the same
    Anthropic SDK + env knobs the existing :mod:`providers`
    module uses.
  * NO parallel conversation state — composes
    :func:`conversation_bridge.get_default_bridge` for turn
    capture (D4 default).
  * NO hardcoded system prompt — env-tunable via
    :data:`SYSTEM_PROMPT_ENV_VAR` with a curated default that
    cites CLAUDE.md / PRD as the project knowledge anchor.
  * NO new artifact-ref dispatcher — the substrate exposes the
    ring; the existing ``/expand`` dispatcher (extended with a
    ``q-`` prefix branch in serpent_flow.py) handles lookup.

§33.1 cognitive substrate
``JARVIS_FAST_PATH_QA_ENABLED`` default-**FALSE**. Even with
operator approval of D1-D5, the runtime gate must be flipped
explicitly to authorize Q&A traffic.

Authority asymmetry (AST-pinned): stdlib + governance
composers only. Does NOT import orchestrator / iron_gate /
policy / candidate_generator / urgency_router / change_engine
/ semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator.
"""
from __future__ import annotations

import ast
import asyncio
import enum
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


FAST_PATH_QA_SCHEMA_VERSION: str = "fast_path_qa.1"


# ---------------------------------------------------------------------------
# Env knobs — §33.1 + operator-tunable runtime configuration
# ---------------------------------------------------------------------------


_ENV_MASTER = "JARVIS_FAST_PATH_QA_ENABLED"
_ENV_BUDGET_USD = "JARVIS_FAST_PATH_QA_DAILY_BUDGET_USD"
# §41.3 #26 Phase 2 D3b — canonical per-route sub-budget knob.
# Takes precedence over the legacy ``_ENV_BUDGET_USD`` when set
# (legacy retained for backward compat with Phase 0/1 operator
# muscle memory; Phase 2+ should prefer the canonical name).
# Mirrors the operator-signed contract in
# ``ProviderRoute.INFORMATIONAL``'s docstring (2026-05-11).
_ENV_INFORMATIONAL_BUDGET_USD = "JARVIS_INFORMATIONAL_BUDGET_USD"
_ENV_MAX_TOKENS = "JARVIS_FAST_PATH_QA_MAX_TOKENS"
_ENV_TEMPERATURE = "JARVIS_FAST_PATH_QA_TEMPERATURE"
_ENV_STORE_CAPACITY = "JARVIS_FAST_PATH_QA_STORE_CAPACITY"
_ENV_SYSTEM_PROMPT = "JARVIS_FAST_PATH_QA_SYSTEM_PROMPT"
_ENV_TIMEOUT_S = "JARVIS_FAST_PATH_QA_TIMEOUT_S"
_ENV_MODEL = "JARVIS_FAST_PATH_QA_MODEL"

# §41.3 #26 Phase 1 D2c — hybrid retrieval sub-flags. Compose
# the canonical :class:`semantic_index.SemanticIndex` for
# project-context grounding before invoking Claude. The 3-tier
# confidence ladder is fully operator-tunable.
_ENV_RETRIEVAL_ENABLED = (
    "JARVIS_FAST_PATH_QA_RETRIEVAL_ENABLED"
)
_ENV_RETRIEVAL_HIGH_CONFIDENCE = (
    "JARVIS_FAST_PATH_QA_RETRIEVAL_HIGH_CONFIDENCE"
)
_ENV_RETRIEVAL_LOW_CONFIDENCE = (
    "JARVIS_FAST_PATH_QA_RETRIEVAL_LOW_CONFIDENCE"
)
_ENV_RETRIEVAL_TOP_K = (
    "JARVIS_FAST_PATH_QA_RETRIEVAL_TOP_K"
)

# §41.3 #26 Phase 2 D3b — canonical cost_governor composition.
# Default-TRUE (conditional on master). When on, ask_question
# threads start()/charge()/finish() through the canonical
# per-op cost governor for INFORMATIONAL-route attribution.
# Operator opt-out: set to false to fall back to the Phase 0/1
# in-process daily counter ONLY (no per-op caps, no cross-
# substrate cost attribution). The daily aggregate budget gate
# is independent of this knob — that axis has no canonical
# home so it stays Q&A-substrate-local.
_ENV_COMPOSE_COST_GOVERNOR = (
    "JARVIS_FAST_PATH_QA_COMPOSE_COST_GOVERNOR"
)

# Canonical model knob name — operators have ANTHROPIC_API_KEY
# set globally; we don't re-shadow that.
_ENV_ANTHROPIC_KEY = "ANTHROPIC_API_KEY"

# Defaults — values chosen for the Phase 0 D2a Claude-direct
# backend. All operator-tunable via the env knobs above.
_DEFAULT_BUDGET_USD: float = 5.0
_DEFAULT_MAX_TOKENS: int = 400
_DEFAULT_TEMPERATURE: float = 0.3
_DEFAULT_STORE_CAPACITY: int = 100
_DEFAULT_TIMEOUT_S: int = 30
_DEFAULT_MODEL: str = "claude-sonnet-4-5"  # latest Sonnet

# D2c thresholds. Tuned for fastembed/bge-small embeddings —
# operators can adjust per their corpus. The semantic_index
# cosine range is [-1, 1] but real-world signals cluster
# 0.0–0.95; 0.55 high / 0.30 low maps to: "near-exact paraphrase
# matches CLAUDE.md fact" / "topic overlap, worth injecting".
_DEFAULT_RETRIEVAL_HIGH_CONFIDENCE: float = 0.55
_DEFAULT_RETRIEVAL_LOW_CONFIDENCE: float = 0.30
_DEFAULT_RETRIEVAL_TOP_K: int = 5

# Provenance tags for QAArtifact.retrieval_path — closed
# 4-value enum-like vocabulary documenting which path produced
# the answer. Stored as a plain str on the artifact (vs adding
# a closed enum) so the provenance vocabulary can extend
# additively per Phase 2/3 without churning the QAVerdict
# bytes-pin.
RETRIEVAL_PATH_RETRIEVAL_ONLY = "retrieval_only"   # high conf — $0 Claude
RETRIEVAL_PATH_HYBRID = "hybrid_grounded"          # mid conf — Claude w/ context
RETRIEVAL_PATH_CLAUDE_DIRECT = "claude_direct"     # low conf — Phase 0 path
RETRIEVAL_PATH_RETRIEVAL_DISABLED = "retrieval_disabled"  # sub-flag off
_DEFAULT_SYSTEM_PROMPT: str = (
    "You are JARVIS, a developer assistant embedded in the "
    "Ouroboros + Venom (O+V) self-developing governance "
    "system. Answer the operator's question concisely and "
    "accurately. Reference CLAUDE.md / PRD facts when relevant. "
    "Acknowledge uncertainty when you don't know — do NOT "
    "fabricate. Keep answers under 200 words unless the "
    "operator asks for detail. NO code generation, NO file "
    "modifications, NO tool calls — this is a read-only "
    "knowledge lookup path."
)

# q-N ref prefix — mirrors the t-N/d-N/o-N/n-N/p-N family.
# Module-level constant so AST pins can byte-pin the choice.
QA_REF_PREFIX: str = "q-"

# §41.3 #26 Phase 2 D3b — canonical route tag.
#
# String value MUST equal ``ProviderRoute.INFORMATIONAL.value``
# (the closed-5→6 taxonomy expansion shipped 2026-05-11 in
# ``urgency_router.py``). Duplicated here as a module-local
# constant rather than imported because the authority-asymmetry
# pin forbids ``fast_path_qa`` from importing ``urgency_router``
# (read-only Q&A path stays decoupled from routing internals —
# same precedent as :mod:`intent_envelope`'s
# ``_VALID_ROUTING_OVERRIDES`` frozenset duplication).
#
# Cross-reference parity is enforced by a co-located test:
# ``test_fast_path_qa_phase2.test_route_informational_matches_canonical``.
ROUTE_INFORMATIONAL: str = "informational"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Flag accessors — §33.1 pattern
# ---------------------------------------------------------------------------


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-**FALSE**. Even with the D1-D5 operator
    decisions approved, the runtime gate stays default-off so
    operators must flip ``JARVIS_FAST_PATH_QA_ENABLED=true``
    explicitly to authorize Q&A traffic. NEVER raises."""
    return _flag(_ENV_MASTER, default=False)


def daily_budget_usd() -> float:
    """§41.3 #26 Phase 2 D3b — per-route INFORMATIONAL budget.

    Precedence (highest → lowest):

      1. ``JARVIS_INFORMATIONAL_BUDGET_USD`` (canonical, Phase 2+)
      2. ``JARVIS_FAST_PATH_QA_DAILY_BUDGET_USD`` (legacy, Phase 0/1
         backward-compat — still honored to avoid breaking operator
         muscle memory; new deployments should use the canonical
         name)
      3. ``_DEFAULT_BUDGET_USD`` ($5/day)

    Clamped [0, 1000]. NEVER raises.
    """
    canonical = os.environ.get(
        _ENV_INFORMATIONAL_BUDGET_USD, "",
    ).strip()
    if canonical:
        try:
            return max(0.0, min(1000.0, float(canonical)))
        except (TypeError, ValueError):
            pass  # fall through to legacy + default
    raw = os.environ.get(_ENV_BUDGET_USD, "").strip()
    if not raw:
        return _DEFAULT_BUDGET_USD
    try:
        return max(0.0, min(1000.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_BUDGET_USD


def max_tokens() -> int:
    raw = os.environ.get(_ENV_MAX_TOKENS, "").strip()
    if not raw:
        return _DEFAULT_MAX_TOKENS
    try:
        return max(64, min(4000, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS


def temperature() -> float:
    raw = os.environ.get(_ENV_TEMPERATURE, "").strip()
    if not raw:
        return _DEFAULT_TEMPERATURE
    try:
        return max(0.0, min(2.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_TEMPERATURE


def store_capacity() -> int:
    raw = os.environ.get(_ENV_STORE_CAPACITY, "").strip()
    if not raw:
        return _DEFAULT_STORE_CAPACITY
    try:
        return max(1, min(10_000, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_STORE_CAPACITY


def timeout_s() -> int:
    raw = os.environ.get(_ENV_TIMEOUT_S, "").strip()
    if not raw:
        return _DEFAULT_TIMEOUT_S
    try:
        return max(5, min(300, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S


def system_prompt() -> str:
    """Operator can override the system prompt entirely via env.
    Empty / unset → curated default that cites CLAUDE.md / PRD."""
    raw = os.environ.get(_ENV_SYSTEM_PROMPT, "").strip()
    return raw if raw else _DEFAULT_SYSTEM_PROMPT


def model_name() -> str:
    raw = os.environ.get(_ENV_MODEL, "").strip()
    return raw if raw else _DEFAULT_MODEL


# §41.3 #26 Phase 1 — D2c hybrid retrieval accessors.


def retrieval_enabled() -> bool:
    """§41.3 #26 Phase 1 — D2c hybrid sub-flag. Default-TRUE
    (conditional on master). When the substrate is master-on
    AND retrieval is enabled, ``ask_question`` composes
    :class:`semantic_index.SemanticIndex.top_k_for_text` BEFORE
    invoking Claude. Operator opt-out: set
    ``JARVIS_FAST_PATH_QA_RETRIEVAL_ENABLED=false`` to fall
    back to Phase 0 Claude-direct path."""
    if not master_enabled():
        return False
    raw = os.environ.get(_ENV_RETRIEVAL_ENABLED, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def retrieval_high_confidence_threshold() -> float:
    """Cosine threshold above which retrieval ALONE answers
    (no Claude call; cost = $0). Clamped [0.0, 1.0]."""
    raw = os.environ.get(
        _ENV_RETRIEVAL_HIGH_CONFIDENCE, "",
    ).strip()
    if not raw:
        return _DEFAULT_RETRIEVAL_HIGH_CONFIDENCE
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRIEVAL_HIGH_CONFIDENCE


def retrieval_low_confidence_threshold() -> float:
    """Cosine threshold above which retrieval grounds Claude
    (context injected into system prompt). Below this falls to
    pure Claude-direct (Phase 0 path)."""
    raw = os.environ.get(
        _ENV_RETRIEVAL_LOW_CONFIDENCE, "",
    ).strip()
    if not raw:
        return _DEFAULT_RETRIEVAL_LOW_CONFIDENCE
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRIEVAL_LOW_CONFIDENCE


def retrieval_top_k() -> int:
    """Number of corpus items to retrieve."""
    raw = os.environ.get(_ENV_RETRIEVAL_TOP_K, "").strip()
    if not raw:
        return _DEFAULT_RETRIEVAL_TOP_K
    try:
        return max(1, min(50, int(raw)))
    except (TypeError, ValueError):
        return _DEFAULT_RETRIEVAL_TOP_K


def compose_cost_governor_enabled() -> bool:
    """§41.3 #26 Phase 2 D3b — canonical per-op cost-governor
    composition. Default-TRUE (conditional on master). When on,
    ask_question registers each Q&A op with the canonical
    :class:`cost_governor.CostGovernor`, runs the per-op cap
    pre-check, and charges the realized USD post-call. When
    off, the substrate falls back to the Phase 0/1 in-process
    daily counter alone (no per-op caps). NEVER raises."""
    if not master_enabled():
        return False
    raw = os.environ.get(_ENV_COMPOSE_COST_GOVERNOR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# Closed taxonomy — bytes-pinned via AST
# ---------------------------------------------------------------------------


class QAVerdict(str, enum.Enum):
    """Closed 5-value taxonomy."""

    ANSWERED = "answered"
    DISABLED = "disabled"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROVIDER_FAILED = "provider_failed"
    OUT_OF_SCOPE = "out_of_scope"


# ---------------------------------------------------------------------------
# §33.5 frozen artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QAStoreSnapshot:
    """Read-only projection of the BoundedQAStore's state for
    observability. §41.3 #26 Phase 2 Slice 4 — mirrors the
    canonical :class:`ArchiveSnapshot` shape from
    :mod:`permission_decision_archive` so IDE consumers see
    parallel structure across all artifact-ring substrates."""

    capacity: int
    size: int
    next_seq: int
    schema_version: str = FAST_PATH_QA_SCHEMA_VERSION

    @property
    def utilization(self) -> float:
        """Fraction in [0.0, 1.0] of capacity currently used."""
        if self.capacity <= 0:
            return 0.0
        return min(1.0, self.size / self.capacity)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "capacity": self.capacity,
            "size": self.size,
            "next_seq": self.next_seq,
            "utilization": self.utilization,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class QAArtifact:
    """One parked Q&A interaction. Mirrors
    :class:`tool_render_store.StoredBody` shape for `/expand`
    compatibility — the existing dispatcher recognizes `ref` as
    the lookup handle."""

    ref: str               # "q-N" monotonic handle
    question: str
    answer: str
    asked_at_unix: float
    op_id: str             # operator-supplied correlation
    cost_usd: float        # per-question Claude cost estimate
    model: str             # model name used
    elapsed_s: float
    inserted_at: float     # time.monotonic() — for telemetry
    # §41.3 #26 Phase 1 — D2c provenance. One of the
    # ``RETRIEVAL_PATH_*`` module constants. Defaults to
    # ``RETRIEVAL_PATH_CLAUDE_DIRECT`` for backward compat with
    # Phase 0 artifacts that predate the field. Open-vocabulary
    # by design — Phase 2+ may introduce new path tags
    # additively without churning the QAVerdict bytes-pin.
    retrieval_path: str = RETRIEVAL_PATH_CLAUDE_DIRECT
    # Top retrieval cosine score (when retrieval ran). 0.0 for
    # retrieval_disabled / claude_direct paths.
    top_score: float = 0.0
    # §41.3 #26 Phase 2 D3b — canonical route tag. Distinct from
    # ``retrieval_path`` (which is internal-to-substrate
    # provenance about HOW the answer was synthesized); ``route``
    # is the cross-substrate ProviderRoute classification of WHAT
    # KIND of traffic this op was. Q&A is always
    # ``ROUTE_INFORMATIONAL`` by construction — no other route
    # may be stamped here. Mirrors the
    # ``ProviderRoute.INFORMATIONAL`` value (parity enforced by
    # test_fast_path_qa_phase2).
    route: str = ROUTE_INFORMATIONAL
    schema_version: str = FAST_PATH_QA_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ref": self.ref,
            "question": self.question[:1024],
            "answer_chars": len(self.answer),
            "asked_at_unix": float(self.asked_at_unix),
            "op_id": self.op_id[:64],
            "cost_usd": float(self.cost_usd),
            "model": self.model[:128],
            "elapsed_s": float(self.elapsed_s),
            "inserted_at": float(self.inserted_at),
            "retrieval_path": self.retrieval_path[:64],
            "top_score": float(self.top_score),
            "route": self.route[:32],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class QAReport:
    """Top-level Q&A response — what :func:`ask_question`
    returns to the caller."""

    verdict: QAVerdict
    artifact: Optional[QAArtifact]
    diagnostic: str
    evaluated_at_unix: float
    schema_version: str = FAST_PATH_QA_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "artifact": (
                self.artifact.to_dict() if self.artifact else None
            ),
            "diagnostic": self.diagnostic[:512],
            "evaluated_at_unix": float(self.evaluated_at_unix),
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# BoundedQAStore — sibling ring to BoundedBodyStore
#
# Same architecture (RLock + OrderedDict + monotonic seq +
# drop-oldest), different artifact type + ref prefix. Per the
# operator binding 2026-05-11, this composes the BoundedBodyStore
# PATTERN (which is repeated across the 5 existing artifact-ref
# rings: tool_render_store / diff_archive / op_block_buffer /
# narrative_channel / permission_decision_archive). q-N joins
# the family as the 6th ring.
# ---------------------------------------------------------------------------


class BoundedQAStore:
    """Thread-safe bounded FIFO of QA artifacts. Drop-oldest on
    overflow. Monotonic ref allocation — refs never reuse.
    Mirrors :class:`tool_render_store.BoundedBodyStore`'s contract
    so the ``/expand`` dispatcher pattern composes uniformly."""

    def __init__(self, *, capacity: Optional[int] = None) -> None:
        cap = (
            capacity if capacity is not None else store_capacity()
        )
        self._capacity: int = max(1, min(10_000, int(cap)))
        self._items: "OrderedDict[str, QAArtifact]" = OrderedDict()
        self._next_seq: int = 1
        self._lock = threading.RLock()

    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def store(
        self,
        *,
        question: object,
        answer: object,
        asked_at_unix: object = 0.0,
        op_id: object = "",
        cost_usd: object = 0.0,
        model: object = "",
        elapsed_s: object = 0.0,
        retrieval_path: object = RETRIEVAL_PATH_CLAUDE_DIRECT,
        top_score: object = 0.0,
    ) -> QAArtifact:
        """Park a Q&A pair. NEVER raises — coerces garbage to
        safe types. Issues a monotonic ``q-N`` ref."""
        try:
            q_str = str(question or "")[:8192]
        except Exception:  # noqa: BLE001
            q_str = ""
        try:
            a_str = str(answer or "")[:65536]
        except Exception:  # noqa: BLE001
            a_str = ""
        try:
            asked = float(asked_at_unix)
        except (TypeError, ValueError):
            asked = 0.0
        try:
            op_id_str = str(op_id or "")[:64]
        except Exception:  # noqa: BLE001
            op_id_str = ""
        try:
            cost = max(0.0, float(cost_usd))
        except (TypeError, ValueError):
            cost = 0.0
        try:
            model_str = str(model or "")[:128]
        except Exception:  # noqa: BLE001
            model_str = ""
        try:
            elapsed = max(0.0, float(elapsed_s))
        except (TypeError, ValueError):
            elapsed = 0.0
        try:
            path_str = str(
                retrieval_path or RETRIEVAL_PATH_CLAUDE_DIRECT,
            )[:64]
        except Exception:  # noqa: BLE001
            path_str = RETRIEVAL_PATH_CLAUDE_DIRECT
        try:
            top_score_f = max(-1.0, min(1.0, float(top_score)))
        except (TypeError, ValueError):
            top_score_f = 0.0
        with self._lock:
            ref = f"{QA_REF_PREFIX}{self._next_seq}"
            self._next_seq += 1
            artifact = QAArtifact(
                ref=ref,
                question=q_str,
                answer=a_str,
                asked_at_unix=asked,
                op_id=op_id_str,
                cost_usd=cost,
                model=model_str,
                elapsed_s=elapsed,
                inserted_at=time.monotonic(),
                retrieval_path=path_str,
                top_score=top_score_f,
            )
            self._items[ref] = artifact
            while len(self._items) > self._capacity:
                self._items.popitem(last=False)
            return artifact

    def lookup(self, ref: object) -> Optional[QAArtifact]:
        """Resolve a `q-N` ref. NEVER raises."""
        if not isinstance(ref, str):
            return None
        with self._lock:
            return self._items.get(ref)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def next_seq(self) -> int:
        """For telemetry; NEVER raises."""
        with self._lock:
            return self._next_seq

    def all_refs(self) -> Tuple[str, ...]:
        """Snapshot of refs in insertion order. NEVER raises."""
        with self._lock:
            return tuple(self._items.keys())

    # ----------------------------------------------------------------
    # §41.3 #26 Phase 2 Slice 4 — canonical ring API for IDE GET
    # surface. Mirrors permission_decision_archive's contract
    # (recent / snapshot / by_op / by_path) so IDE consumers see
    # parallel structure across all artifact-ring substrates.
    # All methods are read-only + NEVER raise.
    # ----------------------------------------------------------------

    def snapshot(self) -> QAStoreSnapshot:
        """Read-only state projection. NEVER raises."""
        with self._lock:
            return QAStoreSnapshot(
                capacity=self._capacity,
                size=len(self._items),
                next_seq=self._next_seq,
            )

    def recent(self, limit: int = 20) -> Tuple[QAArtifact, ...]:
        """Newest-first list of up to ``limit`` artifacts.
        Bounds: limit clamped to [1, capacity]. NEVER raises."""
        try:
            n = max(1, min(int(limit), self._capacity))
        except (TypeError, ValueError):
            n = 20
        with self._lock:
            # OrderedDict preserves insertion order; reverse for
            # newest-first then slice. Tuple is immutable +
            # cheap for callers to project.
            return tuple(reversed(list(self._items.values())))[:n]

    def by_op(
        self, op_id: object, *, limit: int = 100,
    ) -> Tuple[QAArtifact, ...]:
        """Newest-first filter on artifact.op_id (exact match).
        NEVER raises. Empty tuple when op_id is empty / non-str /
        no matches. Q&A typically issues one artifact per op_id
        but this contract supports repeat ops cleanly."""
        if not isinstance(op_id, str) or not op_id:
            return ()
        try:
            n = max(1, min(int(limit), self._capacity))
        except (TypeError, ValueError):
            n = 100
        with self._lock:
            matches = [
                a for a in reversed(list(self._items.values()))
                if a.op_id == op_id
            ]
        return tuple(matches[:n])

    def by_path(
        self, retrieval_path: object, *, limit: int = 50,
    ) -> Tuple[QAArtifact, ...]:
        """Newest-first filter on artifact.retrieval_path (exact
        match). Composes the open-vocabulary RETRIEVAL_PATH_*
        provenance tags (retrieval_only / hybrid_grounded /
        claude_direct / retrieval_disabled). NEVER raises."""
        if (
            not isinstance(retrieval_path, str)
            or not retrieval_path
        ):
            return ()
        try:
            n = max(1, min(int(limit), self._capacity))
        except (TypeError, ValueError):
            n = 50
        with self._lock:
            matches = [
                a for a in reversed(list(self._items.values()))
                if a.retrieval_path == retrieval_path
            ]
        return tuple(matches[:n])


# Singleton accessor — pattern matches existing artifact-ref rings.

_default_store: Optional[BoundedQAStore] = None
_default_store_lock = threading.Lock()


def get_default_qa_store() -> BoundedQAStore:
    """Singleton accessor. NEVER raises."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = BoundedQAStore()
        return _default_store


def reset_default_qa_store() -> None:
    """Test helper — clear singleton."""
    global _default_store
    with _default_store_lock:
        _default_store = None


# ---------------------------------------------------------------------------
# Cost tracking — daily-window read-only ledger (Phase 0 D3a:
# IMMEDIATE budget). Phase 2 D3b will compose
# urgency_router.Route.INFORMATIONAL + cost_governor; for Phase
# 0 we track in-process to honor the budget cap.
# ---------------------------------------------------------------------------


_cost_today_usd: float = 0.0
_cost_today_date: str = ""
_cost_lock = threading.Lock()


def _today_date_str(now_unix: Optional[float] = None) -> str:
    """UTC day boundary. Resets the cost counter at midnight."""
    t = time.time() if now_unix is None else float(now_unix)
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(t))
    except Exception:  # noqa: BLE001
        return ""


def _record_cost(amount_usd: float, *, now_unix: Optional[float] = None) -> None:
    """Atomic in-process daily-cost accumulator. NEVER raises."""
    global _cost_today_usd, _cost_today_date
    try:
        amt = max(0.0, float(amount_usd))
    except (TypeError, ValueError):
        return
    today = _today_date_str(now_unix)
    with _cost_lock:
        if today != _cost_today_date:
            _cost_today_date = today
            _cost_today_usd = 0.0
        _cost_today_usd += amt


def _check_budget_available(
    estimated_usd: float = 0.0,
    *,
    now_unix: Optional[float] = None,
) -> Tuple[bool, float]:
    """Returns ``(has_budget, remaining_usd)``. NEVER raises."""
    global _cost_today_usd, _cost_today_date
    cap = daily_budget_usd()
    today = _today_date_str(now_unix)
    with _cost_lock:
        if today != _cost_today_date:
            _cost_today_date = today
            _cost_today_usd = 0.0
        remaining = cap - _cost_today_usd
        if estimated_usd > remaining:
            return False, remaining
        return True, remaining


def reset_cost_today() -> None:
    """Test helper. NEVER raises."""
    global _cost_today_usd, _cost_today_date
    with _cost_lock:
        _cost_today_usd = 0.0
        _cost_today_date = ""


def cost_today_usd() -> float:
    """Telemetry accessor. NEVER raises."""
    with _cost_lock:
        return _cost_today_usd


# ---------------------------------------------------------------------------
# Provider callable — operator-injectable interface
#
# Phase 0 D2a: Claude-direct backend. The injectable interface
# lets tests provide fakes + lets operators wire their own
# backend (semantic_index in Phase 1, custom RAG in Phase 3+).
# ---------------------------------------------------------------------------


# Return type: (answer_text, cost_usd). Cost is provider's
# best-estimate per Anthropic's usage stats. NEVER raises is
# the callable's contract — callers wrap in try/except for
# defense-in-depth.
ProviderCallable = Callable[[str, str], Awaitable[Tuple[str, float]]]


async def _default_claude_callable(
    system: str, user_question: str,
) -> Tuple[str, float]:
    """Default Phase 0 D2a backend — direct Claude SDK call.

    Composes the Anthropic SDK using the same env knobs the
    existing :mod:`providers` module uses (``ANTHROPIC_API_KEY``,
    operator-tunable model selection via
    ``JARVIS_FAST_PATH_QA_MODEL``). Does NOT compose the heavy
    :class:`ClaudeProvider.generate` path — that's tool-loop +
    cost-contract + budget-gated for the code-generation pipeline.
    Q&A is read-only by definition (per §41.3.1 non-decision #1).

    NEVER raises into the caller — exceptions degrade to
    ``("", 0.0)`` so the substrate's verdict path can map to
    PROVIDER_FAILED. Returns ``(answer_text, cost_usd_estimate)``.
    """
    try:
        import anthropic  # type: ignore[import-untyped]  # noqa: F401 — kept for ImportError gate
    except ImportError:
        return ("", 0.0)
    api_key = os.environ.get(_ENV_ANTHROPIC_KEY, "").strip()
    if not api_key:
        return ("", 0.0)
    # Slice 2B-ii — route through Aegis Provider Bridge.
    from backend.core.ouroboros.governance.aegis_provider_bridge import (
        acquire_call_lease as _aegis_acquire_call_lease,
        make_async_anthropic_client as _aegis_make_anthropic,
        merge_lease_header as _aegis_merge_lease,
    )
    try:
        client = _aegis_make_anthropic(api_key=api_key)
    except Exception:  # noqa: BLE001
        return ("", 0.0)
    try:
        # Per-call Aegis lease (FastPathQA is a read-only Q&A surface;
        # synthetic op_id tags the path for cap accounting).
        _aegis_lease = await _aegis_acquire_call_lease(
            op_id="fast-path-qa",
            route="standard",
            estimated_cost_usd=0.005,
        )
        resp = await client.messages.create(
            model=model_name(),
            max_tokens=max_tokens(),
            temperature=temperature(),
            system=system,
            messages=[{"role": "user", "content": user_question}],
            extra_headers=_aegis_merge_lease(None, _aegis_lease),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] Claude call failed: %r", exc,
        )
        return ("", 0.0)
    # Extract text from response — Anthropic SDK returns a list
    # of content blocks; we concatenate text blocks.
    try:
        answer_parts: List[str] = []
        for block in (resp.content or []):
            txt = getattr(block, "text", None)
            if isinstance(txt, str):
                answer_parts.append(txt)
        answer = "".join(answer_parts).strip()
    except Exception:  # noqa: BLE001
        answer = ""
    # Cost estimate — Claude Sonnet 4.6 pricing (operator can
    # override via env if model changes). Tokens come from
    # resp.usage; degrades to 0.0 when unavailable.
    try:
        usage = getattr(resp, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        # $3 / $15 per Mtoks — close enough for budget tracking
        # in Phase 0. Phase 2 will route through cost_governor's
        # canonical pricing table.
        cost = (
            (input_tokens * 3.0 / 1_000_000)
            + (output_tokens * 15.0 / 1_000_000)
        )
    except Exception:  # noqa: BLE001
        cost = 0.0
    return (answer, cost)


# ---------------------------------------------------------------------------
# Composers — ConversationBridge (D4 default)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Phase 1 D2c retrieval composer
# ---------------------------------------------------------------------------
#
# Composes semantic_index.get_default_index + top_k_for_text.
# NO parallel embedder, NO parallel corpus, NO duplicate cosine
# math. Read-only over the canonical index — retrieval does NOT
# contribute signals to the centroid (asymmetric: score() reads-
# and-records, this helper reads-only).


@dataclass(frozen=True)
class _RetrievalResult:
    """Outcome of one retrieve_context call."""

    top_score: float
    snippets: Tuple[Tuple[str, str, float], ...]
    item_count: int
    elapsed_s: float
    diagnostic: str = ""


RetrievalCallable = Callable[
    [str, int, float],
    Awaitable[Tuple[Tuple[Any, float], ...]],
]


async def _default_semantic_retrieval(
    query: str, k: int, min_score: float,
) -> Tuple[Tuple[Any, float], ...]:
    """Default Phase 1 retriever. Composes semantic_index
    singleton. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501  # type: ignore[import-not-found]
            get_default_index,
        )
    except Exception:  # noqa: BLE001
        return ()
    try:
        index = get_default_index()
    except Exception:  # noqa: BLE001
        return ()
    if index is None:
        return ()
    try:
        return index.top_k_for_text(
            query, k=k, min_score=min_score,
        )
    except Exception:  # noqa: BLE001
        return ()


async def _retrieve_context(
    question: str,
    *,
    retrieval_callable: Optional[RetrievalCallable] = None,
    now_unix: Optional[float] = None,
) -> _RetrievalResult:
    """Top-K retrieval over the canonical semantic_index.
    NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)
    fn = (
        retrieval_callable if retrieval_callable is not None
        else _default_semantic_retrieval
    )
    try:
        raw = await fn(question, retrieval_top_k(), 0.0)
    except Exception as exc:  # noqa: BLE001
        return _RetrievalResult(
            top_score=0.0,
            snippets=(),
            item_count=0,
            elapsed_s=max(0.0, time.time() - started),
            diagnostic=f"retrieval raised: {exc!r}"[:200],
        )
    if not raw:
        return _RetrievalResult(
            top_score=0.0,
            snippets=(),
            item_count=0,
            elapsed_s=max(0.0, time.time() - started),
            diagnostic="empty corpus or embedder offline",
        )
    snippets: List[Tuple[str, str, float]] = []
    top = -1.0
    for pair in raw:
        try:
            item, score = pair[0], float(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        try:
            txt = str(getattr(item, "text", "") or "")[:512]
            src = str(getattr(item, "source", "") or "")[:64]
        except Exception:  # noqa: BLE001
            continue
        if not txt:
            continue
        snippets.append((src, txt, score))
        if score > top:
            top = score
    top_clamped = max(0.0, top) if top >= 0 else 0.0
    return _RetrievalResult(
        top_score=top_clamped,
        snippets=tuple(snippets),
        item_count=len(snippets),
        elapsed_s=max(0.0, time.time() - started),
        diagnostic=f"retrieved {len(snippets)} item(s)",
    )


def _format_snippets_for_claude_prompt(
    snippets: Tuple[Tuple[str, str, float], ...],
) -> str:
    """Render retrieved snippets as a system-prompt block to
    ground Claude. NEVER raises."""
    if not snippets:
        return ""
    lines: List[str] = [
        "",
        "Relevant project context (retrieved from the "
        "semantic index — excerpts from CLAUDE.md, the PRD, "
        "prior commits, and conversation history; ground "
        "your answer but do NOT quote verbatim unless "
        "directly relevant):",
        "",
    ]
    for i, (src, text, score) in enumerate(snippets, start=1):
        lines.append(
            f"[{i}] (source={src}, relevance={score:.2f}):"
        )
        lines.append(f"    {text}")
    return "\n".join(lines)


def _format_snippets_for_operator_answer(
    snippets: Tuple[Tuple[str, str, float], ...],
) -> str:
    """High-confidence retrieval-only path. Renders top snippets
    as the operator-facing answer. NEVER raises."""
    if not snippets:
        return ""
    lines: List[str] = [
        "Based on the project's semantic index, the most "
        "relevant context for your question is:",
        "",
    ]
    for i, (src, text, score) in enumerate(snippets, start=1):
        lines.append(
            f"  [{i}] ({src}, relevance {score:.2f}):"
        )
        lines.append(f"      {text}")
    lines.append("")
    lines.append(
        "(retrieved without invoking Claude — $0 cost; set "
        f"{_ENV_RETRIEVAL_ENABLED}=false to force Claude "
        "synthesis.)"
    )
    return "\n".join(lines)


def _get_cost_governor_safely() -> Any:
    """Compose :func:`cost_governor.get_default_cost_governor`.
    NEVER raises. Returns the singleton governor or None when
    the canonical governor is unavailable / disabled — caller
    must defensive-check the result."""
    try:
        from backend.core.ouroboros.governance.cost_governor import (  # noqa: E501  # type: ignore[import-not-found]
            get_default_cost_governor,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        return get_default_cost_governor()
    except Exception:  # noqa: BLE001
        return None


def _cost_governor_start_safely(
    governor: Any, op_id: str,
) -> Optional[float]:
    """Register a Q&A op with the canonical cost governor. Q&A
    is always:
      * route = ROUTE_INFORMATIONAL (the canonical INFORMATIONAL
        route value, matching cost_governor's route_factors
        entry added 2026-05-11)
      * complexity = "simple" (small-token Sonnet calls, no
        tool-loop, no codegen — §41.3.1 non-decision #1)
      * is_read_only = True (read-only by definition)

    NEVER raises — defensive-degrades to None so the substrate
    falls back to the daily aggregate counter alone."""
    if governor is None or not op_id:
        return None
    try:
        return governor.start(
            op_id, ROUTE_INFORMATIONAL, "simple",
            is_read_only=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] cost_governor.start failed: %r", exc,
        )
        return None


def _cost_governor_remaining_safely(
    governor: Any, op_id: str,
) -> Optional[float]:
    """Read per-op remaining cap. NEVER raises."""
    if governor is None or not op_id:
        return None
    try:
        rem = governor.remaining(op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] cost_governor.remaining failed: %r",
            exc,
        )
        return None
    try:
        return float(rem)
    except (TypeError, ValueError):
        return None


def _cost_governor_charge_safely(
    governor: Any, op_id: str, cost_usd: float,
) -> None:
    """Attribute realized cost. NEVER raises."""
    if governor is None or not op_id:
        return
    try:
        governor.charge(op_id, float(cost_usd), "claude")
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] cost_governor.charge failed: %r", exc,
        )


def _cost_governor_finish_safely(
    governor: Any, op_id: str,
) -> None:
    """Finalize op (fires finalize_observer chain). NEVER raises."""
    if governor is None or not op_id:
        return
    try:
        governor.finish(op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] cost_governor.finish failed: %r", exc,
        )


def _publish_qa_recorded_safely(artifact: QAArtifact) -> None:
    """§41.3 #26 Phase 2 Slice 3 — producer-bridge for the
    canonical ``qa_recorded`` SSE event.

    Composes :func:`ide_observability_stream.publish_task_event`
    via lazy import (mirrors the v2.91 permission_decision_archive
    pattern). Fires AFTER ``qa_store.store()`` returns — the
    artifact has already been parked + given its monotonic q-N
    ref by the time we publish.

    Best-effort contract: broker exceptions are swallowed at
    BOTH levels (publish_task_event itself never raises; the
    outer try/except here is defense-in-depth in case the lazy
    import or the symbol lookup fails). SSE broker failure MUST
    NOT propagate into the Q&A pipeline.

    Stream-side gate (``JARVIS_IDE_STREAM_ENABLED``) is checked
    inside ``publish_task_event``; substrate-side gate
    (``JARVIS_FAST_PATH_QA_ENABLED``) is checked at the master
    flag — when off, ``ask_question`` short-circuits before
    reaching this helper.
    """
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501  # type: ignore[import-not-found]
            EVENT_TYPE_QA_RECORDED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_QA_RECORDED,
            artifact.op_id,
            artifact.to_dict(),
        )
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug(
            "[FastPathQA] qa_recorded SSE publish failed: %r",
            exc,
        )


def _record_turn_safely(
    role: str, text: str, *, source: str, op_id: str = "",
) -> None:
    """Compose :func:`conversation_bridge.get_default_bridge`.
    NEVER raises — D4 default per §41.3.1 says Q&A turns ARE
    recorded so subsequent ops have context."""
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501  # type: ignore[import-not-found]
            get_default_bridge,
        )
        bridge = get_default_bridge()
        if bridge is None:
            return
        bridge.record_turn(role, text, source=source, op_id=op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] bridge.record_turn(%r) failed: %r",
            source, exc,
        )


# ---------------------------------------------------------------------------
# Top-level entry — ask_question
# ---------------------------------------------------------------------------


async def ask_question(
    question: object,
    *,
    op_id: str = "",
    store: Optional[BoundedQAStore] = None,
    provider_callable: Optional[ProviderCallable] = None,
    bridge_callable: Optional[
        Callable[[str, str, str, str], None]
    ] = None,
    retrieval_callable: Optional[RetrievalCallable] = None,
    now_unix: Optional[float] = None,
) -> QAReport:
    """Top-level Q&A entry. NEVER raises.

    Pipeline (Phase 0):
      1. Master flag gate → DISABLED
      2. Question coercion + validation → OUT_OF_SCOPE on empty
      3. Budget check (D3a IMMEDIATE) → BUDGET_EXHAUSTED
      4. Record user turn via ConversationBridge (D4 default)
      5. Provider callable invocation (D2a Claude-direct;
         operator can inject a fake / semantic_index hybrid)
      6. Empty answer → PROVIDER_FAILED
      7. Record cost; record assistant turn via ConversationBridge
      8. Park artifact in :class:`BoundedQAStore` → `q-N` ref
      9. Return QAReport with ANSWERED + artifact

    Parameters
    ----------
    question:
        Operator-typed question. Coerced via ``str(...)``; empty
        / non-string → OUT_OF_SCOPE.
    op_id:
        Correlation handle for ConversationBridge + audit.
    store:
        Sibling ring instance. None → uses singleton via
        :func:`get_default_qa_store`.
    provider_callable:
        Operator-injectable provider. None → composes
        :func:`_default_claude_callable`.
    bridge_callable:
        Operator-injectable conversation-bridge recorder for
        hermetic testing. None → composes
        :func:`_record_turn_safely`.
    now_unix:
        Time override for tests.
    """
    started = time.time() if now_unix is None else float(now_unix)

    # Step 1: master flag gate.
    if not master_enabled():
        return QAReport(
            verdict=QAVerdict.DISABLED,
            artifact=None,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            evaluated_at_unix=started,
        )

    # Step 2: input validation.
    try:
        q_text = str(question or "").strip()
    except Exception:  # noqa: BLE001
        q_text = ""
    if not q_text:
        return QAReport(
            verdict=QAVerdict.OUT_OF_SCOPE,
            artifact=None,
            diagnostic="empty / non-string question",
            evaluated_at_unix=started,
        )
    if len(q_text) > 4096:
        q_text = q_text[:4096]

    # Step 3a: daily aggregate budget check (Q&A-substrate-local —
    # no canonical "daily per-route aggregate" surface exists,
    # so this remains a substrate-local rolling counter).
    has_budget, remaining = _check_budget_available(
        now_unix=started,
    )
    if not has_budget:
        return QAReport(
            verdict=QAVerdict.BUDGET_EXHAUSTED,
            artifact=None,
            diagnostic=(
                f"daily Q&A budget exhausted "
                f"(remaining=${remaining:.4f}; "
                f"cap=${daily_budget_usd():.2f} via "
                f"{_ENV_INFORMATIONAL_BUDGET_USD} or "
                f"{_ENV_BUDGET_USD})"
            ),
            evaluated_at_unix=started,
        )

    # Step 3b: §41.3 #26 Phase 2 D3b — register Q&A op with the
    # canonical cost_governor for per-op cap tracking + cross-
    # substrate cost attribution. Returns None when the
    # governor is unavailable or composition is disabled — in
    # which case we fall back to the daily aggregate counter
    # alone (Phase 0/1 behavior). The governor and op_id are
    # both required downstream for the pre-call cap check and
    # the post-call charge.
    gov = (
        _get_cost_governor_safely()
        if (compose_cost_governor_enabled() and op_id)
        else None
    )
    if gov is not None:
        _cost_governor_start_safely(gov, op_id)
        per_op_remaining = _cost_governor_remaining_safely(
            gov, op_id,
        )
        # Pre-call cap check. cost_governor.is_exceeded is
        # post-hoc (cumulative >= cap); for pre-flight we use
        # remaining >= 0. Re-issued ops with cumulative spend
        # already past the cap will short-circuit here without
        # invoking the provider.
        if (
            per_op_remaining is not None
            and per_op_remaining <= 0.0
        ):
            _cost_governor_finish_safely(gov, op_id)
            return QAReport(
                verdict=QAVerdict.BUDGET_EXHAUSTED,
                artifact=None,
                diagnostic=(
                    f"per-op cost cap exhausted "
                    f"(cost_governor: route={ROUTE_INFORMATIONAL}, "
                    f"op={op_id[:12]!r}, remaining="
                    f"${per_op_remaining:.4f})"
                ),
                evaluated_at_unix=started,
            )

    # Step 4: record user turn via ConversationBridge (D4
    # default). Operator-injectable for tests.
    recorder = (
        bridge_callable if bridge_callable is not None
        else (lambda role, text, source, op: _record_turn_safely(
            role, text, source=source, op_id=op,
        ))
    )
    try:
        recorder("user", q_text, "ask_human_q", op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] user-turn record failed: %r", exc,
        )

    # Step 5: §41.3 #26 Phase 1 — D2c hybrid retrieval.
    # 3-tier confidence ladder; operator-tunable thresholds:
    #
    #   * HIGH (top_score >= high_threshold): retrieved
    #     snippets ARE the answer — no Claude call, $0 cost.
    #   * MEDIUM (top_score >= low_threshold): inject snippets
    #     into Claude's system prompt for grounding.
    #   * LOW (no snippets or below low threshold): Phase 0
    #     Claude-direct path unchanged.
    #
    # Retrieval-disabled / failed → falls through cleanly to
    # the LOW-confidence Phase 0 path (byte-identical to Phase
    # 0 behavior under that flag).
    retrieval_path = RETRIEVAL_PATH_RETRIEVAL_DISABLED
    top_score = 0.0
    retrieved_snippets: Tuple[Tuple[str, str, float], ...] = ()
    if retrieval_enabled():
        retrieval = await _retrieve_context(
            q_text,
            retrieval_callable=retrieval_callable,
            now_unix=started,
        )
        top_score = retrieval.top_score
        retrieved_snippets = retrieval.snippets
        high_t = retrieval_high_confidence_threshold()
        low_t = retrieval_low_confidence_threshold()
        # Tier 1: HIGH confidence → retrieval-only path.
        if (
            retrieved_snippets
            and top_score >= high_t
        ):
            answer_text = _format_snippets_for_operator_answer(
                retrieved_snippets,
            )
            try:
                recorder(
                    "assistant", answer_text,
                    "ask_human_a", op_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[FastPathQA] retrieval-only "
                    "assistant-turn record failed: %r", exc,
                )
            qa_store = (
                store if store is not None
                else get_default_qa_store()
            )
            elapsed = max(0.0, time.time() - started)
            artifact = qa_store.store(
                question=q_text,
                answer=answer_text,
                asked_at_unix=started,
                op_id=op_id,
                cost_usd=0.0,  # no Claude call
                model="semantic_index",  # provenance
                elapsed_s=elapsed,
                retrieval_path=RETRIEVAL_PATH_RETRIEVAL_ONLY,
                top_score=top_score,
            )
            # §41.3 #26 Phase 2 Slice 3 — publish qa_recorded SSE
            # AFTER the artifact is parked (q-N ref allocated).
            # Best-effort; broker exceptions never raise.
            _publish_qa_recorded_safely(artifact)
            # Finalize cost_governor op (fires finalize_observer
            # chain). Retrieval-only paths charge $0 — the
            # governor still gets the op's lifecycle for cross-
            # substrate observability.
            _cost_governor_finish_safely(gov, op_id)
            return QAReport(
                verdict=QAVerdict.ANSWERED,
                artifact=artifact,
                diagnostic=(
                    f"ok retrieval-only (top_score="
                    f"{top_score:.3f} >= {high_t:.2f}, "
                    f"ref={artifact.ref}, $0)"
                ),
                evaluated_at_unix=started,
            )
        # Tier 2: MEDIUM confidence → snippets ground Claude.
        if (
            retrieved_snippets
            and top_score >= low_t
        ):
            retrieval_path = RETRIEVAL_PATH_HYBRID
        else:
            retrieval_path = RETRIEVAL_PATH_CLAUDE_DIRECT

    # Step 6: build Claude system prompt (with or without
    # retrieved context injection).
    base_prompt = system_prompt()
    if retrieval_path == RETRIEVAL_PATH_HYBRID:
        ctx_block = _format_snippets_for_claude_prompt(
            retrieved_snippets,
        )
        effective_prompt = base_prompt + "\n\n" + ctx_block
    else:
        effective_prompt = base_prompt

    # Step 7: invoke provider callable.
    provider = (
        provider_callable
        if provider_callable is not None
        else _default_claude_callable
    )
    try:
        answer, cost = await asyncio.wait_for(
            provider(effective_prompt, q_text),
            timeout=float(timeout_s()),
        )
    except asyncio.TimeoutError:
        _cost_governor_finish_safely(gov, op_id)
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic=f"provider timeout after {timeout_s()}s",
            evaluated_at_unix=started,
        )
    except Exception as exc:  # noqa: BLE001
        _cost_governor_finish_safely(gov, op_id)
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic=f"provider raised: {exc!r}"[:200],
            evaluated_at_unix=started,
        )

    # Step 8: empty answer guard.
    try:
        answer_text = str(answer or "").strip()
        cost_usd = max(0.0, float(cost))
    except Exception:  # noqa: BLE001
        answer_text = ""
        cost_usd = 0.0
    if not answer_text:
        _cost_governor_finish_safely(gov, op_id)
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic="provider returned empty answer",
            evaluated_at_unix=started,
        )

    # Step 9: record cost across BOTH axes:
    #   * Q&A-substrate daily aggregate (rolling daily counter).
    #   * Canonical cost_governor per-op cumulative spend (cross-
    #     substrate observability via finalize_observer chain).
    _record_cost(cost_usd, now_unix=started)
    _cost_governor_charge_safely(gov, op_id, cost_usd)
    try:
        recorder("assistant", answer_text, "ask_human_a", op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] assistant-turn record failed: %r", exc,
        )

    # Step 10: park artifact — gets q-N ref.
    qa_store = (
        store if store is not None else get_default_qa_store()
    )
    elapsed = max(0.0, time.time() - started)
    artifact = qa_store.store(
        question=q_text,
        answer=answer_text,
        asked_at_unix=started,
        op_id=op_id,
        cost_usd=cost_usd,
        model=model_name(),
        elapsed_s=elapsed,
        retrieval_path=retrieval_path,
        top_score=top_score,
    )

    # §41.3 #26 Phase 2 Slice 3 — publish qa_recorded SSE AFTER
    # the artifact is parked (q-N ref allocated). Best-effort;
    # broker exceptions never raise.
    _publish_qa_recorded_safely(artifact)

    # Step 11: finalize cost_governor op (fires finalize_observer
    # chain for cross-substrate observability — e.g.,
    # CostWarningObserver band-crossing SSE events).
    _cost_governor_finish_safely(gov, op_id)

    # Step 12: return ANSWERED with path provenance.
    return QAReport(
        verdict=QAVerdict.ANSWERED,
        artifact=artifact,
        diagnostic=(
            f"ok (cost=${cost_usd:.5f}, {elapsed:.2f}s, "
            f"ref={artifact.ref}, path={retrieval_path}, "
            f"top_score={top_score:.3f})"
        ),
        evaluated_at_unix=started,
    )


# ---------------------------------------------------------------------------
# AST pins — invariants future refactors must not break
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered by shipped_code_invariants module walker."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501  # type: ignore[import-not-found]
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/fast_path_qa.py"
    )

    _EXPECTED_VERDICTS = {
        "answered", "disabled", "budget_exhausted",
        "provider_failed", "out_of_scope",
    }

    def _validate_verdict_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "QAVerdict"
            ):
                found = set()
                for sub in node.body:
                    if (
                        isinstance(sub, ast.Assign)
                        and len(sub.targets) == 1
                        and isinstance(sub.targets[0], ast.Name)
                        and isinstance(sub.value, ast.Constant)
                        and isinstance(sub.value.value, str)
                    ):
                        found.add(sub.value.value)
                if found != _EXPECTED_VERDICTS:
                    return (
                        f"QAVerdict drift: got={sorted(found)} "
                        f"expected={sorted(_EXPECTED_VERDICTS)}",
                    )
                return ()
        return ("QAVerdict class not found",)

    def _validate_qa_ref_prefix_pinned(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin: QA_REF_PREFIX is exactly "q-" to slot
        cleanly into the t-N/d-N/o-N/n-N/p-N artifact-ref family."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "QA_REF_PREFIX"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "q-"
            ):
                return ()
        return (
            "QA_REF_PREFIX must be \"q-\" — mirrors the "
            "existing artifact-ref family",
        )

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) with "
                    "default=False per §33.1 + operator-binding gate",
                )
        return ("master_enabled() not found",)

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(f"forbidden import: {mod}")
        return tuple(violations)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        # D4 default — must compose ConversationBridge for turn capture
        if "conversation_bridge" not in source:
            violations.append(
                "must compose conversation_bridge "
                "(D4 default — turns recorded via "
                "get_default_bridge)"
            )
        # D2a Phase 0 — must use anthropic SDK directly (NOT
        # ClaudeProvider's heavy generate path)
        if "anthropic" not in source:
            violations.append(
                "must compose anthropic SDK directly "
                "(D2a Phase 0 Claude-direct backend)"
            )
        # D5c — must mirror BoundedBodyStore pattern
        if "OrderedDict" not in source or "threading.RLock" not in source:
            violations.append(
                "must mirror BoundedBodyStore pattern "
                "(OrderedDict + threading.RLock + monotonic "
                "seq + drop-oldest)"
            )
        # D2c Phase 1 — must compose canonical semantic_index
        # (no parallel embedder, no parallel corpus). The
        # retrieval composer references get_default_index +
        # top_k_for_text.
        if "semantic_index" not in source:
            violations.append(
                "must compose semantic_index "
                "(D2c Phase 1 hybrid retrieval — no parallel "
                "embedding pipeline, no duplicate corpus)"
            )
        if "top_k_for_text" not in source:
            violations.append(
                "must invoke SemanticIndex.top_k_for_text "
                "(D2c Phase 1 — the canonical retrieval method "
                "we added to semantic_index for this use)"
            )
        # §41.3 #26 Phase 2 D3b — must compose canonical
        # cost_governor for per-op cost attribution. The
        # operator binding 2026-05-11 forbids parallel cost
        # state; cost_governor is the canonical surface.
        if "cost_governor" not in source:
            violations.append(
                "must compose cost_governor "
                "(Phase 2 D3b — per-op cost attribution + "
                "cross-substrate observability)"
            )
        if "get_default_cost_governor" not in source:
            violations.append(
                "must call get_default_cost_governor "
                "(canonical singleton accessor — no parallel "
                "governor instance)"
            )
        # §41.3 #26 Phase 2 Slice 3 — must compose canonical
        # SSE event-publish for qa_recorded. Closes the
        # observability-triad parity gap with every other
        # artifact-ring substrate.
        if "EVENT_TYPE_QA_RECORDED" not in source:
            violations.append(
                "must reference EVENT_TYPE_QA_RECORDED "
                "(Phase 2 Slice 3 — canonical SSE event for "
                "Q&A artifact-record beacon)"
            )
        if "publish_task_event" not in source:
            violations.append(
                "must compose publish_task_event "
                "(canonical SSE broker producer-bridge — "
                "no parallel publisher)"
            )
        return tuple(violations)

    def _validate_route_informational_pinned(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """§41.3 #26 Phase 2 D3b bytes-pin: ROUTE_INFORMATIONAL
        is exactly "informational" — must match
        :data:`urgency_router.ProviderRoute.INFORMATIONAL.value`.

        Authority-asymmetry forbids importing ``urgency_router``
        here; the parity contract is enforced structurally by
        this pin (constant value) + behaviorally by
        ``test_fast_path_qa_phase2.test_route_informational_matches_canonical``
        (which DOES import ProviderRoute and asserts equality)."""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "ROUTE_INFORMATIONAL"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "informational"
            ):
                return ()
        return (
            "ROUTE_INFORMATIONAL must be 'informational' — "
            "mirrors ProviderRoute.INFORMATIONAL.value "
            "(closed-5→6 taxonomy expansion, operator-signed "
            "2026-05-11 per §41.3.1 D3b)",
        )

    def _validate_d2a_no_provider_generate(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        """Bytes-pin: Phase 0 D2a does NOT compose
        ClaudeProvider.generate (heavy tool-loop + cost-contract
        + budget-gated path designed for code generation). Q&A
        is read-only by definition (§41.3.1 non-decision #1).

        AST-checks for ``ClaudeProvider`` IMPORTS and attribute
        accesses — docstring mentions are intentionally allowed
        (this pin's own description references it for clarity)."""
        violations: List[str] = []
        for node in ast.walk(tree):
            # ImportFrom: from ... import ClaudeProvider
            if isinstance(node, ast.ImportFrom):
                for alias in (node.names or []):
                    if alias.name == "ClaudeProvider":
                        violations.append(
                            "imports ClaudeProvider directly",
                        )
            # Attribute access: providers.ClaudeProvider
            if isinstance(node, ast.Attribute):
                if node.attr == "ClaudeProvider":
                    violations.append(
                        "attribute access to ClaudeProvider",
                    )
            # Bare name reference: ClaudeProvider(...) call
            if (
                isinstance(node, ast.Name)
                and node.id == "ClaudeProvider"
                and isinstance(
                    getattr(node, "ctx", None), ast.Load,
                )
            ):
                violations.append(
                    "load-context reference to ClaudeProvider",
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "fast_path_qa_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "QAVerdict 5-value taxonomy bytes-pinned. New "
                "values require explicit scope-doc + AST pin "
                "update."
            ),
            validate=_validate_verdict_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name="fast_path_qa_ref_prefix_pinned",
            target_file=target,
            description=(
                "QA_REF_PREFIX = 'q-' joins the existing "
                "t-N/d-N/o-N/n-N/p-N artifact-ref family. "
                "Operator binding: NO parallel ref dispatcher."
            ),
            validate=_validate_qa_ref_prefix_pinned,
        ),
        ShippedCodeInvariant(
            invariant_name="fast_path_qa_master_default_false",
            target_file=target,
            description=(
                "§33.1 + operator-binding gate. Even with D1-D5 "
                "approved, runtime traffic stays gated."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="fast_path_qa_authority_asymmetry",
            target_file=target,
            description=(
                "Substrate MUST NOT import orchestrator / "
                "iron_gate / policy / etc — read-only Q&A path."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="fast_path_qa_composes_canonical",
            target_file=target,
            description=(
                "Composes conversation_bridge (D4 default) + "
                "anthropic SDK (D2a Phase 0) + BoundedBodyStore "
                "pattern (D5c). NO parallel state."
            ),
            validate=_validate_composes_canonical,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "fast_path_qa_route_informational_pinned"
            ),
            target_file=target,
            description=(
                "§41.3 #26 Phase 2 D3b bytes-pin. "
                "ROUTE_INFORMATIONAL = 'informational' mirrors "
                "ProviderRoute.INFORMATIONAL.value (closed-5→6 "
                "expansion, operator-signed 2026-05-11). "
                "Authority-asymmetry forbids importing "
                "urgency_router; parity is structural + tested."
            ),
            validate=_validate_route_informational_pinned,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "fast_path_qa_no_provider_generate"
            ),
            target_file=target,
            description=(
                "Phase 0 D2a MUST use the anthropic SDK "
                "directly, NOT ClaudeProvider.generate (heavy "
                "tool-loop + cost-contract path for code gen). "
                "§41.3.1 non-decision #1: Q&A is read-only."
            ),
            validate=_validate_d2a_no_provider_generate,
        ),
    ]


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = "backend/core/ouroboros/governance/fast_path_qa.py"
    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Fast-Path Q&A master. §33.1 default-FALSE + "
                "operator-binding gate. §41.3 #26 Phase 0 — "
                "operator approved D1c/D2c/D3b/D4=defaults/D5c "
                "on 2026-05-11; runtime traffic still gated."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_INFORMATIONAL_BUDGET_USD,
            type=FlagType.FLOAT,
            default=_DEFAULT_BUDGET_USD,
            description=(
                "§41.3 #26 Phase 2 D3b — canonical per-route "
                "INFORMATIONAL budget cap (USD/day). Takes "
                "precedence over the legacy "
                "JARVIS_FAST_PATH_QA_DAILY_BUDGET_USD knob. "
                "Operator-signed 2026-05-11. Clamped [0, 1000]."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_INFORMATIONAL_BUDGET_USD}=10.0",
        ),
        FlagSpec(
            name=_ENV_BUDGET_USD,
            type=FlagType.FLOAT,
            default=_DEFAULT_BUDGET_USD,
            description=(
                "Daily USD budget cap for Q&A (Phase 0/1 "
                "legacy knob). Phase 2+ should prefer the "
                "canonical JARVIS_INFORMATIONAL_BUDGET_USD; "
                "this name is retained for backward-compat "
                "with existing operator muscle memory."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_BUDGET_USD}=10.0",
        ),
        FlagSpec(
            name=_ENV_MAX_TOKENS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_TOKENS,
            description=(
                "Max output tokens per Q&A. Capped to keep "
                "answers concise."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MAX_TOKENS}=600",
        ),
        FlagSpec(
            name=_ENV_TEMPERATURE,
            type=FlagType.FLOAT,
            default=_DEFAULT_TEMPERATURE,
            description=(
                "Sampling temperature for Q&A answers. Lower = "
                "more deterministic."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_TEMPERATURE}=0.0",
        ),
        FlagSpec(
            name=_ENV_STORE_CAPACITY,
            type=FlagType.INT,
            default=_DEFAULT_STORE_CAPACITY,
            description=(
                "BoundedQAStore ring capacity. Q&A artifacts "
                "evict drop-oldest beyond this count."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_STORE_CAPACITY}=200",
        ),
        FlagSpec(
            name=_ENV_SYSTEM_PROMPT,
            type=FlagType.STR,
            default="",
            description=(
                "Override the curated default system prompt. "
                "Empty / unset → default cites CLAUDE.md / PRD "
                "as the project knowledge anchor."
            ),
            category=Category.TUNING,
            source_file=src,
            example=(
                f"{_ENV_SYSTEM_PROMPT}='Answer only with code "
                f"snippets'"
            ),
        ),
        FlagSpec(
            name=_ENV_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_TIMEOUT_S,
            description="Provider-call timeout (seconds).",
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_TIMEOUT_S}=60",
        ),
        FlagSpec(
            name=_ENV_MODEL,
            type=FlagType.STR,
            default=_DEFAULT_MODEL,
            description=(
                "Claude model name. Default "
                f"{_DEFAULT_MODEL!r}. Operator can pin a "
                "different model without touching code."
            ),
            category=Category.ROUTING,
            source_file=src,
            example=f"{_ENV_MODEL}=claude-haiku-4-5",
        ),
        FlagSpec(
            name=_ENV_RETRIEVAL_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "§41.3 #26 Phase 1 D2c hybrid retrieval. "
                "Default-TRUE (conditional on master). When "
                "on, ask_question composes semantic_index "
                "top_k_for_text BEFORE invoking Claude. "
                "Operator opt-out: set to false to fall back "
                "to Phase 0 Claude-direct path."
            ),
            category=Category.ROUTING,
            source_file=src,
            example=f"{_ENV_RETRIEVAL_ENABLED}=false",
        ),
        FlagSpec(
            name=_ENV_RETRIEVAL_HIGH_CONFIDENCE,
            type=FlagType.FLOAT,
            default=_DEFAULT_RETRIEVAL_HIGH_CONFIDENCE,
            description=(
                "Cosine threshold above which retrieval ALONE "
                "answers (no Claude call, $0 cost). Clamped "
                "[0.0, 1.0]. Tune per corpus characteristics."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_RETRIEVAL_HIGH_CONFIDENCE}=0.65",
        ),
        FlagSpec(
            name=_ENV_RETRIEVAL_LOW_CONFIDENCE,
            type=FlagType.FLOAT,
            default=_DEFAULT_RETRIEVAL_LOW_CONFIDENCE,
            description=(
                "Cosine threshold above which retrieval "
                "grounds Claude (context injected into system "
                "prompt). Below → pure Claude-direct (Phase 0 "
                "path). Clamped [0.0, 1.0]."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_RETRIEVAL_LOW_CONFIDENCE}=0.40",
        ),
        FlagSpec(
            name=_ENV_RETRIEVAL_TOP_K,
            type=FlagType.INT,
            default=_DEFAULT_RETRIEVAL_TOP_K,
            description=(
                "Number of corpus items to retrieve. Clamped "
                "[1, 50]. More items → more context but more "
                "tokens consumed under the hybrid path."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_RETRIEVAL_TOP_K}=8",
        ),
        FlagSpec(
            name=_ENV_COMPOSE_COST_GOVERNOR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "§41.3 #26 Phase 2 D3b — compose canonical "
                "cost_governor for per-op cost attribution. "
                "Default-TRUE (conditional on master). When "
                "off, falls back to in-process daily counter "
                "alone (no per-op caps, no cross-substrate "
                "cost observability)."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_COMPOSE_COST_GOVERNOR}=false",
        ),
    ]
    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "FAST_PATH_QA_SCHEMA_VERSION",
    "QA_REF_PREFIX",
    "RETRIEVAL_PATH_CLAUDE_DIRECT",
    "RETRIEVAL_PATH_HYBRID",
    "RETRIEVAL_PATH_RETRIEVAL_DISABLED",
    "RETRIEVAL_PATH_RETRIEVAL_ONLY",
    "ROUTE_INFORMATIONAL",
    "BoundedQAStore",
    "QAArtifact",
    "QAReport",
    "QAStoreSnapshot",
    "QAVerdict",
    "ask_question",
    "cost_today_usd",
    "daily_budget_usd",
    "get_default_qa_store",
    "master_enabled",
    "max_tokens",
    "model_name",
    "register_flags",
    "register_shipped_invariants",
    "reset_cost_today",
    "reset_default_qa_store",
    "retrieval_enabled",
    "retrieval_high_confidence_threshold",
    "retrieval_low_confidence_threshold",
    "retrieval_top_k",
    "store_capacity",
    "system_prompt",
    "temperature",
    "timeout_s",
]
