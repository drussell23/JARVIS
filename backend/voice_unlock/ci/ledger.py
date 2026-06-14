from __future__ import annotations

from dataclasses import dataclass

from backend.voice_unlock.ci.issue_client import Issue

# The exact title the cron-blind creator used (matching existing artifacts — data, not config).
PHANTOM_TITLE = "🚨 Critical: Unlock Test Suite Failed"
PHANTOM_REQUIRED_LABELS = frozenset({"unlock", "automated-test"})
# The GitHub Actions bot authored these issues. The REST API reports its login
# as "github-actions[bot]"; the GraphQL API (and the github MCP tool) report
# "github-actions". Accept both so selection works regardless of fetch path.
PHANTOM_BOT_USERS = frozenset({"github-actions", "github-actions[bot]"})

# The single rolling ledger issue.
LEDGER_TITLE = "📓 Canonical Hardware Integration Ledger (unlock CI)"
LEDGER_LABEL = "unlock-ci-ledger"
LEDGER_MARKER = "<!-- unlock-ci-ledger:v1 -->"


def select_phantom_issues(issues: list[Issue]) -> list[Issue]:
    """Auto-filed duplicate unlock-failure issues: exact title, bot author,
    and both required labels present."""
    out: list[Issue] = []
    for i in issues:
        if i.title != PHANTOM_TITLE:
            continue
        if i.user_login not in PHANTOM_BOT_USERS:
            continue
        if not PHANTOM_REQUIRED_LABELS.issubset(set(i.labels)):
            continue
        out.append(i)
    return out


def find_ledger(issues: list[Issue]) -> Issue | None:
    """Return the canonical ledger issue if present, else None. Matches on the
    ledger label OR the load-bearing body marker (robust to manual label edits).
    Single source of truth for ledger lookup, shared by plan_purge and the
    reporter."""
    for i in issues:
        if LEDGER_LABEL in i.labels or LEDGER_MARKER in i.body:
            return i
    return None


@dataclass(frozen=True)
class PurgePlan:
    ledger: Issue | None
    to_close: list[Issue]
    ledger_needs_conversion: bool  # True when a phantom must be retitled/relabeled into the ledger


def plan_purge(issues: list[Issue]) -> PurgePlan:
    """Idempotent purge plan. If a ledger already exists, keep it and close ALL
    phantoms. Otherwise promote the newest phantom (highest issue number) to the
    ledger and close the rest. Non-phantom issues are never touched."""
    existing = find_ledger(issues)
    phantoms = select_phantom_issues(issues)
    if existing is not None:
        return PurgePlan(ledger=existing, to_close=list(phantoms), ledger_needs_conversion=False)
    if not phantoms:
        return PurgePlan(ledger=None, to_close=[], ledger_needs_conversion=False)
    newest = max(phantoms, key=lambda i: i.number)
    to_close = [i for i in phantoms if i.number != newest.number]
    return PurgePlan(ledger=newest, to_close=to_close, ledger_needs_conversion=True)


# Deterministic emoji-driven state tags. Unknown statuses degrade to a
# clearly-marked UNKNOWN tag rather than silently dropping signal.
_STATE_TAGS = {
    "passing": "🟢 PASSING",
    "failure": "🔴 FAILING",
    "degraded": "⚠️ DEGRADED",
    "initialized": "⚪ INITIALIZED",
}


def state_tag(status: str) -> str:
    """Map a status token to a deterministic emoji-tagged label."""
    return _STATE_TAGS.get(status, f"❔ {status.upper()}")


def build_ledger_body(
    *,
    run_url: str,
    timestamp_iso: str,
    status: str,
    detail: str,
) -> str:
    """Render the canonical ledger body as a deterministic, structured Markdown
    schema. The marker is load-bearing: the reporter finds this issue by it.

    The body carries an emoji-driven overall state tag plus a Track A / Track B
    verification matrix. Track A (Cloud Logic, ``mock`` provider) reflects the
    reported status; Track B (Sovereign Hardware, ``real`` provider) stays
    DORMANT until a self-hosted macOS runner is provisioned (Slice 250 design).
    """
    tag = state_tag(status)
    return (
        f"{LEDGER_MARKER}\n"
        "# 🔓 Canonical Hardware Integration Ledger\n\n"
        f"**State:** {tag}\n\n"
        "This is the **single rolling** issue for unlock-CI status. It is "
        "auto-maintained by `backend/voice_unlock/ci/ledger_reporter.py`; do not "
        "open new per-run issues. (Replaces the old cron-blind issue creator that "
        "produced ~225 duplicate issues — see Slice 250.)\n\n"
        "## Verification Matrix\n\n"
        "| Track | Scope | Provider | Status |\n"
        "| --- | --- | --- | --- |\n"
        f"| **A — Cloud Logic** | PR / push / scheduled | `mock` | {tag} |\n"
        "| **B — Sovereign Hardware** | self-hosted macOS | `real` "
        "| ⚪ DORMANT (no self-hosted runner) |\n\n"
        "## Latest update\n\n"
        f"- **Status:** {status}\n"
        f"- **Updated:** {timestamp_iso}\n"
        f"- **Run:** {run_url}\n"
        f"- **Detail:** {detail}\n"
    )
