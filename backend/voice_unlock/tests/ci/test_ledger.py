from __future__ import annotations

from backend.voice_unlock.ci.issue_client import Issue
from backend.voice_unlock.ci import ledger


def _phantom(number: int) -> Issue:
    return Issue(
        number=number,
        title=ledger.PHANTOM_TITLE,
        body="auto",
        user_login="github-actions",
        labels=["bug", "critical", "unlock", "automated-test"],
        state="open",
        created_at=f"2026-05-{number:02d}T00:00:00Z",
    )


def _real_issue(number: int) -> Issue:
    return Issue(
        number=number, title="Phase 11 cleanup", body="real",
        user_login="drussell23", labels=["bug", "phase-11"],
        state="open", created_at="2026-05-11T00:00:00Z",
    )


def test_select_phantom_issues_matches_only_auto_filed():
    issues = [_phantom(1), _phantom(2), _real_issue(33911)]
    selected = ledger.select_phantom_issues(issues)
    assert {i.number for i in selected} == {1, 2}


def test_select_ignores_wrong_user_or_missing_labels():
    not_bot = Issue(number=5, title=ledger.PHANTOM_TITLE, body="x",
                    user_login="someone", labels=["unlock", "automated-test"],
                    state="open", created_at="2026-05-05T00:00:00Z")
    missing_label = Issue(number=6, title=ledger.PHANTOM_TITLE, body="x",
                          user_login="github-actions", labels=["unlock"],
                          state="open", created_at="2026-05-06T00:00:00Z")
    assert ledger.select_phantom_issues([not_bot, missing_label]) == []


def test_plan_purge_keeps_newest_phantom_as_ledger():
    issues = [_phantom(1), _phantom(9), _phantom(4), _real_issue(33911)]
    plan = ledger.plan_purge(issues)
    assert plan.ledger.number == 9
    assert {i.number for i in plan.to_close} == {1, 4}
    assert plan.ledger_needs_conversion is True


def test_plan_purge_prefers_existing_ledger_and_is_idempotent():
    existing_ledger = Issue(number=100, title=ledger.LEDGER_TITLE, body=ledger.LEDGER_MARKER,
                            user_login="github-actions", labels=[ledger.LEDGER_LABEL],
                            state="open", created_at="2026-06-14T00:00:00Z")
    issues = [_phantom(1), _phantom(2), existing_ledger]
    plan = ledger.plan_purge(issues)
    assert plan.ledger.number == 100
    assert {i.number for i in plan.to_close} == {1, 2}
    assert plan.ledger_needs_conversion is False


def test_plan_purge_empty_when_no_phantoms():
    plan = ledger.plan_purge([_real_issue(1)])
    assert plan.ledger is None
    assert plan.to_close == []


def test_build_ledger_body_embeds_marker_and_status():
    body = ledger.build_ledger_body(
        run_url="https://github.com/o/r/actions/runs/123",
        timestamp_iso="2026-06-14T04:00:00Z",
        status="failure",
        detail="Track A: biometric reject-case regression",
    )
    assert ledger.LEDGER_MARKER in body
    assert "2026-06-14T04:00:00Z" in body
    assert "runs/123" in body
    assert "failure" in body
    # The body must be self-describing so humans understand it is auto-maintained.
    assert "single rolling" in body.lower()


def test_build_ledger_body_handles_passing_status():
    body = ledger.build_ledger_body(
        run_url="https://x/runs/9", timestamp_iso="2026-06-14T04:00:00Z",
        status="passing", detail="all green",
    )
    assert ledger.LEDGER_MARKER in body
    assert "passing" in body


def test_state_tag_mapping():
    assert ledger.state_tag("passing") == "🟢 PASSING"
    assert ledger.state_tag("failure") == "🔴 FAILING"
    assert ledger.state_tag("degraded") == "⚠️ DEGRADED"
    assert ledger.state_tag("initialized") == "⚪ INITIALIZED"
    assert ledger.state_tag("weird") == "❔ WEIRD"


def test_build_ledger_body_has_state_tag_and_track_matrix():
    body = ledger.build_ledger_body(
        run_url="https://x/runs/1", timestamp_iso="2026-06-14T04:00:00Z",
        status="failure", detail="d",
    )
    # emoji-driven state tag (overall + Track A cell)
    assert "🔴 FAILING" in body
    # verification matrix with both tracks
    assert "Verification Matrix" in body
    assert "A — Cloud Logic" in body
    assert "B — Sovereign Hardware" in body
    # Track B is dormant until a self-hosted runner is provisioned
    assert "DORMANT" in body


def test_build_ledger_body_passing_shows_green_tag():
    body = ledger.build_ledger_body(
        run_url="https://x/runs/2", timestamp_iso="t", status="passing", detail="ok",
    )
    assert "🟢 PASSING" in body


def test_select_phantom_accepts_bot_suffixed_login():
    # GitHub REST reports the github-actions bot author as 'github-actions[bot]'
    # (GraphQL/MCP strips the suffix). The filter must accept both forms.
    issue = Issue(number=7, title=ledger.PHANTOM_TITLE, body="x",
                  user_login="github-actions[bot]",
                  labels=["bug", "critical", "unlock", "automated-test"],
                  state="open", created_at="2026-05-07T00:00:00Z")
    assert ledger.select_phantom_issues([issue]) == [issue]
