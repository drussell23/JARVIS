"""Event Breadcrumb Registry — connect the whole backend event surface to the CLI.

One descriptor-driven registry renders ANY of the ~149 broker event types; an
unregistered/new event still surfaces via a name-based severity heuristic + a
generic formatter. Verbosity filter + coalescer keep it calm.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.breadcrumbs_repl import (
    dispatch_breadcrumbs_command,
)
from backend.core.ouroboros.governance.event_breadcrumb_registry import (
    BreadcrumbCoalescer,
    SEV_CRITICAL,
    SEV_IMPORTANT,
    SEV_INFO,
    SEV_VERBOSE,
    build_default_registry,
    get_min_severity,
    set_min_severity,
    severity_name,
)


def _strip(s: str) -> str:
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


def test_seeded_descriptor_tailored_render() -> None:
    reg = build_default_registry()
    sev, text = reg.render("circuit_breaker_tripped",
                           {"provider": "doubleword", "to_state": "OPEN_TERMINAL",
                            "terminal_reason_code": "quota"})
    assert sev == SEV_CRITICAL
    assert "circuit breaker tripped" in text
    assert "doubleword" in text and "OPEN_TERMINAL" in text and "quota" in text


def test_unknown_event_still_surfaces_via_heuristic() -> None:
    reg = build_default_registry()
    # A brand-new event nobody registered — MUST still render, at a sane severity.
    sev, text = reg.render("quantum_entangler_tripped", {"detail": "spooky", "op_id": "x"})
    assert sev == SEV_CRITICAL                      # "tripped" → critical heuristic
    assert "quantum entangler tripped" in text
    assert "detail=spooky" in text                  # generic formatter
    assert "op_id" not in text                      # noisy keys skipped

    sev2, _ = reg.render("some_new_thing_changed", {"a": 1})
    assert sev2 == SEV_INFO                           # generic "changed" → info
    sev_imp, _ = reg.render("widget_state_change", {})
    assert sev_imp == SEV_IMPORTANT                   # specific "state_change" → important
    sev3, _ = reg.render("progress_tick", {})
    assert sev3 == SEV_VERBOSE                        # "tick" → verbose


def test_bespoke_events_are_skipped_by_router() -> None:
    reg = build_default_registry()
    # The two events with tailored bespoke listeners must be flagged so the
    # unified router does not double-print them.
    assert reg.is_bespoke("shadow_action_trapped") is True
    assert reg.is_bespoke("provider_state_changed") is True
    assert reg.is_bespoke("circuit_breaker_tripped") is False


def test_severity_filter_levels() -> None:
    set_min_severity("critical")
    assert get_min_severity() == SEV_CRITICAL
    set_min_severity("all")
    assert get_min_severity() == SEV_VERBOSE
    set_min_severity("important")
    assert get_min_severity() == SEV_IMPORTANT
    assert severity_name(SEV_IMPORTANT) == "important"
    set_min_severity("off")
    assert get_min_severity() >= 99                  # nothing shown
    set_min_severity("important")                     # restore default for other tests


def test_coalescer_deflood() -> None:
    c = BreadcrumbCoalescer(window_s=6.0)
    assert c.should_show("circuit_breaker_approaching", "dw", now=100.0) is True
    assert c.should_show("circuit_breaker_approaching", "dw", now=101.0) is False  # within window
    assert c.should_show("circuit_breaker_approaching", "dw", now=107.0) is True   # window passed
    # Different key is independent.
    assert c.should_show("circuit_breaker_approaching", "claude", now=101.0) is True


def test_render_never_raises_on_bad_payload() -> None:
    reg = build_default_registry()
    for bad in (None, "not a dict", 42, {"nested": {"x": 1}}, {"big": "z" * 500}):
        sev, text = reg.render("provider_failure_classified", bad if isinstance(bad, dict) else {})
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# /breadcrumbs verb
# ---------------------------------------------------------------------------


def test_breadcrumbs_verb() -> None:
    assert dispatch_breadcrumbs_command("/status").matched is False
    assert dispatch_breadcrumbs_command("/breadcrumbs").matched is True
    assert "Live event feed" in _strip(dispatch_breadcrumbs_command("/breadcrumbs").text)

    r = dispatch_breadcrumbs_command("/breadcrumbs critical")
    assert r.ok is True and get_min_severity() == SEV_CRITICAL
    assert "critical" in _strip(r.text)

    r2 = dispatch_breadcrumbs_command("/breadcrumbs bogus")
    assert r2.ok is False and "unknown level" in r2.text

    assert "verbosity" in _strip(dispatch_breadcrumbs_command("/breadcrumbs help").text)
    set_min_severity("important")   # restore
