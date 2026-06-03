#!/usr/bin/env python3
"""Async token-bucket PR janitor — drains the runaway ``app/github-actions``
"🚨 Fix CI/CD" bot-PR backlog at a GitHub-safe pace, never touching a human PR.

Context (Slice 76 Phase 5, PRD §50.11 follow-on): a misconfigured
``failed-ci-auto-pr.yml`` (``workflows: ["*"]`` + no loop-guard) opened **67,744**
``app/github-actions`` "Fix CI/CD" pull requests against ``drussell23/JARVIS``.
The workflow is disabled (backlog cannot grow); this drains the standing debt.

Safety — a closed PR is irreversible, so the close gate is *defense-in-depth*:
a PR is closed ONLY IF **all** hold (``should_close``):
  1. its number is NOT in the immutable :data:`PROTECTED_PR_IDS` backstop;
  2. the author is positively a bot (``author.type == "Bot"``) ...
  3. ... whose login is a known CI-bot login; and
  4. the title is the exact runaway pattern (``🚨 Fix CI/CD``).
A human PR fails gate 2/3/4 even if (1) were ever stale. The author/title filters
are the *dynamic primary* gate; the hardcoded ID set is the explicit backstop.

Rate discipline (GitHub secondary limits punish concurrent mutations): closes run
SERIALLY through a token bucket (default ~1 close/s) and parse ``Retry-After`` +
``x-ratelimit-remaining``/``-reset`` headers, applying adaptive exponential backoff
on 403/429. A JSONL checkpoint makes the multi-hour drain resumable + auditable.

Usage:
  python3 scripts/utils/async_pr_janitor.py            # drain (live)
  python3 scripts/utils/async_pr_janitor.py --dry-run  # log gate decisions only
  RATE_PER_S=0.5 python3 scripts/utils/async_pr_janitor.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp  # hard dependency of the async network path

REPO = os.environ.get("PR_JANITOR_REPO", "drussell23/JARVIS")
API = "https://api.github.com"

# Immutable backstop — the verified human PRs that must NEVER be closed.
PROTECTED_PR_IDS = frozenset(
    {36818, 35182, 31230, 29868, 21835, 21154, 19664, 253, 181, 158, 134, 113, 106}
)
# Known CI-bot author logins (search API renders "app/github-actions"; the REST
# pulls API renders "github-actions[bot]" — accept both).
BOT_LOGINS = frozenset({"app/github-actions", "github-actions[bot]"})
BOT_TITLE_PREFIX = "🚨 Fix CI/CD"

CHECKPOINT = Path(os.environ.get(
    "PR_JANITOR_CHECKPOINT", ".jarvis/pr_janitor_checkpoint.jsonl"
))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [PRJanitor] %(message)s",
)
logger = logging.getLogger("pr_janitor")


# ---------------------------------------------------------------------------
# Pure, unit-testable core
# ---------------------------------------------------------------------------

def should_close(pr: Dict[str, Any], *, protected=PROTECTED_PR_IDS) -> bool:
    """Defense-in-depth close gate. Returns True ONLY for a runaway bot PR.

    Pure function — every closure decision flows through here so the
    "never close a human PR" invariant is one testable predicate.
    """
    number = pr.get("number")
    if not isinstance(number, int) or number in protected:
        return False
    user = pr.get("user") or {}
    if user.get("type") != "Bot":
        return False
    if user.get("login") not in BOT_LOGINS:
        return False
    title = pr.get("title") or ""
    return title.startswith(BOT_TITLE_PREFIX)


def compute_backoff(attempt: int, retry_after: Optional[float], *, base: float = 2.0,
                    cap: float = 300.0) -> float:
    """Adaptive backoff: honor an explicit Retry-After, else exponential
    (base^attempt) clamped to ``cap``. Never negative."""
    if retry_after is not None and retry_after > 0:
        return min(float(retry_after), cap)
    return min(base ** max(0, attempt), cap)


def _load_done(checkpoint: Path) -> set[int]:
    done: set[int] = set()
    if not checkpoint.exists():
        return done
    for line in checkpoint.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            n = row.get("number")
            if isinstance(n, int):
                done.add(n)
        except (ValueError, TypeError):
            continue
    return done


# ---------------------------------------------------------------------------
# GitHub I/O (async)
# ---------------------------------------------------------------------------

def _auth_token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok.strip()
    env = {**os.environ, "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
    out = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, env=env, check=False,
    )
    return out.stdout.strip()


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _retry_after_from(headers: Dict[str, str]) -> Optional[float]:
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except (ValueError, TypeError):
            return None
    # secondary-limit signal: remaining == 0 → wait until reset
    rem = headers.get("X-RateLimit-Remaining") or headers.get("x-ratelimit-remaining")
    reset = headers.get("X-RateLimit-Reset") or headers.get("x-ratelimit-reset")
    if rem == "0" and reset:
        try:
            return max(0.0, float(reset) - time.time())
        except (ValueError, TypeError):
            return None
    return None


async def _fetch_open_page(session, token: str, per_page: int) -> List[Dict[str, Any]]:
    """Fetch the newest page of open PRs (we close as we go, so re-fetching
    the first page each round walks the shrinking set without cursor drift)."""
    url = f"{API}/repos/{REPO}/pulls?state=open&per_page={per_page}&sort=created&direction=desc"
    # Self-healing: a transient connection reset (Errno 54) / timeout must back
    # the loop off and retry, never crash the unattended ~18h drain. Return the
    # existing error sentinel so the run loop's backoff path handles it.
    try:
        async with session.get(url, headers=_headers(token)) as resp:
            if resp.status != 200:
                ra = _retry_after_from(dict(resp.headers))
                return [{"__error__": resp.status, "__retry_after__": ra}]
            return await resp.json()
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        return [{"__error__": "transient", "__retry_after__": None}]


async def _close_pr(session, token: str, number: int) -> Tuple[bool, Optional[float]]:
    url = f"{API}/repos/{REPO}/pulls/{number}"
    try:
        async with session.patch(url, headers=_headers(token),
                                 json={"state": "closed"}) as resp:
            if resp.status == 200:
                return True, None
            ra = _retry_after_from(dict(resp.headers))
            return False, ra if ra is not None else 0.0
    except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
        # transient — signal the loop to back off (no Retry-After → exp backoff)
        return False, 0.0


async def run(rate_per_s: float, dry_run: bool, max_closes: Optional[int]) -> int:
    import aiohttp  # local import so unit tests of the pure core need no dep

    token = _auth_token()
    if not token:
        logger.error("no GitHub token (GH_TOKEN / gh auth token) — aborting")
        return 0
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(CHECKPOINT)
    min_interval = 1.0 / rate_per_s if rate_per_s > 0 else 1.0
    closed = 0
    attempt = 0
    logger.info("janitor start repo=%s protected=%d already_done=%d dry_run=%s",
                REPO, len(PROTECTED_PR_IDS), len(done), dry_run)
    async with aiohttp.ClientSession() as session:
        while True:
            if max_closes is not None and closed >= max_closes:
                logger.info("reached --max-closes=%d — stopping", max_closes)
                break
            page = await _fetch_open_page(session, token, 100)
            if page and page[0].get("__error__"):
                wait = compute_backoff(attempt, page[0].get("__retry_after__"))
                attempt += 1
                logger.warning("list rate-limited (status=%s) backoff %.1fs",
                               page[0].get("__error__"), wait)
                await asyncio.sleep(wait)
                continue
            attempt = 0
            closable = [p for p in page if should_close(p) and p["number"] not in done]
            if not closable:
                # page is all human/protected/already-done → backlog drained
                remaining_bot = [p for p in page if should_close(p)]
                if not remaining_bot:
                    logger.info("no closable bot PRs on newest page — DRAINED. "
                                "total closed this run=%d", closed)
                    break
                # everything closable is already in `done` (resumed) — skip ahead
                for p in remaining_bot:
                    done.discard(p["number"])  # force re-close (idempotent)
            for p in closable:
                n = p["number"]
                if dry_run:
                    logger.info("[dry-run] would close #%d %s", n, p.get("title", "")[:50])
                    done.add(n)
                    closed += 1
                    continue
                ok, ra = await _close_pr(session, token, n)
                if ok:
                    closed += 1
                    done.add(n)
                    with CHECKPOINT.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"number": n, "ts": time.time()}) + "\n")
                    if closed % 50 == 0:
                        logger.info("closed=%d (latest #%d)", closed, n)
                    await asyncio.sleep(min_interval)
                else:
                    wait = compute_backoff(attempt, ra)
                    attempt += 1
                    logger.warning("close #%d throttled — backoff %.1fs", n, wait)
                    await asyncio.sleep(wait)
                if max_closes is not None and closed >= max_closes:
                    break
    logger.info("janitor done — closed=%d", closed)
    return closed


def main() -> None:
    ap = argparse.ArgumentParser(description="Async bot-PR janitor")
    ap.add_argument("--dry-run", action="store_true", help="log gate decisions, close nothing")
    ap.add_argument("--rate-per-s", type=float,
                    default=float(os.environ.get("RATE_PER_S", "1.0")),
                    help="sustained close throughput (default 1/s — GitHub-safe)")
    ap.add_argument("--max-closes", type=int, default=None,
                    help="stop after N closes (for a bounded first batch)")
    args = ap.parse_args()
    asyncio.run(run(args.rate_per_s, args.dry_run, args.max_closes))


if __name__ == "__main__":
    main()
