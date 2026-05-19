#!/usr/bin/env python3
"""
Operator Commit Authority -- CLI + hook dispatcher (Slice 2)
============================================================

Consumer of the :mod:`operator_commit_authority` substrate. This is
the versioned replacement for the untracked bash Iron Gate wrapper.
It is a *consumer* (not the substrate) so it may freely compose the
substrate, run git, exec the chained integrity hook, and read env.

Subcommands
-----------
* ``hook pre-commit`` -- the gate dispatcher git invokes.
* ``grant``           -- issue a signed, time-bounded commit grant.
* ``revoke``          -- revoke a grant (by id or all).
* ``status``          -- show gate state + a dry verify.

Behavior contract (zero behavior change until graduated)
--------------------------------------------------------
* OCA master **OFF** (default ``JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED``
  unset): the legacy operator token check is enforced with the *same*
  SHA-256 semantics as the retired bash hook (sha256 of the exact
  ``JARVIS_AUTHORIZE_COMMIT_TOKEN`` bytes vs the trimmed contents of
  ``~/.jarvis/commit_token.sha256``, constant-time compare). The gate
  is byte-equivalent to pre-Slice-2 -- nothing changes for the
  operator until they opt in.
* OCA master **ON**: operator channels (repl/cli/ide/daemon) require a
  signed grant on disk; **no env-var export is needed for the IDE
  GUI** -- that is the entire point of OCA.
* On authority pass (either path), the dispatcher chains to the
  file-integrity guardian ``pre-commit.project`` -- existing
  protection is preserved, not replaced.

The legacy token check lives **here, once**. The bash file is retired
by ``install_hooks.py``; there is no parallel copy of the logic.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Legacy operator-token compatibility (single source -- retires the bash)
# ---------------------------------------------------------------------------

_LEGACY_TOKEN_ENV = "JARVIS_AUTHORIZE_COMMIT_TOKEN"
_LEGACY_HASH_FILE_ENV = "JARVIS_COMMIT_TOKEN_HASH_FILE"
_DEFAULT_HASH_FILE = "~/.jarvis/commit_token.sha256"

_RED = "\033[1;31m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _refuse(reason: str, hint: str = "") -> None:
    sys.stderr.write(
        f"\n{_RED}# IRON GATE -- Commit blocked: {reason}{_RESET}\n"
    )
    if hint:
        sys.stderr.write(f"   {hint}\n")
    sys.stderr.write("\n")


def legacy_token_ok() -> Tuple[bool, str]:
    """Mirror the retired bash hook exactly. NEVER raises.

    bash: ``sha256(printf '%s' "$TOKEN")`` vs trimmed hash-file
    contents, length+constant-time compare, fail-closed on any gap.
    """
    token = os.environ.get(_LEGACY_TOKEN_ENV, "")
    if not token:
        return False, f"{_LEGACY_TOKEN_ENV} unset"
    hash_file = (
        os.environ.get(_LEGACY_HASH_FILE_ENV, "").strip()
        or os.path.expanduser(_DEFAULT_HASH_FILE)
    )
    try:
        expected = (
            Path(hash_file)
            .expanduser()
            .read_text(encoding="utf-8")
            .strip()
        )
    except Exception:  # noqa: BLE001
        return False, (
            f"expected-hash file missing/unreadable: {hash_file}"
        )
    if not expected:
        return False, "expected-hash file empty (fail-closed)"
    actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if len(actual) != len(expected) or not hmac.compare_digest(
        actual, expected
    ):
        return False, "token hash mismatch"
    return True, "legacy operator token verified"


# ---------------------------------------------------------------------------
# Git introspection (bounded; never shell=True)
# ---------------------------------------------------------------------------


def _git(args: List[str], root: str) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None


def _repo_root() -> str:
    r = _git(["rev-parse", "--show-toplevel"], os.getcwd())
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return os.getcwd()


def _branch(root: str) -> str:
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], root)
    return r.stdout.strip() if r and r.returncode == 0 else ""


def _staged_files(root: str) -> Tuple[str, ...]:
    r = _git(
        ["diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        root,
    )
    if r is None or r.returncode != 0:
        return ()
    return tuple(
        ln.strip() for ln in r.stdout.splitlines() if ln.strip()
    )


def _chain_project_hook(root: str) -> int:
    """Run the file-integrity guardian after authority passes.
    Mirrors the bash ``exec "$_NEXT"`` chain. If no integrity hook is
    installed, do not block (authority already passed)."""
    candidates: List[Path] = []
    r = _git(["rev-parse", "--git-path", "hooks"], root)
    if r is not None and r.returncode == 0 and r.stdout.strip():
        hp = Path(r.stdout.strip())
        if not hp.is_absolute():
            hp = Path(root) / hp
        candidates.append(hp / "pre-commit.project")
    candidates.append(Path(root) / "scripts" / "hooks" / "pre-commit.project")
    for cand in candidates:
        if cand.exists():
            try:
                return subprocess.run(
                    [sys.executable, str(cand)],
                    cwd=root,
                    check=False,
                ).returncode
            except Exception as exc:  # noqa: BLE001 — fail closed
                _refuse(
                    f"file-integrity hook failed to run: "
                    f"{type(exc).__name__}"
                )
                return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_hook_pre_commit() -> int:
    root = _repo_root()
    # Late import so the dispatcher works even if the substrate moves.
    try:
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed
        _refuse(
            f"OCA substrate unavailable: {type(exc).__name__}",
            "fail closed -- commit blocked",
        )
        return 1

    if oca.master_enabled():
        channel = (
            os.environ.get("JARVIS_COMMIT_CHANNEL", "").strip().lower()
            or "ide"
        )
        ctx = oca.CommitAuthorityContext(
            channel=channel,
            repo_root=root,
            branch=_branch(root),
            staged_files=_staged_files(root),
        )
        verdict = oca.verify_pre_commit(ctx)
        if not verdict.authorized():
            _refuse(
                f"{verdict.verdict.value} -- {verdict.detail}",
                "operator: issue a grant -> "
                "`/commit grant <minutes>` (Serpent REPL) or "
                "`python3 -m backend.core.ouroboros.governance"
                ".commit_authority_cli grant --minutes 60`",
            )
            return 1
        sys.stderr.write(
            f"{_DIM}# OCA: {verdict.verdict.value} "
            f"({verdict.detail}){_RESET}\n"
        )
    else:
        ok, reason = legacy_token_ok()
        if not ok:
            _refuse(
                f"Missing operator cryptographic authorization "
                f"({reason})",
                "operator: export "
                "JARVIS_AUTHORIZE_COMMIT_TOKEN before an "
                "authorized commit (or graduate OCA: set "
                "JARVIS_OPERATOR_COMMIT_AUTHORITY_ENABLED=true "
                "and use signed grants).",
            )
            return 1

    return _chain_project_hook(root)


def cmd_grant(args: argparse.Namespace) -> int:
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )

    root = _repo_root()
    ttl_s = int(args.minutes) * 60 if args.minutes else None
    outcome = oca.issue_grant(
        channel=args.channel,
        operator_label=(
            args.label
            or os.environ.get("USER")
            or "operator"
        ),
        ttl_s=ttl_s,
        scopes=tuple(args.scope or ()),
        branch=args.branch or "",
        governance_amend=bool(args.governance_amend),
        repo_root=Path(root),
    )
    if not outcome.ok:
        print(f"grant FAILED: {outcome.error}", file=sys.stderr)
        return 1
    print(
        "grant issued: "
        f"id={outcome.grant_id} "
        f"channel={args.channel} "
        f"expires_at_unix={outcome.expires_at_unix:.0f} "
        f"ledger={outcome.grants_path_str}"
    )
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )

    n = oca.revoke_grants(
        grant_id=args.id,
        revoke_all=bool(args.all),
    )
    print(
        "revoked all grants"
        if args.all
        else f"revoke recorded: id={args.id} (n={n})"
    )
    return 0 if n == 1 else 1


def cmd_enable(args: argparse.Namespace) -> int:
    """Persistently graduate OCA (signed, out-of-repo). This is what
    makes the Cursor/VS Code SCM button work -- no shell env needed."""
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )

    label = (
        args.label
        or os.environ.get("USER")
        or "operator"
    )
    if oca.enable_authority(label):
        print(
            "OCA persistently ENABLED (signed) at "
            f"{oca.enable_file_path()} -- master is now ON for GUI "
            "git (Cursor SCM) with no shell env. label=" + label
        )
        return 0
    print(
        "enable FAILED (could not bootstrap secret or write record)",
        file=sys.stderr,
    )
    return 1


def cmd_disable(_args: argparse.Namespace) -> int:
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )

    if oca.disable_authority():
        print(
            "OCA persistently DISABLED (enable record removed); "
            "master reverts to env-only (default FALSE)."
        )
        return 0
    print("disable FAILED (enable record still present)", file=sys.stderr)
    return 1


def cmd_status(_args: argparse.Namespace) -> int:
    from backend.core.ouroboros.governance import (
        operator_commit_authority as oca,
    )

    root = _repo_root()
    sp = oca.secret_path()
    ep = oca.enable_file_path()
    print("Operator Commit Authority -- status")
    print(f"  master_enabled        : {oca.master_enabled()}")
    print(
        f"  persistent enable     : {oca.persistent_enabled()} "
        f"({ep} {'present' if ep.exists() else 'absent'})"
    )
    print(f"  grants ledger         : {oca.grants_path()}")
    print(
        f"  per-machine secret    : {sp} "
        f"({'present' if sp.exists() else 'absent'})"
    )
    print(f"  default grant TTL (s) : {oca.default_ttl_s()}")
    if oca.master_enabled():
        ctx = oca.CommitAuthorityContext(
            channel="ide",
            repo_root=root,
            branch=_branch(root),
            staged_files=_staged_files(root),
        )
        v = oca.verify_pre_commit(ctx)
        print(
            f"  dry verify (ide)      : {v.verdict.value} "
            f"-- {v.detail}"
        )
    else:
        ok, reason = legacy_token_ok()
        print(
            f"  legacy token path     : "
            f"{'OK' if ok else 'NOT SATISFIED'} ({reason})"
        )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="commit_authority_cli",
        description="Operator Commit Authority CLI + hook dispatcher",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hook", help="git hook dispatcher")
    h.add_argument(
        "phase", choices=["pre-commit"], help="hook phase"
    )

    g = sub.add_parser("grant", help="issue a signed commit grant")
    g.add_argument(
        "--minutes",
        type=int,
        default=None,
        help="grant lifetime in minutes (default: adaptive TTL)",
    )
    g.add_argument(
        "--channel",
        default="ide",
        help="commit channel (repl/cli/ide/daemon/autonomous)",
    )
    g.add_argument(
        "--scope",
        action="append",
        default=[],
        help="repo-relative path prefix the grant covers "
        "(repeatable; default: whole repo)",
    )
    g.add_argument("--branch", default="", help="bind to a branch")
    g.add_argument("--label", default="", help="operator audit label")
    g.add_argument(
        "--governance-amend",
        action="store_true",
        help="permit a governance/ drift commit",
    )

    r = sub.add_parser("revoke", help="revoke grant(s)")
    r.add_argument("--id", default=None, help="grant id to revoke")
    r.add_argument(
        "--all", action="store_true", help="revoke all grants"
    )

    e = sub.add_parser(
        "enable",
        help="persistently graduate OCA (signed, out-of-repo) -- "
        "makes Cursor/VS Code SCM work with no shell env",
    )
    e.add_argument("--label", default="", help="operator audit label")

    sub.add_parser(
        "disable", help="remove the persistent enable record"
    )

    sub.add_parser("status", help="show gate state + dry verify")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "hook" and args.phase == "pre-commit":
        return cmd_hook_pre_commit()
    if args.cmd == "grant":
        return cmd_grant(args)
    if args.cmd == "revoke":
        return cmd_revoke(args)
    if args.cmd == "enable":
        return cmd_enable(args)
    if args.cmd == "disable":
        return cmd_disable(args)
    if args.cmd == "status":
        return cmd_status(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
