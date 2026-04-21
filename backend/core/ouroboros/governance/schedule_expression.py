"""
ScheduleExpression — Slice 1 of the Scheduled Wake-ups arc.
============================================================

A human-friendly wrapper over the existing :class:`CronParser` that
adds:

* **Named aliases** — ``@hourly`` / ``@daily`` / ``@weekly`` / ``@monthly`` /
  ``@yearly`` / ``@midnight`` expand to canonical 5-field cron. The
  same special tokens vixie-cron ships.
* **Named weekdays and months** — ``0 9 * * MON`` reads more
  naturally than ``0 9 * * 1``. The expander lowercases and maps
  ``MON/TUE/WED/THU/FRI/SAT/SUN`` to cron day-of-week integers, and
  ``JAN..DEC`` to month integers.
* **"Every Monday at 9am"-style sugar** — a narrow, documented pattern
  set for the most common operator phrasings, so the quote from the
  gap writeup ("check this file every Monday morning") is actually
  expressible without cron grammar.
* **Frozen value-type wrapper** — :class:`ScheduleExpression` is a
  dataclass carrying the canonical form + the original phrasing.
  Equality and hashing work for use as dict keys.
* **Next-fire / is-due helpers** — thin forwards to :class:`CronParser`
  for now; kept as methods on the wrapper so future expression kinds
  (wakeups, one-shots, event-triggered) can plug in without breaking
  callers.

Manifesto alignment
-------------------

* §5 — deterministic. No LLM. Every alias / expansion is a static
  lookup or regex.
* §7 — fail-closed. An unparseable phrase raises
  :class:`ScheduleExpressionError` at construction time; the runner
  never sees a broken expression at fire time.
* §8 — observable. Every construction logs the ``{original_phrase,
  canonical_cron}`` pair at DEBUG so postmortems can reconstruct what
  an operator typed.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, FrozenSet, Optional, Tuple

import calendar

from backend.core.ouroboros.governance.scheduled_agents import (
    CronParseError,
    CronParser,
    _cron_dow_match,
)

logger = logging.getLogger("Ouroboros.ScheduleExpression")


SCHEDULE_EXPRESSION_SCHEMA_VERSION: str = "schedule_expression.v1"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScheduleExpressionError(ValueError):
    """Raised when an expression cannot be parsed into a canonical cron."""


# ---------------------------------------------------------------------------
# Named aliases — vixie-cron's `@special` tokens + a few readable extras
# ---------------------------------------------------------------------------


_ALIAS_TABLE: Dict[str, str] = {
    "@yearly":    "0 0 1 1 *",
    "@annually":  "0 0 1 1 *",
    "@monthly":   "0 0 1 * *",
    "@weekly":    "0 0 * * 0",
    "@daily":     "0 0 * * *",
    "@midnight":  "0 0 * * *",
    "@hourly":    "0 * * * *",
}


# Day-of-week name → cron integer (Sunday=0 in cron convention)
_DOW_ALIASES: Dict[str, str] = {
    "sun": "0", "mon": "1", "tue": "2", "wed": "3",
    "thu": "4", "fri": "5", "sat": "6",
    "sunday":    "0", "monday":    "1", "tuesday":   "2",
    "wednesday": "3", "thursday":  "4", "friday":    "5", "saturday":  "6",
}

# Month name → cron integer
_MONTH_ALIASES: Dict[str, str] = {
    "jan": "1", "feb": "2", "mar": "3", "apr": "4",
    "may": "5", "jun": "6", "jul": "7", "aug": "8",
    "sep": "9", "oct": "10", "nov": "11", "dec": "12",
    "january":   "1", "february":  "2", "march":     "3", "april":     "4",
    "june":      "6", "july":      "7", "august":    "8", "september": "9",
    "october":   "10", "november":  "11", "december":  "12",
}


# "Every Monday at 9am" sugar — narrow, documented pattern set.
# Case-insensitive; extra whitespace tolerated. Hour token is 12h OR 24h.
_SUGAR_RX = re.compile(
    r"""
    ^\s*
    every\s+
    (?P<dow>mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?
          |fri(?:day)?|sat(?:urday)?|sun(?:day)?|day|weekday|weekend)
    (?:\s+at\s+
      (?P<hour>\d{1,2})
      (?:[:.](?P<minute>\d{1,2}))?
      (?:\s*(?P<ampm>am|pm))?
    )?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


_SUGAR_DOW_MAP: Dict[str, str] = {
    # Specific weekdays — delegated to _DOW_ALIASES
    **{k: v for k, v in _DOW_ALIASES.items() if v.isdigit()},
    # Collective shortcuts
    "day":     "*",         # every day of the week
    "weekday": "1-5",       # Mon–Fri
    "weekend": "0,6",       # Sun + Sat
}


# ---------------------------------------------------------------------------
# Expansion helpers
# ---------------------------------------------------------------------------


def _expand_alias(phrase: str) -> Optional[str]:
    """Return canonical cron for a vixie-style ``@`` alias, or None."""
    key = phrase.strip().lower()
    return _ALIAS_TABLE.get(key)


def _substitute_names(field: str, table: Dict[str, str]) -> str:
    """Replace every name in *field* with its numeric equivalent.

    Handles comma-separated lists and range endpoints: ``MON-FRI``
    becomes ``1-5``; ``MON,WED,FRI`` becomes ``1,3,5``.
    """
    def _repl(match: re.Match[str]) -> str:
        token = match.group(0).lower()
        return table.get(token, match.group(0))

    return re.sub(r"[A-Za-z]+", _repl, field)


def _substitute_named_fields(expr: str) -> str:
    """Substitute named days + months in a 5-field cron expression."""
    parts = expr.strip().split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts
    month = _substitute_names(month, _MONTH_ALIASES)
    dow = _substitute_names(dow, _DOW_ALIASES)
    return " ".join([minute, hour, dom, month, dow])


def _expand_sugar(phrase: str) -> Optional[str]:
    """Return canonical cron for an "every <dow> [at <time>]" phrase."""
    m = _SUGAR_RX.match(phrase)
    if not m:
        return None
    dow_token = m.group("dow").lower()
    # Strip the "day" suffix for matching against the compact table
    dow_compact = (
        dow_token[:3] if dow_token not in ("day", "weekday", "weekend")
        else dow_token
    )
    dow_field = _SUGAR_DOW_MAP.get(dow_compact, _SUGAR_DOW_MAP.get(dow_token))
    if dow_field is None:
        return None

    hour_s = m.group("hour")
    minute_s = m.group("minute")
    ampm = (m.group("ampm") or "").lower()

    if hour_s is None:
        # "every monday" → 9am default (CC's ScheduleWakeup convention —
        # weekday check-in assumes morning)
        hour_i, minute_i = 9, 0
    else:
        hour_i = int(hour_s)
        minute_i = int(minute_s) if minute_s is not None else 0
        if ampm == "pm" and hour_i < 12:
            hour_i += 12
        elif ampm == "am" and hour_i == 12:
            hour_i = 0
        if not (0 <= hour_i <= 23):
            return None
        if not (0 <= minute_i <= 59):
            return None

    return f"{minute_i} {hour_i} * * {dow_field}"


# ---------------------------------------------------------------------------
# ScheduleExpression dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleExpression:
    """Value type: canonicalised cron expression + original phrasing.

    Use :meth:`from_phrase` to build — direct construction is allowed
    but callers should prefer the factory so they get the canonical
    form + validation in one step.
    """

    canonical_cron: str
    original_phrase: str
    schema_version: str = SCHEDULE_EXPRESSION_SCHEMA_VERSION

    # --- factory ---------------------------------------------------------

    @classmethod
    def from_phrase(cls, phrase: str) -> "ScheduleExpression":
        """Parse any supported phrasing into a canonical cron expression.

        Supported kinds (tried in order):
          1. Vixie `@` aliases (@hourly, @daily, @weekly, @monthly,
             @yearly, @midnight, @annually)
          2. "every monday [at 9am]" operator sugar
          3. Standard 5-field cron with optional named months / weekdays
          4. Standard 5-field cron with numeric fields (bare passthrough)

        Raises :class:`ScheduleExpressionError` if none match.
        """
        if not isinstance(phrase, str) or not phrase.strip():
            raise ScheduleExpressionError(
                "expression must be a non-empty string",
            )
        original = phrase.strip()

        # 1) @alias
        alias_expansion = _expand_alias(original)
        if alias_expansion is not None:
            logger.debug(
                "[ScheduleExpression] alias %r → %r",
                original, alias_expansion,
            )
            return cls(
                canonical_cron=alias_expansion,
                original_phrase=original,
            )

        # 2) operator sugar
        sugar_expansion = _expand_sugar(original)
        if sugar_expansion is not None:
            logger.debug(
                "[ScheduleExpression] sugar %r → %r",
                original, sugar_expansion,
            )
            return cls(
                canonical_cron=sugar_expansion,
                original_phrase=original,
            )

        # 3) 5-field cron (maybe with names)
        canonical = _substitute_named_fields(original)
        try:
            fields = CronParser._expand_all(canonical)
        except CronParseError as exc:
            raise ScheduleExpressionError(
                f"could not parse {phrase!r}: {exc}"
            ) from exc
        # CronParser._expand_field silently clamps out-of-range values
        # to the empty set; a fully-empty field means NO possible fire
        # and should be rejected at construction — otherwise the runner
        # would silently skip this schedule forever.
        field_names = ("minute", "hour", "dom", "month", "dow")
        for name, expanded in zip(field_names, fields):
            if not expanded:
                raise ScheduleExpressionError(
                    f"{name} field of {phrase!r} resolves to no valid values "
                    "(all out of range)"
                )
        logger.debug(
            "[ScheduleExpression] cron %r → %r", original, canonical,
        )
        return cls(
            canonical_cron=canonical,
            original_phrase=original,
        )

    # --- forwards to CronParser ------------------------------------------

    def next_fire_time(self, *, after: float) -> float:
        """UTC epoch of next fire strictly after ``after``.

        Deliberately does NOT forward to :meth:`CronParser.next_fire_time`:
        the legacy implementation has a weekday-check bug that silently
        returns "no next fire within 400 days" for specific-dow crons
        like ``0 9 * * 1``. We compute the same answer here using the
        correct :func:`_cron_dow_match` helper, one minute per iteration,
        with a ~400-day budget.
        """
        fields = CronParser._expand_all(self.canonical_cron)
        minutes_f, hours_f, doms_f, months_f, dows_f = fields

        dt = datetime.fromtimestamp(after, tz=timezone.utc).replace(
            second=0, microsecond=0,
        )
        start_ts = calendar.timegm(dt.timetuple()) + 60
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        max_iterations = 576_000  # ~400 days at 1-minute resolution
        for _ in range(max_iterations):
            if (
                dt.minute in minutes_f
                and dt.hour in hours_f
                and dt.day in doms_f
                and dt.month in months_f
                and _cron_dow_match(dt, dows_f)
            ):
                return calendar.timegm(dt.timetuple())
            new_ts = calendar.timegm(dt.timetuple()) + 60
            dt = datetime.fromtimestamp(new_ts, tz=timezone.utc)
        raise ScheduleExpressionError(
            f"no fire within 400 days for {self.canonical_cron!r} "
            f"(after={after})"
        )

    def is_due(self, *, last_run: Optional[float], now: float) -> bool:
        """Return True iff ``now`` is a fire point given ``last_run``."""
        try:
            after = last_run if last_run is not None else (now - 86400)
            nxt = self.next_fire_time(after=after)
        except ScheduleExpressionError:
            return False
        return nxt <= now

    def describe(self) -> str:
        """Return a human-readable description of the schedule.

        Loose-but-useful: reconstructs from the canonical fields where
        possible; falls back to ``original_phrase`` for unusual cases.
        """
        # Recognise exact alias-expansions and render their alias back.
        for alias, expansion in _ALIAS_TABLE.items():
            if self.canonical_cron == expansion:
                return alias
        parts = self.canonical_cron.split()
        if len(parts) == 5:
            minute, hour, dom, month, dow = parts
            if dom == "*" and month == "*" and dow != "*":
                dow_names: Tuple[str, ...] = _cron_dow_names(dow)
                hhmm = _fmt_time(hour, minute)
                if dow_names:
                    return f"every {', '.join(dow_names)} at {hhmm}"
        return self.original_phrase


# ---------------------------------------------------------------------------
# describe() helpers
# ---------------------------------------------------------------------------


_DOW_NUM_TO_NAME: Dict[int, str] = {
    0: "sunday", 1: "monday", 2: "tuesday", 3: "wednesday",
    4: "thursday", 5: "friday", 6: "saturday",
}


def _cron_dow_names(field: str) -> Tuple[str, ...]:
    """Best-effort rendering of a dow field to ordered names."""
    try:
        values = sorted(CronParser._expand_field(field, 0, 6))
    except CronParseError:
        return ()
    return tuple(_DOW_NUM_TO_NAME.get(v, str(v)) for v in values)


def _fmt_time(hour: str, minute: str) -> str:
    """Render hour + minute fields to an HH:MM clock when concrete."""
    try:
        h = int(hour)
        m = int(minute)
        return f"{h:02d}:{m:02d}"
    except ValueError:
        return f"{hour}:{minute}"


__all__ = [
    "SCHEDULE_EXPRESSION_SCHEMA_VERSION",
    "ScheduleExpression",
    "ScheduleExpressionError",
]

_ = (datetime, timezone, FrozenSet)  # silence unused-import guards
