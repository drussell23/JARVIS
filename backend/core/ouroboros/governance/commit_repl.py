"""``/commit`` — Operator Commit Authority REPL surface (Slice 3 #1).

Auto-discovered via the §32.11 naming-cage convention: file
``commit_repl.py`` → verb ``/commit`` → dispatcher
``dispatch_commit_command(line)``. ZERO edits to
``repl_dispatch_registry.py`` (the basename-minus-``_repl`` rule
maps it; ``commit`` is not in the custom-handler exclusion list).

**Subcommands**

  * ``/commit`` / ``/commit status`` — gate state + dry verify
    for the ``ide`` channel on the current repo/branch.
  * ``/commit grant [--minutes N] [--branch B] [--label L]`` —
    issue a signed, branch-bound ``ide`` grant. ``--branch``
    DEFAULTS to the current git branch (NEVER an empty
    whole-repo grant). Composes :func:`issue_grant`, which also
    mints the operator-presence marker (the channel-resolution
    seam) — so a single ``/commit grant`` is the full operator
    ritual.
  * ``/commit revoke [--id ID | --all]`` — append a revocation
    tombstone (the ledger is append-only). Composes
    :func:`revoke_grants`.
  * ``/commit enable [--label L]`` — write the OCA persistent
    signed master-enable record (the Cursor-SCM-button fix).
    Composes :func:`enable_authority`. NOTE: this is the OCA
    enable; sovereignty's persistent enable is a SEPARATE record
    (``persistent_master``) — the two stay decoupled by design.
  * ``/commit recent [N]`` — last N archived authority events
    (composes :mod:`commit_authority_archive`; graceful when the
    archive substrate is absent / disabled).
  * ``/commit help`` — usage.

**Composition** — operator-facing browser pattern (mirrors
``mode_repl`` / ``posture_repl``): composes ONLY
:mod:`operator_commit_authority` public surface +
:mod:`commit_authority_archive` (read side). The operator's
grant/revoke/enable writes ARE the authority state; this module
holds zero parallel state and NEVER reimplements verification,
the grant ledger, or the HMAC.

**Authority asymmetry** (AST-pinned in the spine): NO orchestrator
/ iron_gate / providers / change_engine / semantic_guardian /
candidate_generator / urgency_router import. Read/issue authority
only — never a commit decision side-channel.

**NEVER raises** — every path defensive; degrades to a friendly
message rather than propagating.
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("Ouroboros.CommitREPL")


_VERBS = ("/commit",)
_VALID_SUBCOMMANDS = {
    "status", "grant", "revoke", "enable", "recent", "help",
}


@dataclass
class CommitDispatchResult:
    """Auto-discovery contract: ``ok: bool``, ``text: str``,
    ``matched: bool`` (mirrors ``ModeDispatchResult``)."""

    ok: bool
    text: str
    matched: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    if not line:
        return False
    return line.split(None, 1)[0] in _VERBS


def _repo_root() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "."


def _current_branch(repo_root: str) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root, capture_output=True, text=True,
            timeout=5, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _parse_opts(args: List[str]) -> dict:
    """Tiny ``--k v`` / ``--flag`` parser. NEVER raises."""
    opts: dict = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("--"):
            key = tok[2:]
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                opts[key] = args[i + 1]
                i += 2
            else:
                opts[key] = True
                i += 1
        else:
            i += 1
    return opts


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_commit_command(line: str) -> CommitDispatchResult:
    """Parse a ``/commit`` line and dispatch. ``matched=False``
    short-circuit lets the registry fall through. NEVER raises."""
    if not _matches(line):
        return CommitDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return CommitDispatchResult(
            ok=False, text=f"/commit: parse error — {exc}",
        )
    args = tokens[1:]
    if not args:
        return _render_status()
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return CommitDispatchResult(
            ok=False,
            text=(
                f"/commit: unknown subcommand {sub!r}. "
                f"Try /commit help."
            ),
        )
    rest = args[1:]
    try:
        if sub == "help":
            return _render_help()
        if sub == "status":
            return _render_status()
        if sub == "grant":
            return _handle_grant(rest)
        if sub == "revoke":
            return _handle_revoke(rest)
        if sub == "enable":
            return _handle_enable(rest)
        if sub == "recent":
            return _handle_recent(rest)
    except Exception as exc:  # noqa: BLE001 — NEVER raise into REPL
        logger.debug("[CommitREPL] %s failed: %s", sub, exc)
        return CommitDispatchResult(
            ok=False, text=f"/commit {sub}: failed (non-fatal): {exc}",
        )
    return CommitDispatchResult(
        ok=False, text=f"/commit: unhandled subcommand {sub!r}",
    )


def _render_help() -> CommitDispatchResult:
    return CommitDispatchResult(ok=True, text=(
        "/commit — Operator Commit Authority surface (Slice 3)\n"
        "\n"
        "  /commit [status]                gate state + dry "
        "verify (ide, current branch)\n"
        "  /commit grant [--minutes N]     issue signed ide "
        "grant + mint presence\n"
        "          [--branch B] [--label L]  (--branch defaults "
        "to current branch)\n"
        "  /commit revoke [--id ID|--all]  append revocation "
        "tombstone\n"
        "  /commit enable [--label L]      OCA persistent "
        "master-enable (Cursor SCM)\n"
        "  /commit recent [N]              last N archived "
        "authority events\n"
        "  /commit help                    this message\n"
        "\n"
        "Composes operator_commit_authority (no parallel state). "
        "A single `/commit grant` is the full operator ritual "
        "(grant + presence)."
    ))


def _oca():
    """Lazy import of the substrate. Returns the module or None."""
    try:
        from backend.core.ouroboros.governance import (
            operator_commit_authority as oca,
        )
        return oca
    except Exception:  # noqa: BLE001
        return None


def _render_status() -> CommitDispatchResult:
    oca = _oca()
    if oca is None:
        return CommitDispatchResult(
            ok=False, text="/commit: OCA substrate unavailable",
        )
    root = _repo_root()
    branch = _current_branch(root)
    try:
        master = oca.master_enabled()
        ctx = oca.CommitAuthorityContext(
            channel=oca.resolve_commit_channel(
                Path(root), branch, env_channel="",
            ).value,
            repo_root=root,
            branch=branch,
        )
        verdict = oca.verify_pre_commit(ctx)
    except Exception as exc:  # noqa: BLE001
        return CommitDispatchResult(
            ok=False, text=f"/commit status: read failed: {exc}",
        )
    return CommitDispatchResult(ok=True, text=(
        f"/commit status:\n"
        f"  master_enabled  = {master}\n"
        f"  repo_root       = {root}\n"
        f"  branch          = {branch or '(detached/unknown)'}\n"
        f"  resolved_channel= {ctx.channel}\n"
        f"  dry_verify      = {verdict.verdict.value}"
        f"{(' — ' + verdict.detail) if verdict.detail else ''}"
    ))


def _handle_grant(rest: List[str]) -> CommitDispatchResult:
    oca = _oca()
    if oca is None:
        return CommitDispatchResult(
            ok=False, text="/commit: OCA substrate unavailable",
        )
    opts = _parse_opts(rest)
    root = _repo_root()
    branch = (
        str(opts.get("branch")).strip()
        if isinstance(opts.get("branch"), str)
        else _current_branch(root)
    )
    if not branch:
        return CommitDispatchResult(ok=False, text=(
            "/commit grant: cannot resolve current branch and no "
            "--branch given. Refusing an empty whole-repo grant "
            "(structural: grants are branch-bound by default)."
        ))
    minutes_raw = opts.get("minutes")
    ttl_s: Optional[int] = None
    if isinstance(minutes_raw, str):
        try:
            ttl_s = max(1, int(float(minutes_raw))) * 60
        except (TypeError, ValueError):
            return CommitDispatchResult(
                ok=False,
                text=f"/commit grant: bad --minutes {minutes_raw!r}",
            )
    label = (
        str(opts.get("label")).strip()
        if isinstance(opts.get("label"), str)
        else "repl-commit-grant"
    )
    try:
        out = oca.issue_grant(
            channel="ide",
            operator_label=label,
            ttl_s=ttl_s,
            branch=branch,
            repo_root=Path(root),
        )
    except Exception as exc:  # noqa: BLE001
        return CommitDispatchResult(
            ok=False, text=f"/commit grant: failed: {exc}",
        )
    if not getattr(out, "ok", False):
        return CommitDispatchResult(
            ok=False,
            text=f"/commit grant: refused — {getattr(out, 'error', '?')}",
        )
    _archive_best_effort(
        "grant_issue",
        {
            "grant_id": out.grant_id, "channel": "ide",
            "branch": branch, "label": label,
        },
    )
    return CommitDispatchResult(ok=True, text=(
        f"/commit grant: issued ide grant {out.grant_id[:12]}… "
        f"branch={branch} (presence minted). "
        f"expires_at_unix={out.expires_at_unix:.0f}"
    ))


def _handle_revoke(rest: List[str]) -> CommitDispatchResult:
    oca = _oca()
    if oca is None:
        return CommitDispatchResult(
            ok=False, text="/commit: OCA substrate unavailable",
        )
    opts = _parse_opts(rest)
    revoke_all = bool(opts.get("all"))
    gid = opts.get("id")
    if not revoke_all and not isinstance(gid, str):
        return CommitDispatchResult(ok=False, text=(
            "/commit revoke: need --id <grant_id> or --all"
        ))
    try:
        n = oca.revoke_grants(
            grant_id=gid if isinstance(gid, str) else None,
            revoke_all=revoke_all,
        )
    except Exception as exc:  # noqa: BLE001
        return CommitDispatchResult(
            ok=False, text=f"/commit revoke: failed: {exc}",
        )
    _archive_best_effort(
        "revoke",
        {"all": revoke_all, "grant_id": gid if isinstance(gid, str) else ""},
    )
    target = "ALL grants" if revoke_all else f"grant {gid}"
    return CommitDispatchResult(
        ok=bool(n),
        text=(
            f"/commit revoke: {target} — "
            f"{'tombstone appended' if n else 'append failed'}"
        ),
    )


def _handle_enable(rest: List[str]) -> CommitDispatchResult:
    oca = _oca()
    if oca is None:
        return CommitDispatchResult(
            ok=False, text="/commit: OCA substrate unavailable",
        )
    opts = _parse_opts(rest)
    label = (
        str(opts.get("label")).strip()
        if isinstance(opts.get("label"), str)
        else "repl-commit-enable"
    )
    try:
        ok = oca.enable_authority(label)
    except Exception as exc:  # noqa: BLE001
        return CommitDispatchResult(
            ok=False, text=f"/commit enable: failed: {exc}",
        )
    _archive_best_effort("enable", {"label": label, "ok": ok})
    return CommitDispatchResult(ok=bool(ok), text=(
        "/commit enable: OCA persistently enabled (signed) — "
        "Cursor SCM works with no shell env"
        if ok else
        "/commit enable: failed (secret/crypto unavailable)"
    ))


def _handle_recent(rest: List[str]) -> CommitDispatchResult:
    n = 10
    if rest:
        try:
            n = max(1, min(200, int(rest[0])))
        except (TypeError, ValueError):
            pass
    try:
        from backend.core.ouroboros.governance import (
            commit_authority_archive as arch,
        )
    except Exception:  # noqa: BLE001
        return CommitDispatchResult(
            ok=True,
            text="/commit recent: archive substrate not present",
        )
    try:
        records = arch.recent(n)
    except Exception as exc:  # noqa: BLE001
        return CommitDispatchResult(
            ok=False, text=f"/commit recent: read failed: {exc}",
        )
    if not records:
        return CommitDispatchResult(
            ok=True, text="/commit recent: (no archived events)",
        )
    lines = [f"/commit recent (last {len(records)}):"]
    for r in records:
        lines.append(
            f"  {r.get('ref', '?')}  {r.get('kind', '?')}  "
            f"{r.get('detail', '')}"[:160]
        )
    return CommitDispatchResult(ok=True, text="\n".join(lines))


def _archive_best_effort(kind: str, detail: dict) -> None:
    """Append to the authority archive if present. NEVER raises;
    archive absence/disable is silent (telemetry, not authority)."""
    try:
        from backend.core.ouroboros.governance import (
            commit_authority_archive as arch,
        )
        arch.record(kind=kind, detail=detail)
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "CommitDispatchResult",
    "dispatch_commit_command",
]
