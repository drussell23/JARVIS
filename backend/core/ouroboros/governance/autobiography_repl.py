"""``/autobiography`` REPL — §40 Wave 1 #8 operator surface
(PRD v2.98+).

Auto-discovered via §32.11 Slice 4 ``repl_dispatch_registry`` by
§33.3 naming-cage convention (filename ``autobiography_repl.py``
→ verb ``autobiography``).

Subcommands::

    /autobiography                      alias for ``status``
    /autobiography status               aggregate report panel
    /autobiography refresh              recompute audit now
    /autobiography commit <hash>        detailed per-commit audit
    /autobiography escapes              list every commit with
                                        a corpus match
    /autobiography corpus               echo canonical corpus
                                        size + categories covered
    /autobiography help                 this text

Thin browser over ``adversarial_autobiography``. Composes the
canonical substrate via lazy-import; falls through cleanly when
master is off or substrate unavailable. NEVER raises.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, List

logger = logging.getLogger(__name__)


AUTOBIOGRAPHY_REPL_SCHEMA_VERSION: str = "autobiography_repl.1"


_HELP = (
    "/autobiography — §40 Wave 1 #8 adversarial autobiography\n"
    "\n"
    "Retrospective auditor — runs the canonical P9.4 corpus\n"
    "against O+V's own commit history (its 'autobiography') to\n"
    "find Quine-shaped hallucinations that already shipped.\n"
    "Closes §3.6.2 Vector #7 (Quine-shape cage bypass)\n"
    "empirically — the prospective P9.4 harness proves the\n"
    "cage CAN catch each pattern; this surface proves the cage\n"
    "HAS held on every commit O+V actually shipped.\n"
    "\n"
    "Findings (per-commit + aggregate):\n"
    "  ⚠ CORPUS_ESCAPE     pattern shipped despite the cage\n"
    "  ✓ CORPUS_CLEAN      audited, no patterns matched\n"
    "  ○ CORPUS_NO_COMMITS no OV-signed commits in scan window\n"
    "  ◌ CORPUS_DISABLED   master flag off\n"
    "\n"
    "Subcommands:\n"
    "  /autobiography                alias for /status\n"
    "  /autobiography status         aggregate audit panel\n"
    "  /autobiography refresh        recompute now\n"
    "  /autobiography commit <hash>  detailed per-commit audit\n"
    "  /autobiography escapes        list commits w/ matches\n"
    "  /autobiography corpus         canonical corpus summary\n"
    "  /autobiography help           this text\n"
    "\n"
    "Master flag: JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED "
    "(default false per §33.1).\n"
)


@dataclass(frozen=True)
class AutobiographyReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = AUTOBIOGRAPHY_REPL_SCHEMA_VERSION


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/autobiography"
        or s == "autobiography"
        or s.startswith("/autobiography ")
        or s.startswith("autobiography ")
    )


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.adversarial_autobiography import (  # noqa: E501
            master_enabled,
        )
        return bool(master_enabled())
    except Exception:  # noqa: BLE001
        return False


def dispatch_autobiography_command(
    line: str,
) -> AutobiographyReplDispatchResult:
    """§32.11 Slice 4 canonical entry point — auto-discovered."""
    if not _matches(line):
        return AutobiographyReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return AutobiographyReplDispatchResult(
            ok=False,
            text=f"  /autobiography parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return AutobiographyReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                "  /autobiography: retrospective auditor "
                "disabled (default per §33.1). Set "
                "JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED=true."
            ),
        )

    try:
        if head == "status":
            return _render_status()
        if head == "refresh":
            return _render_refresh()
        if head == "commit":
            return _render_commit(
                args[1] if len(args) >= 2 else "",
            )
        if head == "escapes":
            return _render_escapes()
        if head == "corpus":
            return _render_corpus()
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                f"  /autobiography: unknown subcommand "
                f"{head!r}. Try /autobiography help."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                f"  /autobiography: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _render_status() -> AutobiographyReplDispatchResult:
    from backend.core.ouroboros.governance.adversarial_autobiography import (  # noqa: E501
        audit_autobiography,
        format_autobiography_panel,
    )
    report = audit_autobiography()
    out = format_autobiography_panel(report)
    return AutobiographyReplDispatchResult(ok=True, text=out)


def _render_refresh() -> AutobiographyReplDispatchResult:
    from backend.core.ouroboros.governance.adversarial_autobiography import (  # noqa: E501
        audit_autobiography,
    )
    report = audit_autobiography(force_refresh=True)
    parts: List[str] = [
        "# /autobiography refresh",
        f"  audited_at_unix   : {report.audited_at_unix:.0f}",
        f"  finding           : {report.finding.value}",
        f"  commits_audited   : {report.commits_audited}",
        f"  escape_count      : {report.escape_count}",
        f"  clean_count       : {report.clean_count}",
        f"  cage_health_ratio : {report.cage_health_ratio:.3f}",
        f"  elapsed_s         : {report.elapsed_s:.3f}",
    ]
    return AutobiographyReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_commit(commit_hash: str) -> AutobiographyReplDispatchResult:
    if not commit_hash:
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                "  /autobiography commit: hash required "
                "(full or prefix)"
            ),
        )
    from backend.core.ouroboros.governance.adversarial_autobiography import (  # noqa: E501
        audit_autobiography,
        format_commit_audit,
        get_cached_audits,
    )
    audit_autobiography()
    audits = get_cached_audits()
    target = commit_hash.strip().lower()
    found = None
    for a in audits:
        if a.commit_hash.lower().startswith(target):
            found = a
            break
    if found is None:
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                f"  /autobiography commit: no audit matches "
                f"prefix {commit_hash!r} (run /autobiography "
                "refresh; only OV-signed commits are audited)"
            ),
        )
    return AutobiographyReplDispatchResult(
        ok=True, text=format_commit_audit(found),
    )


def _render_escapes() -> AutobiographyReplDispatchResult:
    from backend.core.ouroboros.governance.adversarial_autobiography import (  # noqa: E501
        AutobiographyFinding,
        audit_autobiography,
        get_cached_audits,
    )
    audit_autobiography()
    audits = get_cached_audits()
    escapes = [
        a for a in audits
        if a.finding is AutobiographyFinding.CORPUS_ESCAPE
    ]
    if not escapes:
        return AutobiographyReplDispatchResult(
            ok=True,
            text=(
                "# /autobiography escapes — none. Cage has "
                f"held on all {len(audits)} audited commit(s)."
            ),
        )
    parts: List[str] = [
        f"# /autobiography escapes ({len(escapes)} found)",
    ]
    for a in escapes:
        short = (a.commit_hash or "?")[:12]
        cats = sorted({m.category for m in a.matches})
        parts.append(
            f"  ⚠ {short}  matches={len(a.matches)}  "
            f"categories={','.join(cats) or '?'}"
        )
    return AutobiographyReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_corpus() -> AutobiographyReplDispatchResult:
    """Echo canonical corpus state — confirms the substrate is
    composing the canonical P9.4 corpus correctly."""
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            categories_covered,
            corpus_size,
        )
        size = corpus_size()
        cats = sorted(c.value for c in categories_covered())
    except Exception as exc:  # noqa: BLE001
        return AutobiographyReplDispatchResult(
            ok=False,
            text=(
                f"  /autobiography corpus: canonical "
                f"p9_4_adversarial_corpus unavailable "
                f"({type(exc).__name__})"
            ),
        )
    parts: List[str] = [
        "# /autobiography corpus (canonical P9.4)",
        f"  total entries     : {size}",
        f"  categories covered: {len(cats)}",
    ]
    for c in cats:
        parts.append(f"    - {c}")
    return AutobiographyReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def register_verbs(registry: Any) -> int:
    """Auto-discovered registration hook for help_dispatcher."""
    if registry is None:
        return 0
    try:
        registry.register(
            verb="autobiography",
            description=(
                "Adversarial autobiography — retrospective "
                "audit of OV-signed commits against the "
                "canonical P9.4 corpus"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "autobiography_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "AUTOBIOGRAPHY_REPL_SCHEMA_VERSION",
    "AutobiographyReplDispatchResult",
    "dispatch_autobiography_command",
    "register_verbs",
]
