"""Slice 1 tests — ScheduleExpression primitive."""
from __future__ import annotations

import calendar
from datetime import datetime, timezone

import pytest

from backend.core.ouroboros.governance.schedule_expression import (
    SCHEDULE_EXPRESSION_SCHEMA_VERSION,
    ScheduleExpression,
    ScheduleExpressionError,
)


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert SCHEDULE_EXPRESSION_SCHEMA_VERSION == "schedule_expression.v1"


# ===========================================================================
# Vixie `@` aliases
# ===========================================================================


@pytest.mark.parametrize("alias,expected", [
    ("@hourly", "0 * * * *"),
    ("@daily", "0 0 * * *"),
    ("@midnight", "0 0 * * *"),
    ("@weekly", "0 0 * * 0"),
    ("@monthly", "0 0 1 * *"),
    ("@yearly", "0 0 1 1 *"),
    ("@annually", "0 0 1 1 *"),
])
def test_alias_expands_to_canonical_cron(alias: str, expected: str):
    expr = ScheduleExpression.from_phrase(alias)
    assert expr.canonical_cron == expected
    assert expr.original_phrase == alias


def test_alias_case_insensitive_and_trimmed():
    expr = ScheduleExpression.from_phrase("  @DAILY  ")
    assert expr.canonical_cron == "0 0 * * *"


# ===========================================================================
# "every <weekday> at <time>" operator sugar
# ===========================================================================


def test_every_monday_defaults_to_9am():
    expr = ScheduleExpression.from_phrase("every monday")
    assert expr.canonical_cron == "0 9 * * 1"


def test_every_monday_at_9am():
    expr = ScheduleExpression.from_phrase("every monday at 9am")
    assert expr.canonical_cron == "0 9 * * 1"


def test_every_friday_at_5pm():
    expr = ScheduleExpression.from_phrase("every friday at 5pm")
    assert expr.canonical_cron == "0 17 * * 5"


def test_every_monday_at_noon_via_12pm():
    expr = ScheduleExpression.from_phrase("every monday at 12pm")
    # 12pm = noon = 12:00 24h
    assert expr.canonical_cron == "0 12 * * 1"


def test_every_monday_at_midnight_via_12am():
    expr = ScheduleExpression.from_phrase("every monday at 12am")
    # 12am = midnight = 00:00 24h
    assert expr.canonical_cron == "0 0 * * 1"


def test_every_monday_at_14_30():
    expr = ScheduleExpression.from_phrase("every monday at 14:30")
    assert expr.canonical_cron == "30 14 * * 1"


def test_every_weekday_expands_to_monfri():
    expr = ScheduleExpression.from_phrase("every weekday at 9am")
    assert expr.canonical_cron == "0 9 * * 1-5"


def test_every_weekend_expands_to_sunsat():
    expr = ScheduleExpression.from_phrase("every weekend at 10am")
    assert expr.canonical_cron == "0 10 * * 0,6"


def test_every_day_at_8am():
    expr = ScheduleExpression.from_phrase("every day at 8am")
    assert expr.canonical_cron == "0 8 * * *"


def test_sugar_case_and_whitespace_tolerant():
    expr = ScheduleExpression.from_phrase("  EVERY Monday AT 9am  ")
    assert expr.canonical_cron == "0 9 * * 1"


def test_sugar_rejects_invalid_hour():
    # "at 25pm" — hour 25 is invalid
    # Falls through to cron attempt → fails parse
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("every monday at 25pm")


# ===========================================================================
# Named weekdays and months in 5-field cron
# ===========================================================================


def test_cron_with_named_weekday():
    expr = ScheduleExpression.from_phrase("0 9 * * MON")
    assert expr.canonical_cron == "0 9 * * 1"


def test_cron_with_named_weekday_full_name():
    expr = ScheduleExpression.from_phrase("0 9 * * monday")
    assert expr.canonical_cron == "0 9 * * 1"


def test_cron_with_named_weekday_range():
    expr = ScheduleExpression.from_phrase("0 9 * * MON-FRI")
    assert expr.canonical_cron == "0 9 * * 1-5"


def test_cron_with_named_weekday_list():
    expr = ScheduleExpression.from_phrase("0 9 * * MON,WED,FRI")
    assert expr.canonical_cron == "0 9 * * 1,3,5"


def test_cron_with_named_month():
    expr = ScheduleExpression.from_phrase("0 0 1 JAN *")
    assert expr.canonical_cron == "0 0 1 1 *"


def test_cron_with_named_month_range():
    expr = ScheduleExpression.from_phrase("0 0 1 JUN-AUG *")
    assert expr.canonical_cron == "0 0 1 6-8 *"


def test_cron_mixed_names_and_numbers():
    expr = ScheduleExpression.from_phrase("0 9 * jan MON")
    assert expr.canonical_cron == "0 9 * 1 1"


# ===========================================================================
# Numeric 5-field cron passthrough
# ===========================================================================


def test_numeric_cron_passthrough():
    expr = ScheduleExpression.from_phrase("*/15 9-17 * * 1-5")
    assert expr.canonical_cron == "*/15 9-17 * * 1-5"


def test_numeric_cron_specific():
    expr = ScheduleExpression.from_phrase("30 14 1 * *")
    assert expr.canonical_cron == "30 14 1 * *"


# ===========================================================================
# Validation errors
# ===========================================================================


def test_empty_phrase_rejected():
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("")


def test_whitespace_only_phrase_rejected():
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("   ")


def test_non_string_rejected():
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase(None)  # type: ignore[arg-type]


def test_unknown_alias_rejected():
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("@fortnightly")


def test_malformed_cron_rejected():
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("not a cron expression at all")


def test_wrong_field_count_rejected():
    # 4 fields instead of 5
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("0 9 * MON")


def test_out_of_range_field_rejected():
    # Hour 99 is not valid
    with pytest.raises(ScheduleExpressionError):
        ScheduleExpression.from_phrase("0 99 * * *")


# ===========================================================================
# Immutability + hashing
# ===========================================================================


def test_expression_is_frozen():
    expr = ScheduleExpression.from_phrase("@hourly")
    with pytest.raises(Exception):
        expr.canonical_cron = "0 0 * * *"  # type: ignore[misc]


def test_expressions_equal_by_value():
    a = ScheduleExpression.from_phrase("@daily")
    b = ScheduleExpression.from_phrase("@midnight")
    # Same canonical cron → equal via the dataclass contract
    # (both fields compared; they differ in original_phrase so they
    # should NOT be equal — we want that so operators' phrasing
    # survives as a visible distinction)
    assert a != b
    assert a == ScheduleExpression.from_phrase("@daily")


def test_expression_hashable_for_dict_keys():
    expr = ScheduleExpression.from_phrase("@hourly")
    d = {expr: "value"}
    assert d[ScheduleExpression.from_phrase("@hourly")] == "value"


# ===========================================================================
# next_fire_time correctness
# ===========================================================================


def _utc_epoch(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return calendar.timegm(
        datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timetuple()
    )


def test_next_fire_every_monday_9am():
    expr = ScheduleExpression.from_phrase("every monday at 9am")
    # 2026-04-22 is a Wednesday; next Monday at 9am UTC is 2026-04-27
    after = _utc_epoch(2026, 4, 22, 10, 0)
    nxt = expr.next_fire_time(after=after)
    expected = _utc_epoch(2026, 4, 27, 9, 0)
    assert nxt == expected


def test_next_fire_hourly():
    expr = ScheduleExpression.from_phrase("@hourly")
    after = _utc_epoch(2026, 4, 22, 10, 30)
    nxt = expr.next_fire_time(after=after)
    expected = _utc_epoch(2026, 4, 22, 11, 0)
    assert nxt == expected


def test_next_fire_daily():
    expr = ScheduleExpression.from_phrase("@daily")
    after = _utc_epoch(2026, 4, 22, 10, 30)
    nxt = expr.next_fire_time(after=after)
    expected = _utc_epoch(2026, 4, 23, 0, 0)
    assert nxt == expected


def test_next_fire_crosses_month_boundary():
    expr = ScheduleExpression.from_phrase("0 0 1 * *")  # 1st of month
    after = _utc_epoch(2026, 4, 15, 12, 0)
    nxt = expr.next_fire_time(after=after)
    expected = _utc_epoch(2026, 5, 1, 0, 0)
    assert nxt == expected


def test_next_fire_crosses_year_boundary():
    expr = ScheduleExpression.from_phrase("@yearly")
    after = _utc_epoch(2026, 6, 1, 0, 0)
    nxt = expr.next_fire_time(after=after)
    expected = _utc_epoch(2027, 1, 1, 0, 0)
    assert nxt == expected


# ===========================================================================
# is_due
# ===========================================================================


def test_is_due_when_next_fire_is_before_now():
    expr = ScheduleExpression.from_phrase("@hourly")
    now = _utc_epoch(2026, 4, 22, 11, 5)
    # last_run was an hour ago; next fire at 11:00 ≤ now=11:05 → due
    last = _utc_epoch(2026, 4, 22, 10, 0)
    assert expr.is_due(last_run=last, now=now) is True


def test_is_not_due_immediately_after_last_run():
    expr = ScheduleExpression.from_phrase("@hourly")
    last = _utc_epoch(2026, 4, 22, 10, 0)
    now = _utc_epoch(2026, 4, 22, 10, 5)
    # Only 5 min since last fire; next fire at 11:00 > now → not due
    assert expr.is_due(last_run=last, now=now) is False


def test_is_due_first_run_never():
    expr = ScheduleExpression.from_phrase("@hourly")
    now = _utc_epoch(2026, 4, 22, 11, 0)
    # last_run=None → treat as 24h ago. Cron fires every hour → due.
    assert expr.is_due(last_run=None, now=now) is True


# ===========================================================================
# describe()
# ===========================================================================


def test_describe_aliases_roundtrip():
    assert ScheduleExpression.from_phrase("@hourly").describe() == "@hourly"
    assert ScheduleExpression.from_phrase("@daily").describe() == "@daily"


def test_describe_every_monday():
    expr = ScheduleExpression.from_phrase("every monday at 9am")
    described = expr.describe()
    # Should read more naturally than the raw cron
    assert "monday" in described.lower()
    assert "09:00" in described


def test_describe_weekday_range():
    expr = ScheduleExpression.from_phrase("every weekday at 9am")
    described = expr.describe()
    # Monday..Friday names present
    for dow in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        assert dow in described.lower()


def test_describe_falls_back_to_original_for_complex_cron():
    # Minute field with step isn't covered by describe(); should fall back
    expr = ScheduleExpression.from_phrase("*/15 9-17 * * 1-5")
    described = expr.describe()
    # Either the original phrase or something including the raw cron is fine;
    # just pin that it doesn't crash and returns a non-empty string.
    assert described
    assert isinstance(described, str)


# ===========================================================================
# Integration: the exact user-quoted phrase must parse cleanly
# ===========================================================================


def test_gap_writeup_quote_parses():
    """The quote from the gap writeup: 'check this file every Monday morning'.

    The expression engine doesn't do NL understanding, but the 'every
    monday' sugar must cover the schedule part of that request.
    """
    expr = ScheduleExpression.from_phrase("every monday")
    # Default 9am = morning
    assert expr.canonical_cron == "0 9 * * 1"
    # And the clean form "every monday at 9am" also works
    expr2 = ScheduleExpression.from_phrase("every monday at 9am")
    assert expr2.canonical_cron == expr.canonical_cron
