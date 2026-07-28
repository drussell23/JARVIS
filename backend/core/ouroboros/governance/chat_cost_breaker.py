"""The Token Circuit Breaker — a chat brain that cannot run away with the bill.

``ClaudeChatActionExecutor`` already carries a per-call cap and a
per-INSTANCE session budget. Neither is sufficient to arm a real brain on
the operator's prompt, for two structural reasons:

* **Per-instance accounting is not session accounting.** The dispatcher is
  built per bridge/attach; a reconnect, a `/chat clear`, or a second cockpit
  mints a fresh executor whose counter starts at zero. Ten reconnects is ten
  budgets. The ledger here is process-global and file-backed, so spend
  survives every object that spends it.
* **Refusal is not degradation.** On exhaustion the executor returns an
  `error-session-budget-exhausted-<turn>` token: the operator types a
  question and gets a machine sentinel with no explanation and no answer.
  The breaker instead ROUTES the same payload to the logging executor —
  the turn still lands, still gets a receipt, still enters history — and
  says plainly, once, that the brain is off.

The breaker is a middleware implementing the same ``ChatActionExecutor``
Protocol it wraps, so it composes into the EXISTING factory chain
(Claude → Subagent → Backlog → Logging) without any leg knowing it exists.
Three of the four Protocol methods delegate untouched; only the one that
spends is gated.

**One spend surface.** Chat cost is recorded into the organism's own
``CostTracker`` when one is mounted, so the status line's ``$0.14/$2.50``
counts every dollar the organism spends — chat included — instead of two
ledgers disagreeing about the same money. The chat-scoped cap is a SECOND,
tighter ring inside that one: chat can exhaust its own allowance long
before the organism exhausts its session budget.

**Projection, not optimism.** The gate runs BEFORE the network call and
projects the worst case using the wrapped executor's OWN per-call cap
rather than a number invented here — so tightening the executor tightens
the projection, and there is no second constant to drift.

Env:
  * ``JARVIS_SESSION_BUDGET_USD``   chat's cumulative allowance (1.00)
  * ``JARVIS_CHAT_BREAKER_ENABLED`` master (default true — and the factory
    fails CLOSED without it: no breaker, no brain)
  * ``JARVIS_CHAT_SPEND_LEDGER``    where cumulative spend persists

NEVER raises into a dispatch path: a breaker fault degrades to the
fallback executor, which is the safe direction.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional

logger = logging.getLogger(__name__)

CHAT_COST_BREAKER_SCHEMA_VERSION: str = "chat_cost_breaker.1"

SESSION_BUDGET_ENV_VAR: str = "JARVIS_SESSION_BUDGET_USD"
MASTER_FLAG_ENV_VAR: str = "JARVIS_CHAT_BREAKER_ENABLED"
LEDGER_PATH_ENV_VAR: str = "JARVIS_CHAT_SPEND_LEDGER"

DEFAULT_SESSION_BUDGET_USD: float = 1.00
_DEFAULT_LEDGER = ".jarvis/chat_spend.json"

#: What the operator sees, once, when the brain goes dark.
TRIP_NOTICE: str = (
    "🛑 chat budget exhausted ({spent} of {cap}) — degrading to local "
    "logging. Raise JARVIS_SESSION_BUDGET_USD or `/chat budget reset` to "
    "re-arm."
)


def is_breaker_enabled() -> bool:
    """Master flag — default TRUE. Off means the factory refuses to arm the
    brain at all (fail-closed): an unguarded paid API is not a degraded
    mode, it is the failure this module exists to prevent."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def session_budget_usd() -> float:
    """Chat's cumulative allowance. NEVER raises; a malformed value falls
    back to the default rather than to無 limit."""
    try:
        value = float(os.environ.get(
            SESSION_BUDGET_ENV_VAR, str(DEFAULT_SESSION_BUDGET_USD),
        ))
        return max(0.0, value)
    except (TypeError, ValueError):
        return DEFAULT_SESSION_BUDGET_USD


def _ledger_path() -> Optional[Path]:
    try:
        explicit = os.environ.get(LEDGER_PATH_ENV_VAR, "").strip()
        if explicit:
            return Path(explicit).expanduser()
        root = os.environ.get("JARVIS_REPO_PATH", "").strip() or "."
        return Path(root).expanduser() / _DEFAULT_LEDGER
    except Exception:  # noqa: BLE001
        return None


class SessionCostLedger:
    """Process-global, file-backed chat spend.

    Global because the executor's counter dies with the executor and the
    operator's money does not. File-backed because a daemon restart in the
    middle of a runaway loop must not hand the next process a fresh
    allowance — the failure mode this exists to stop is precisely the one
    that restarts things."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spent: float = 0.0
        self._trips: int = 0
        self._loaded = False

    # -- persistence -------------------------------------------------------

    def _load_once(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            path = _ledger_path()
            if path is None or not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self._spent = max(0.0, float(data.get("spent_usd") or 0.0))
                self._trips = int(data.get("trips") or 0)
        except Exception:  # noqa: BLE001 — an unreadable ledger starts clean
            logger.debug("[ChatBreaker] ledger load degraded", exc_info=True)

    def _persist(self) -> None:
        try:
            path = _ledger_path()
            if path is None:
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps({
                "schema_version": CHAT_COST_BREAKER_SCHEMA_VERSION,
                "spent_usd": round(self._spent, 6),
                "trips": self._trips,
                "updated_at": time.time(),
            }), encoding="utf-8")
            os.replace(tmp, path)
        except Exception:  # noqa: BLE001
            logger.debug("[ChatBreaker] ledger persist degraded",
                         exc_info=True)

    # -- accounting --------------------------------------------------------

    @property
    def spent(self) -> float:
        with self._lock:
            self._load_once()
            return self._spent

    @property
    def trips(self) -> int:
        with self._lock:
            self._load_once()
            return self._trips

    def record(self, cost_usd: float, *, provider: str = "chat") -> float:
        """Add real spend. Also reports into the ORGANISM's cost tracker so
        the status line counts every dollar once. NEVER raises."""
        try:
            delta = float(cost_usd or 0.0)
            if delta <= 0:
                return self.spent
            with self._lock:
                self._load_once()
                self._spent += delta
                self._persist()
                total = self._spent
            _record_organism_cost(provider, delta)
            return total
        except Exception:  # noqa: BLE001
            return self.spent

    def note_trip(self) -> int:
        try:
            with self._lock:
                self._load_once()
                self._trips += 1
                self._persist()
                return self._trips
        except Exception:  # noqa: BLE001
            return 0

    def reset(self) -> None:
        """Operator re-arm. NEVER raises."""
        try:
            with self._lock:
                self._loaded = True
                self._spent = 0.0
                self._trips = 0
                self._persist()
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        cap = session_budget_usd()
        spent = self.spent
        return {
            "schema_version": CHAT_COST_BREAKER_SCHEMA_VERSION,
            "spent_usd": round(spent, 6),
            "cap_usd": cap,
            "remaining_usd": max(0.0, cap - spent),
            "tripped": cap > 0 and spent >= cap,
            "trips": self.trips,
        }


_LEDGER = SessionCostLedger()


def get_ledger() -> SessionCostLedger:
    return _LEDGER


def _record_organism_cost(provider: str, cost_usd: float) -> None:
    """Fold chat spend into the organism's CostTracker when one is mounted.

    ONE spend surface: the status line's ``$x/$y`` should mean "what the
    organism has spent", and a chat turn spends real money. Best-effort by
    design — no tracker mounted (a bare cockpit, a unit test) simply means
    the chat ledger is the only book, which is still correct."""
    try:
        from backend.core.ouroboros.battle_test.harness import (
            get_active_cost_tracker,
        )
        tracker = get_active_cost_tracker()
        if tracker is not None:
            tracker.record(provider, cost_usd)
    except Exception:  # noqa: BLE001
        pass


def chat_budget_chip() -> str:
    """The status-line chip, or "" while nothing has been spent.

    Silent at rest (Style Guide §01: restraint by default) — a $0.00
    counter on every repaint teaches the operator to ignore the number
    they will one day need to read."""
    try:
        snap = _LEDGER.snapshot()
        spent = float(snap["spent_usd"])
        if spent <= 0:
            return ""
        cap = float(snap["cap_usd"])
        mark = "🛑" if snap["tripped"] else "💬"
        return f"{mark} ${spent:.2f}/${cap:.2f}"
    except Exception:  # noqa: BLE001
        return ""


class CostCapMiddleware:
    """Circuit breaker around the spending leg of the executor chain.

    ``primary`` is the real brain; ``fallback`` is where a tripped turn
    lands (the logging executor — the same safe default the chain uses
    when the brain is disarmed). Only ``query_claude`` is gated; the other
    three Protocol methods pass through untouched, because they do not
    spend."""

    def __init__(
        self,
        primary: Any,
        fallback: Any,
        *,
        ledger: Optional[SessionCostLedger] = None,
        notify: Optional[Callable[[str], None]] = None,
        budget_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._ledger = ledger if ledger is not None else _LEDGER
        self._notify = notify or (lambda _m: None)
        self._budget_fn = budget_fn or session_budget_usd
        #: The notice is said ONCE per trip-streak, not once per turn: a
        #: banner on every message after exhaustion is noise the operator
        #: learns to scroll past.
        self._announced = False

    # -- transparency ------------------------------------------------------

    @property
    def primary(self) -> Any:
        """The caged brain. Named so callers can INSPECT what they wrapped
        without reaching through a private name."""
        return self._primary

    @property
    def fallback(self) -> Any:
        return self._fallback

    def __getattr__(self, name: str) -> Any:
        """Delegate anything we do not define to the wrapped executor.

        A breaker that hides the executor's surface would force every
        caller — audit tooling, the cost-cap tests, a future `/chat
        budget` verb — to learn that a wrapper exists and unwrap it. The
        middleware gates ONE method; for everything else it should be
        invisible. Only called for attributes normal lookup missed, so it
        can never shadow the gate."""
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._primary, name)

    # -- pass-through legs -------------------------------------------------

    def dispatch_backlog(self, message: str, turn: Any) -> str:
        return self._primary.dispatch_backlog(message, turn)

    def spawn_subagent(self, message: str, turn: Any) -> str:
        return self._primary.spawn_subagent(message, turn)

    def attach_context(self, message: str, turn: Any, target_turn: Any) -> str:
        return self._primary.attach_context(message, turn, target_turn)

    # -- the gated leg -----------------------------------------------------

    def _projected_call_usd(self) -> float:
        """Worst-case cost of the NEXT call, read from the wrapped
        executor's own per-call cap. Deliberately not a constant here:
        tightening the executor must tighten the projection, and two
        numbers for one fact drift."""
        try:
            value = getattr(self._primary, "_cost_cap_per_call_usd", None)
            if value is None:
                from backend.core.ouroboros.governance.chat_repl_claude_executor import (  # noqa: E501
                    DEFAULT_COST_CAP_PER_CALL_USD,
                )
                value = DEFAULT_COST_CAP_PER_CALL_USD
            return max(0.0, float(value))
        except Exception:  # noqa: BLE001
            return 0.05

    def would_trip(self) -> bool:
        """Would the next call breach the cap? Public so the cockpit can
        warn BEFORE the operator types, not only after."""
        try:
            cap = float(self._budget_fn())
            if cap <= 0:
                return True
            return (self._ledger.spent + self._projected_call_usd()) > cap
        except Exception:  # noqa: BLE001
            return False

    def query_claude(
        self, message: str, turn: Any, recent_turns: List[Any],
    ) -> str:
        try:
            if self.would_trip():
                return self._trip(message, turn, recent_turns)
        except Exception:  # noqa: BLE001 — a gate fault degrades SAFELY
            logger.debug("[ChatBreaker] gate degraded", exc_info=True)
            return self._fallback.query_claude(message, turn, recent_turns)

        before = self._read_cumulative()
        try:
            result = self._primary.query_claude(message, turn, recent_turns)
        except Exception:  # noqa: BLE001
            # A provider fault must not lose the turn: the fallback still
            # records it, and the operator gets a receipt rather than a
            # traceback.
            logger.debug("[ChatBreaker] primary raised", exc_info=True)
            return self._fallback.query_claude(message, turn, recent_turns)
        # Reconcile REAL spend from the executor's own accounting — the
        # projection was for the gate, never for the books.
        try:
            delta = max(0.0, self._read_cumulative() - before)
            if delta > 0:
                self._ledger.record(delta, provider="chat_claude")
                self._announced = False   # money moved; a new trip may speak
        except Exception:  # noqa: BLE001
            pass
        return result

    def _read_cumulative(self) -> float:
        try:
            return float(getattr(self._primary, "cumulative_cost_usd", 0.0))
        except Exception:  # noqa: BLE001
            return 0.0

    def _trip(self, message: str, turn: Any, recent_turns: List[Any]) -> str:
        """Route to the fallback and say so — once."""
        try:
            self._ledger.note_trip()
            if not self._announced:
                self._announced = True
                snap = self._ledger.snapshot()
                self._notify(TRIP_NOTICE.format(
                    spent=f"${float(snap['spent_usd']):.2f}",
                    cap=f"${float(snap['cap_usd']):.2f}",
                ))
            logger.warning(
                "[ChatBreaker] TRIPPED spent=%.4f cap=%.4f — routing turn "
                "%s to the logging executor",
                self._ledger.spent, self._budget_fn(),
                getattr(turn, "turn_id", "?"),
            )
        except Exception:  # noqa: BLE001
            pass
        return self._fallback.query_claude(message, turn, recent_turns)


def wrap_with_breaker(
    primary: Any,
    fallback: Any,
    *,
    notify: Optional[Callable[[str], None]] = None,
) -> Any:
    """Install the breaker, or return the FALLBACK when it is disabled.

    Fail-closed: with the breaker off, the answer is the logging executor,
    never the unguarded brain. NEVER raises."""
    try:
        if not is_breaker_enabled():
            logger.warning(
                "[ChatBreaker] disabled — refusing to arm the chat brain "
                "without a circuit breaker (fail-closed)",
            )
            return fallback
        return CostCapMiddleware(primary, fallback, notify=notify)
    except Exception:  # noqa: BLE001
        logger.debug("[ChatBreaker] wrap degraded", exc_info=True)
        return fallback


__all__ = [
    "CHAT_COST_BREAKER_SCHEMA_VERSION",
    "CostCapMiddleware",
    "DEFAULT_SESSION_BUDGET_USD",
    "LEDGER_PATH_ENV_VAR",
    "MASTER_FLAG_ENV_VAR",
    "SESSION_BUDGET_ENV_VAR",
    "SessionCostLedger",
    "TRIP_NOTICE",
    "chat_budget_chip",
    "get_ledger",
    "is_breaker_enabled",
    "session_budget_usd",
    "wrap_with_breaker",
]
