#!/usr/bin/env python3
"""OCA health smoke — READ-ONLY.

Run: ``python3 scripts/verify_oca.py``

Verifies the closed OCA arc (Slices 1→4 + git_index_guard +
persistent_master + harness_sovereignty_pin) is healthy and the
operator's ritual is active. THREE checks:

  1. pre-commit hook authorizes with NO shell token (your active
     presence+grant ritual is working).
  2. a presence-less, forged ``ide`` context → ``denied_sovereignty``
     (the rogue-Agent defense bites).
  3. the Unix-socket daemon answers ``status`` → authorized.

Read-only: issues NO grants, makes NO commits, never writes to the
operator's ``~/.jarvis`` presence/grant state. Checks 2 & 3 use
throwaway tmp repos / a tmp socket. Exit 0 iff all three PASS.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

REPO = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    capture_output=True, text=True,
).stdout.strip() or os.getcwd()

# Self-bootstrap: make `backend` importable when run as
# `python3 scripts/verify_oca.py` (no PYTHONPATH needed — the
# operator shouldn't have to know about it).
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _ok(name: str, passed: bool, detail: str = "") -> bool:
    mark = "PASS" if passed else "FAIL"
    print(f"  [{mark}] {name}{(' — ' + detail) if detail else ''}")
    return passed


def check_hook_authorizes() -> bool:
    """1. cli hook pre-commit, NO token → exit 0 (ritual active).
    Read-only: the dispatcher authorizes + chains the integrity
    check; with nothing staged it mutates nothing."""
    env = dict(os.environ)
    env.pop("JARVIS_AUTHORIZE_COMMIT_TOKEN", None)
    try:
        r = subprocess.run(
            [sys.executable, "-m",
             "backend.core.ouroboros.governance.commit_authority_cli",
             "hook", "pre-commit"],
            cwd=REPO, env={**env, "PYTHONPATH": REPO},
            capture_output=True, text=True, timeout=30,
        )
        return _ok(
            "hook authorizes with grant, no shell token",
            r.returncode == 0,
            f"exit={r.returncode}"
            + ("" if r.returncode == 0
               else " — run the ritual: `commit_authority_cli "
                    "grant --channel ide --branch <branch>`"),
        )
    except Exception as exc:  # noqa: BLE001
        return _ok("hook authorizes", False, f"error {exc!r}")


def check_forged_ide_denied() -> bool:
    """2. presence-less forged ``ide`` + sovereignty ON →
    ``denied_sovereignty``. Deterministic: sovereignty master is
    forced ON *in this process only* (no file/record writes); a
    throwaway tmp git repo has no presence."""
    try:
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
        os.environ["JARVIS_LEDGER_SOVEREIGNTY_ENABLED"] = "true"
        d = tempfile.mkdtemp()
        r = Path(d) / "x"
        r.mkdir()
        for a in (["init", "-q"], ["config", "user.email", "t@t"],
                  ["config", "user.name", "t"]):
            subprocess.run(["git", *a], cwd=r, capture_output=True)
        (r / "f").write_text("x")
        subprocess.run(["git", "add", "f"], cwd=r,
                        capture_output=True)
        subprocess.run(["git", "commit", "-qm", "s"], cwd=r,
                        capture_output=True)
        ch = oca.resolve_commit_channel(r, "main", env_channel="ide")
        v = oca.verify_pre_commit(
            oca.CommitAuthorityContext(
                channel=ch.value, repo_root=str(r), branch="main",
            )
        )
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        passed = (
            ch.value == "autonomous"
            and v.verdict.value == "denied_sovereignty"
        )
        return _ok(
            "forged ide + no presence → denied_sovereignty",
            passed, f"channel={ch.value} verdict={v.verdict.value}",
        )
    except Exception as exc:  # noqa: BLE001
        return _ok("forged ide denied", False, f"error {exc!r}")


def check_daemon_status() -> bool:
    """3. daemon ``status`` (read-only verb) → authorized for the
    real repo. Tmp socket, started + shut down here."""
    try:
        from backend.core.ouroboros.governance import (
            commit_authority_daemon as dmn,
        )
        sock = f"/tmp/ocad_verify_{os.getpid()}_{uuid.uuid4().hex[:8]}.sock"
        os.environ["JARVIS_COMMIT_AUTHORITY_DAEMON_ENABLED"] = "true"
        os.environ["JARVIS_COMMIT_AUTHORITY_SOCK"] = sock
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=REPO, capture_output=True, text=True,
        ).stdout.strip() or "main"

        async def _run():
            s = await dmn.serve()
            if s is None:
                return None
            try:
                rd, wr = await asyncio.open_unix_connection(sock)
                wr.write((json.dumps({
                    "verb": "status", "repo_root": REPO,
                    "branch": branch,
                }) + "\n").encode())
                await wr.drain()
                raw = await asyncio.wait_for(rd.readline(), 5)
                wr.close()
                return json.loads(raw.decode())
            finally:
                await dmn.shutdown(s)

        resp = asyncio.run(_run())
        if resp is None:
            return _ok("daemon status", False, "serve() returned None")
        passed = (
            resp.get("ok") is True
            and resp.get("dry_verdict") == "authorized"
        )
        return _ok(
            "daemon status → authorized", passed,
            f"presence={resp.get('presence_valid')} "
            f"verdict={resp.get('dry_verdict')}",
        )
    except Exception as exc:  # noqa: BLE001
        return _ok("daemon status", False, f"error {exc!r}")


def main() -> int:
    print(f"OCA health smoke (read-only) — repo={REPO}")
    results = [
        check_hook_authorizes(),
        check_forged_ide_denied(),
        check_daemon_status(),
    ]
    n_pass = sum(1 for x in results if x)
    print(f"\n{n_pass}/{len(results)} checks passed.")
    if n_pass != len(results):
        print(
            "Not all green — see docs/operations/"
            "operator_commit_authority.md for the matching fix."
        )
        return 1
    print("OCA arc healthy; operator ritual active.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
