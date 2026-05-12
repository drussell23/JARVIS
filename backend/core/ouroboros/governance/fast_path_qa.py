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
_ENV_MAX_TOKENS = "JARVIS_FAST_PATH_QA_MAX_TOKENS"
_ENV_TEMPERATURE = "JARVIS_FAST_PATH_QA_TEMPERATURE"
_ENV_STORE_CAPACITY = "JARVIS_FAST_PATH_QA_STORE_CAPACITY"
_ENV_SYSTEM_PROMPT = "JARVIS_FAST_PATH_QA_SYSTEM_PROMPT"
_ENV_TIMEOUT_S = "JARVIS_FAST_PATH_QA_TIMEOUT_S"
_ENV_MODEL = "JARVIS_FAST_PATH_QA_MODEL"

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
        import anthropic  # type: ignore[import-untyped]
    except ImportError:
        return ("", 0.0)
    api_key = os.environ.get(_ENV_ANTHROPIC_KEY, "").strip()
    if not api_key:
        return ("", 0.0)
    try:
        client = anthropic.AsyncAnthropic(api_key=api_key)
    except Exception:  # noqa: BLE001
        return ("", 0.0)
    try:
        resp = await client.messages.create(
            model=model_name(),
            max_tokens=max_tokens(),
            temperature=temperature(),
            system=system,
            messages=[{"role": "user", "content": user_question}],
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

    # Step 3: budget check. Phase 0 D3a — IMMEDIATE budget via
    # in-process daily counter. Phase 2 will route through
    # urgency_router INFORMATIONAL sub-budget (D3b approved).
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
                f"{_ENV_BUDGET_USD})"
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

    # Step 5: invoke provider callable.
    provider = (
        provider_callable
        if provider_callable is not None
        else _default_claude_callable
    )
    try:
        answer, cost = await asyncio.wait_for(
            provider(system_prompt(), q_text),
            timeout=float(timeout_s()),
        )
    except asyncio.TimeoutError:
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic=f"provider timeout after {timeout_s()}s",
            evaluated_at_unix=started,
        )
    except Exception as exc:  # noqa: BLE001
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic=f"provider raised: {exc!r}"[:200],
            evaluated_at_unix=started,
        )

    # Step 6: empty answer guard.
    try:
        answer_text = str(answer or "").strip()
        cost_usd = max(0.0, float(cost))
    except Exception:  # noqa: BLE001
        answer_text = ""
        cost_usd = 0.0
    if not answer_text:
        return QAReport(
            verdict=QAVerdict.PROVIDER_FAILED,
            artifact=None,
            diagnostic="provider returned empty answer",
            evaluated_at_unix=started,
        )

    # Step 7: record cost; record assistant turn via
    # ConversationBridge (D4 default).
    _record_cost(cost_usd, now_unix=started)
    try:
        recorder("assistant", answer_text, "ask_human_a", op_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[FastPathQA] assistant-turn record failed: %r", exc,
        )

    # Step 8: park artifact — gets q-N ref.
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
    )

    # Step 9: return ANSWERED.
    return QAReport(
        verdict=QAVerdict.ANSWERED,
        artifact=artifact,
        diagnostic=(
            f"ok (cost=${cost_usd:.5f}, {elapsed:.2f}s, "
            f"ref={artifact.ref})"
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
        return tuple(violations)

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
            name=_ENV_BUDGET_USD,
            type=FlagType.FLOAT,
            default=_DEFAULT_BUDGET_USD,
            description=(
                "Daily USD budget cap for Q&A. Phase 0 D3a "
                "(IMMEDIATE in-process counter). Phase 2 D3b "
                "will route through urgency_router."
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
    "BoundedQAStore",
    "QAArtifact",
    "QAReport",
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
    "store_capacity",
    "system_prompt",
    "temperature",
    "timeout_s",
]
