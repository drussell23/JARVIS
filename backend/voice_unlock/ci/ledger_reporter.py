from __future__ import annotations

from dataclasses import dataclass

from backend.voice_unlock.ci.issue_client import IssueClient
from backend.voice_unlock.ci import ledger


@dataclass(frozen=True)
class LedgerContext:
    run_url: str
    timestamp_iso: str
    status: str  # "failure" | "passing"
    detail: str


def find_or_update_ledger(client: IssueClient, ctx: LedgerContext) -> int:
    """Maintain exactly one ledger issue. Update (and reopen on failure) if it
    exists; create it only if missing. Returns the ledger issue number."""
    body = ledger.build_ledger_body(
        run_url=ctx.run_url,
        timestamp_iso=ctx.timestamp_iso,
        status=ctx.status,
        detail=ctx.detail,
    )
    existing = ledger.find_ledger(client.list_issues(state="all"))
    if existing is None:
        return client.create_issue(ledger.LEDGER_TITLE, body, [ledger.LEDGER_LABEL])

    new_state = "open" if ctx.status == "failure" else None
    client.update_issue(existing.number, body=body, state=new_state)
    return existing.number


def context_from_env(env: dict, *, now_iso: str) -> LedgerContext:
    """Build a LedgerContext from GitHub Actions environment variables."""
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else server
    status = env.get("LEDGER_STATUS", "passing")
    detail = env.get("LEDGER_DETAIL", "(no detail provided)")
    return LedgerContext(run_url=run_url, timestamp_iso=now_iso, status=status, detail=detail)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin glue
    import os
    from datetime import datetime, timezone

    from backend.voice_unlock.ci.github_rest_client import GitHubRestClient

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY must be set.")

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ctx = context_from_env(dict(os.environ), now_iso=now_iso)
    client = GitHubRestClient(token=token, repo=repo)
    number = find_or_update_ledger(client, ctx)
    print(f"Canonical ledger updated: issue #{number} (status={ctx.status})")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
