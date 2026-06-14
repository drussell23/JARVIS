from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass

from backend.voice_unlock.ci.issue_client import IssueClient
from backend.voice_unlock.ci import ledger
from backend.voice_unlock.ci.github_rest_client import GitHubRestClient

_CLOSE_COMMENT = (
    "Superseded by the Canonical Hardware Integration Ledger (Slice 250). "
    "This was an auto-filed duplicate of the daily unlock-CI failure; closing as "
    "part of the one-time phantom-issue purge. No action needed."
)


@dataclass(frozen=True)
class PurgeResult:
    ledger_number: int | None
    closed_numbers: list[int]


def run_purge(client: IssueClient, *, execute: bool, timestamp_iso: str) -> PurgeResult:
    """Plan (and optionally perform) the purge. Dry-run by default."""
    issues = client.list_issues(state="open")
    plan = ledger.plan_purge(issues)

    if plan.ledger is None:
        return PurgeResult(ledger_number=None, closed_numbers=[])

    closed = [i.number for i in plan.to_close]

    if execute:
        if plan.ledger_needs_conversion:
            body = ledger.build_ledger_body(
                run_url="(purge — no run)",
                timestamp_iso=timestamp_iso,
                status="initialized",
                detail="Ledger established by Slice 250.1 phantom-issue purge.",
            )
            client.update_issue(
                plan.ledger.number,
                title=ledger.LEDGER_TITLE,
                body=body,
                labels=[ledger.LEDGER_LABEL],
            )
        for issue in plan.to_close:
            client.close_issue(issue.number, comment=_CLOSE_COMMENT)

    return PurgeResult(ledger_number=plan.ledger.number, closed_numbers=closed)


def _build_client_from_env() -> GitHubRestClient:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        raise SystemExit("GITHUB_TOKEN and GITHUB_REPOSITORY must be set.")
    return GitHubRestClient(token=token, repo=repo)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Purge phantom unlock-CI issues.")
    parser.add_argument("--execute", action="store_true",
                        help="Actually close issues. Default is dry-run.")
    parser.add_argument("--timestamp", default="", help="Override ISO timestamp (testing).")
    args = parser.parse_args(argv)

    from datetime import datetime, timezone
    ts = args.timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    client = _build_client_from_env()
    result = run_purge(client, execute=args.execute, timestamp_iso=ts)

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[{mode}] Ledger issue: #{result.ledger_number}")
    print(f"[{mode}] Phantoms to close ({len(result.closed_numbers)}): {result.closed_numbers}")
    if not args.execute:
        print("Re-run with --execute to apply.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
