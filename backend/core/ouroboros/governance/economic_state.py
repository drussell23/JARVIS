"""Economic death, said in one vocabulary — and what it costs downstream.

`/liquidity` was a RATE-LIMIT dashboard wearing the name of a funding one. On
2026-08-08, with both paid lanes returning "account balance too low", it
rendered::

    anthropic: 5,000,000 tokens · no reset horizon · status: 200 · runway: ok
    floor: 2,000 tokens · exhausted: no

Five million tokens, HTTP 200, runway ok — while every op died at GENERATE and
a live probe returned `402 Account balance too low`. Not one field was lying
about the thing it measured; the dashboard simply had no field for the thing
that was wrong.

Why the ledger went blind
-------------------------
`record_headers` bails early::

    if tokens is None and reset_delta is None:
        return False          # nothing declared

An economic refusal carries no rate-limit headers — there is no quota to
report, because the problem is not quota. So the response that proves the lane
is dead is exactly the response the ledger discards. The state was recorded
later, by `record_quota_exhaustion` from the generator's error path, and then
never displayed.

This module supplies the missing vocabulary. It stores nothing: it classifies
an upstream refusal, folds the verdict into the row `record_quota_exhaustion`
already owns, and reads back a rendering-ready view. No second tracker, no new
table, no database.

Why the classifier is a TABLE and not an `if status == 402`
-----------------------------------------------------------
Providers disagree about how to say "you are out of money". Observed on ONE
afternoon, from two vendors:

  * DoubleWord  → ``402`` + ``"Account balance too low. Please add credits"``
  * Anthropic   → ``400`` + ``invalid_request_error`` + ``"Your credit balance
    is too low to access the Anthropic API"``

A `== 402` check catches the first and misses the second — and the second is
the one that had already tripped the Claude lane. Status alone is not the
signal; status *plus phrase* is. Both live in tables below, so teaching this a
third vendor's shape is a data edit, not a code path.

Why it is SYNCHRONOUS
---------------------
Classification is pure CPU over values the caller already holds — no I/O, no
network, microseconds. Making it a coroutine would force `record_headers` and
`record_quota_exhaustion` to become awaitable, which would ripple into Aegis's
*synchronous* response hook (`forwarding.py:711`) and put an `await` on the
proxy's hot path in exchange for nothing. The same reasoning that rejected
`to_thread` as a fix for import contention: async is for waiting, and nothing
here waits.

If confirmation ever matters — probing a billing endpoint to verify a balance
rather than inferring it from a refusal — that is genuinely I/O and belongs in
a separate, cancellable enrichment that must never block the dashboard.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.EconomicState")

__all__ = [
    "ECONOMIC",
    "RATE_LIMITED",
    "HEALTHY",
    "UNKNOWN",
    "classify_refusal",
    "fold_economic_state",
    "economic_view",
    "blast_radius",
]

# --- the four things an upstream refusal can mean, for our purposes ---------
#
# Deliberately NOT an enum: these travel into `quota_reason` (a string field
# that already exists) and out to a renderer. A str subclass would be
# ceremony; the values are the contract.
ECONOMIC = "economic"          # out of money — a human must act
RATE_LIMITED = "rate_limited"  # out of quota FOR NOW — a clock will fix it
HEALTHY = "healthy"            # nothing wrong
UNKNOWN = "unknown"            # could not tell — say so, never guess

#: Marker prefixed onto `quota_reason` so a reader can tell an economic outage
#: from a quota one without re-parsing prose. Kept short: the field is capped
#: at 160 chars by `record_quota_exhaustion` and the vendor message is the
#: valuable part.
_CLASS_PREFIX = "class="


# --- the tables -------------------------------------------------------------
#
# Phrases are matched case-insensitively as substrings against the response
# body. They are deliberately vendor-agnostic fragments rather than whole
# messages, because vendors reword ("Please add credits" → "Add credits to
# continue") far more often than they change the noun.

_ECONOMIC_PHRASES: Tuple[str, ...] = (
    "balance too low",
    "credit balance",
    "add credits",
    "insufficient credit",
    "insufficient funds",
    "insufficient balance",
    "payment required",
    "billing",
    "past due",
    "spending limit",
    "no active subscription",
    "purchase additional",
)

#: Rate-limit phrasing that must NOT be read as economic even when it shares a
#: status code with one. "quota" is the trap: providers use it for both a
#: monthly spend cap and a per-minute token ceiling.
_RATE_LIMIT_PHRASES: Tuple[str, ...] = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "requests per",
    "tokens per",
    "try again in",
    "retry after",
    "slow down",
)

#: status -> what that status means ON ITS OWN, before phrases are consulted.
#: `None` means "the status is not decisive; the body decides."
#:
#: 402 is here as DATA, not as an `if`. Adding a vendor that signals funding
#: with 423 is a one-line edit here.
_STATUS_CLASS: Dict[int, Optional[str]] = {
    402: ECONOMIC,        # Payment Required — unambiguous by definition
    403: None,            # auth OR billing — body decides
    429: None,            # rate limit OR spend cap — body decides
    400: None,            # Anthropic signals credit exhaustion here
    401: None,            # usually auth, but some vendors 401 a dead account
}

#: Headers that, when present, mean a real rate-limit window exists. Their
#: presence is strong evidence AGAINST an economic reading: a provider that is
#: refusing you for money has no window to advertise.
_RATE_LIMIT_HEADER_HINTS: Tuple[str, ...] = (
    "retry-after",
    "x-ratelimit-reset",
    "anthropic-ratelimit-tokens-reset",
    "x-ratelimit-remaining",
)


def _text_of(body: Any) -> str:
    """Flatten any body shape to lowercase searchable text. NEVER raises.

    Bodies arrive as dicts, bytes, `str`, exception reprs, or half-truncated
    JSON from a connection that died mid-payload. Mandate: a malformed body
    must degrade to UNKNOWN, never to an exception on a telemetry path.
    """
    try:
        if body is None:
            return ""
        if isinstance(body, (dict, list)):
            return json.dumps(body, default=str).lower()
        if isinstance(body, (bytes, bytearray)):
            return bytes(body).decode("utf-8", errors="replace").lower()
        return str(body).lower()
    except Exception:  # noqa: BLE001
        return ""


def _headers_of(headers: Any) -> Dict[str, str]:
    """Lowercase header map from anything map-like. NEVER raises."""
    out: Dict[str, str] = {}
    try:
        if not headers:
            return out
        items = (
            headers.items() if isinstance(headers, Mapping)
            else getattr(headers, "items", lambda: [])()
        )
        for k, v in items:
            try:
                out[str(k).lower()] = str(v)
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        return {}
    return out


def classify_refusal(
    status: Any = None,
    body: Any = None,
    headers: Any = None,
) -> Tuple[str, str]:
    """Translate one upstream refusal into a unified economic verdict.

    Returns ``(verdict, evidence)`` where verdict is one of ECONOMIC /
    RATE_LIMITED / HEALTHY / UNKNOWN and evidence is a short human-readable
    fragment naming WHY — so a dashboard can show the reasoning rather than a
    bare label.

    Precedence, and the reason for it:

    1. **A rate-limit window in the headers wins over a money phrase.** A
       provider refusing you for funds has no reset horizon to advertise; if
       one is present, a clock will fix this.
    2. **Explicit economic phrasing wins over an ambiguous status.** This is
       what catches Anthropic's ``400 + "credit balance too low"``, which no
       status-based rule would.
    3. **A decisive status wins when the body says nothing.** 402 means
       Payment Required whether or not anyone wrote a message.
    4. **Otherwise UNKNOWN.** Never guess: a wrong ECONOMIC verdict tells the
       operator to go top up an account that is fine, and a wrong HEALTHY
       hides the reason nothing works.

    NEVER raises. Malformed or absent inputs → ``(UNKNOWN, ...)``.
    """
    try:
        text = _text_of(body)
        head = _headers_of(headers)

        try:
            code: Optional[int] = int(status) if status is not None else None
        except (TypeError, ValueError):
            code = None

        # 2xx is not a refusal at all.
        if code is not None and 200 <= code < 300:
            return HEALTHY, f"status {code}"

        has_window = any(h in head for h in _RATE_LIMIT_HEADER_HINTS)
        econ_hit = next((p for p in _ECONOMIC_PHRASES if p in text), None)
        rate_hit = next((p for p in _RATE_LIMIT_PHRASES if p in text), None)

        # (1) an advertised window means a clock fixes this
        if has_window and not econ_hit:
            return RATE_LIMITED, "reset window advertised"
        if rate_hit and not econ_hit:
            return RATE_LIMITED, f"body: {rate_hit!r}"

        # (2) explicit money phrasing beats an ambiguous status
        if econ_hit:
            return ECONOMIC, f"body: {econ_hit!r}"

        # (3) the status alone, only where it is decisive
        if code is not None:
            decided = _STATUS_CLASS.get(code, UNKNOWN)
            if decided:
                return decided, f"status {code}"
            if code in _STATUS_CLASS:
                return UNKNOWN, f"status {code}, body inconclusive"
            return UNKNOWN, f"status {code} unmapped"

        return UNKNOWN, "no status, no phrase"
    except Exception:  # noqa: BLE001 — a classifier must never break telemetry
        logger.debug("[EconomicState] classify degraded", exc_info=True)
        return UNKNOWN, "classifier degraded"


def fold_economic_state(
    provider: str,
    *,
    status: Any = None,
    body: Any = None,
    headers: Any = None,
    now: Optional[float] = None,
) -> str:
    """Classify a refusal and, when economic, record it via the EXISTING hook.

    Composition only: the durable write is `record_quota_exhaustion`, which
    already owns the row, the TTL and the merge-without-clobber semantics. This
    adds the classification the hook was always missing — its `reason` field
    was free-text, so nothing downstream could distinguish "out of money" from
    "out of quota" without re-reading prose.

    Returns the verdict. Non-economic verdicts write NOTHING: a rate limit is
    already the header path's job, and duplicating it here would give one
    condition two writers.

    NEVER raises.
    """
    verdict, evidence = classify_refusal(status, body, headers)
    try:
        if verdict != ECONOMIC:
            return verdict
        from backend.core.ouroboros.governance.provider_liquidity_ledger import (
            record_quota_exhaustion,
        )
        # The vendor's own words are the valuable part — an operator should be
        # able to read the refusal, not a paraphrase of it. The class marker
        # rides in front so `economic_view` can classify without re-parsing.
        detail = _text_of(body).strip()[:110] or evidence
        record_quota_exhaustion(
            provider,
            reason=f"{_CLASS_PREFIX}{ECONOMIC} {evidence} :: {detail}",
            now=now,
        )
        logger.warning(
            "[EconomicState] %s ECONOMIC outage recorded (%s)",
            provider, evidence,
        )
    except Exception:  # noqa: BLE001
        logger.debug("[EconomicState] fold degraded", exc_info=True)
    return verdict


#: A `recorded_unix` older than this is not believable as a real observation
#: from this deployment — it is fixture residue or a clock fault. The live
#: ledger on 2026-08-08 carried `recorded_unix: 1010.0` (→ 1969-12-31), the
#: exact fixture value from `test_header_refresh_never_clears_quota_state`,
#: left in a gitignored file by a historical un-isolated run. It silently
#: poisoned every reset-horizon subtraction that read it.
#:
#: Guarding the READER is the honest fix: production writes `time.time()`, so
#: there is no production time bug to patch. What there is, is a reader that
#: trusts any float.
_PLAUSIBLE_EPOCH_FLOOR = 1_600_000_000.0     # 2020-09-13


def _plausible_recorded(ts: Any) -> Optional[float]:
    """Return *ts* as epoch seconds iff it could be a real observation."""
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v < _PLAUSIBLE_EPOCH_FLOOR:
        return None                       # pre-2020 → not from this system
    if v > time.time() + 86_400.0:
        return None                       # a day in the future → clock fault
    return v


def economic_view(provider: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """Rendering-ready economic state for one provider. NEVER raises.

    Keys:
      ``state``        ECONOMIC / RATE_LIMITED / HEALTHY / UNKNOWN
      ``reason``       the vendor's own refusal text (already truncated)
      ``hard_open``    True while the recorded outage window is still live
      ``expires_in_s`` seconds until the TTL lapses (None when not open)
      ``unverified_since`` epoch at which an economic flag lapsed WITHOUT any
                       evidence the wallet was topped up — the honest reading
                       of an assumption-based TTL
      ``stale_clock``  True when `recorded_unix` failed the plausibility guard

    The `unverified_since` distinction is the point. `record_quota_exhaustion`
    self-heals after `JARVIS_QUOTA_EXHAUSTION_TTL_S` (1800s) so a topped-up
    wallet recovers without manual clearing. That is right for routing — but
    the TTL is an ASSUMPTION, not a measurement. Money does not return on a
    timer. On 2026-08-08 the flag lapsed at 14:42 and the dashboard reverted to
    `ok` while the account stayed empty for hours. Routing keeps its fail-open
    optimism; the display stops calling it knowledge.
    """
    out: Dict[str, Any] = {
        "state": UNKNOWN, "reason": "", "hard_open": False,
        "expires_in_s": None, "unverified_since": None, "stale_clock": False,
    }
    try:
        from backend.core.ouroboros.governance.provider_liquidity_ledger import (
            _load,
        )
        row = (_load().get("providers") or {}).get(
            str(provider or "unknown").lower()) or {}
        t = float(now if now is not None else time.time())

        if row.get("recorded_unix") is not None:
            out["stale_clock"] = _plausible_recorded(row.get("recorded_unix")) is None

        reason = str(row.get("quota_reason") or "")
        until = row.get("quota_exhausted_until")
        is_economic = f"{_CLASS_PREFIX}{ECONOMIC}" in reason
        # Pre-existing rows predate the class marker. Fall back to the phrase
        # table so history recorded before this module still reads correctly.
        if not is_economic and reason:
            is_economic = classify_refusal(None, reason, None)[0] == ECONOMIC

        # Strip the machine marker; an operator wants the vendor's sentence.
        out["reason"] = reason.split("::", 1)[-1].strip() if "::" in reason else reason

        if until is None:
            out["state"] = HEALTHY if not reason else UNKNOWN
            return out
        try:
            until_f = float(until)
        except (TypeError, ValueError):
            return out                     # malformed row → UNKNOWN, no crash

        if t < until_f:
            out["state"] = ECONOMIC if is_economic else RATE_LIMITED
            out["hard_open"] = True
            out["expires_in_s"] = max(0.0, until_f - t)
        else:
            # Lapsed. NOT proof of recovery — only proof that time passed.
            out["state"] = UNKNOWN if is_economic else HEALTHY
            if is_economic:
                out["unverified_since"] = until_f
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[EconomicState] view degraded", exc_info=True)
        return out


def blast_radius(*, now: Optional[float] = None) -> Dict[str, Any]:
    """Who absorbs the traffic when a lane is down — and whether they CAN.

    A naive blast radius names the fallback and stops, which is worse than
    silence: it reports a successful handoff to a lane that may itself be
    dead. On 2026-08-08 that was exactly the situation — Claude economically
    tripped, traffic nominally rerouted to DW, and DW was *also* out of
    credit. The generator logged `IMMEDIATE reroute → DW` and `DW AUTARKY
    ENGAGED` seconds before failing.

    So every absorbing lane is evaluated for its OWN economic and quota
    viability, and a handoff into a dead lane is reported as a cascading route
    failure rather than a handoff.

    Composition only — `collect_provider_availability` already computes each
    lane's verdict and forensic reason; this reads it, and reads the ledger for
    the funding half. No new health model.

    NEVER raises; on any sensing failure returns an empty, honest shape.
    """
    out: Dict[str, Any] = {"lanes": {}, "cascading": [], "degraded": False}
    try:
        from backend.core.ouroboros.governance.provider_availability import (
            collect_provider_availability,
        )
        from backend.core.ouroboros.governance.provider_liquidity_ledger import (
            runway_exhausted,
        )

        snap = collect_provider_availability()
        # (lane, ledger key, available, forensic reason). The ledger and the
        # availability model name providers differently; this is the only
        # place that has to know both.
        lanes = (
            ("claude", "anthropic",
             bool(getattr(snap, "claude_available", True)),
             str(getattr(snap, "claude_reason", "") or "")),
            ("doubleword", "doubleword",
             bool(getattr(snap, "dw_healthy", True)),
             str(getattr(snap, "dw_reason", "") or "")),
        )

        for lane, key, ok, reason in lanes:
            view = economic_view(key, now=now)
            dry = False
            try:
                dry = bool(runway_exhausted(key))
            except Exception:  # noqa: BLE001
                dry = False
            viable = bool(ok) and view.get("state") != ECONOMIC and not dry
            out["lanes"][lane] = {
                "available": ok, "reason": reason,
                "economic": view.get("state"), "runway_dry": dry,
                "viable": viable, "unverified_since": view.get("unverified_since"),
            }

        # A lane that is down hands off to every OTHER lane. If none of them is
        # viable, the handoff is fiction.
        for lane, info in out["lanes"].items():
            if info["viable"]:
                continue
            others = [n for n, i in out["lanes"].items() if n != lane]
            absorbing = [n for n in others if out["lanes"][n]["viable"]]
            if absorbing:
                info["absorbed_by"] = absorbing
            else:
                info["absorbed_by"] = []
                out["cascading"].append(lane)

        if out["lanes"]:
            any_viable = any(x["viable"] for x in out["lanes"].values())
            out["degraded"] = bool(out["cascading"]) or not any_viable
        return out
    except Exception:  # noqa: BLE001
        logger.debug("[EconomicState] blast radius degraded", exc_info=True)
        return out
