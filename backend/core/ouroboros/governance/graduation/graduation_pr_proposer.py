"""Sovereign GitOps Governance Matrix — source-of-truth graduation PR (2026-06-20).

When the autonomic crucible greenlights a cognitive flag (3 clean soaks, zero
TTFT/AST veto), it graduates by rewriting the DEFINITIVE source-of-truth — the
``os.environ.get("<FLAG>", "<falsy>")`` default literal in the flag's consuming
module — NOT by appending to a brittle ``.env``. The rewrite lands on a branch
and is surfaced as a ``[SOVEREIGN GRADUATION]`` PR carrying the Telemetry
Manifest; the merge to ``main`` stays the one human/OCA gate (the Order-2 cage's
``amendment_requires_operator`` is locked-true — the organism PROPOSES, it does
not self-merge).

This module owns two layers:

  * :func:`flip_default_to_true` — the PURE, bounded, AST-validated rewriter.
    Conservative by construction: flips ONLY a single unambiguous falsy default
    literal for the exact flag; refuses (changed=False) on zero matches, >1
    match, an already-truthy default, or a result that fails ``ast.parse``.
  * :func:`propose_graduation_pr` — the gated orchestration that composes the
    rewriter + the Telemetry Manifest + the existing OrangePRReviewer git/gh
    mechanics. Gated by ``JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED`` (default off).

## Authority posture (locked)
  * The rewriter is **pure + stdlib-only** (``ast`` + ``re``), NEVER raises,
    and is the single tested decision point. The orchestration reuses the
    audited OrangePRReviewer (branch + commit + ``gh pr create``) — no new
    git/subprocess surface invented here.
  * **Never self-merges** — opens a PR; the human/OCA merge gate is preserved.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence

# Falsy default literals that mean "feature disabled by default".
_FALSY_LITERALS = frozenset({"", "false", "0", "no", "off"})


@dataclass(frozen=True)
class RewriteResult:
    changed: bool
    new_text: str
    matches: int
    detail: str


def _flag_default_pattern(flag: str) -> re.Pattern:
    """Match ``os.environ.get("<FLAG>", "<literal>")`` capturing the quote +
    inner default literal. Tolerates single/double quotes + inner whitespace."""
    f = re.escape(flag)
    # group 'q' = opening quote of the default literal; 'lit' = its contents.
    return re.compile(
        r"os\.environ\.get\(\s*[\"']" + f + r"[\"']\s*,\s*"
        r"(?P<q>[\"'])(?P<lit>[^\"']*)(?P=q)\s*\)"
    )


def flip_default_to_true(source_text: str, flag: str) -> RewriteResult:
    """Flip the flag's single falsy ``os.environ.get`` default literal to
    ``"true"``. PURE; NEVER raises.

    Refuses (changed=False) when:
      * the flag's default pattern is not found (0 matches)
      * more than one match exists (ambiguous — abstain rather than guess)
      * the existing default literal is already truthy (already graduated)
      * the rewritten module fails ``ast.parse`` (structural safety)
    """
    if not isinstance(source_text, str) or not isinstance(flag, str) or not flag:
        return RewriteResult(False, source_text or "", 0, "bad_input")
    pat = _flag_default_pattern(flag)
    matches = list(pat.finditer(source_text))
    n = len(matches)
    if n == 0:
        return RewriteResult(False, source_text, 0, "no_default_literal_found")
    if n > 1:
        return RewriteResult(
            False, source_text, n, f"ambiguous_{n}_matches_abstained",
        )
    m = matches[0]
    cur = m.group("lit").strip().lower()
    if cur not in _FALSY_LITERALS:
        return RewriteResult(
            False, source_text, 1, f"already_truthy_default:{cur!r}",
        )
    q = m.group("q")
    replacement = m.group(0).replace(
        f"{q}{m.group('lit')}{q}", f'{q}true{q}', 1,
    )
    new_text = source_text[: m.start()] + replacement + source_text[m.end():]
    # Structural safety: the rewrite MUST still parse.
    try:
        ast.parse(new_text)
    except SyntaxError as exc:  # pragma: no cover - defensive
        return RewriteResult(False, source_text, 1, f"ast_parse_failed:{exc}")
    return RewriteResult(True, new_text, 1, "flipped_to_true")


# ---------------------------------------------------------------------------
# Gated orchestration (composes rewriter + manifest + OrangePRReviewer)
# ---------------------------------------------------------------------------


def graduation_pr_enabled() -> bool:
    """Master gate for the autonomous source-of-truth graduation PR. Default
    FALSE — the rewriter + manifest are always importable/testable; only the
    live branch+PR action is gated."""
    return os.environ.get(
        "JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED", "false",
    ).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class ProposalResult:
    proposed: bool
    flag: str
    source_file: str
    pr_url: Optional[str]
    detail: str


def _resolve_source_file(flag: str, registry: Any) -> Optional[str]:
    """The FlagSpec.source_file is the declared home of the flag's default.
    NEVER raises."""
    try:
        spec = registry.get_spec(flag)
        sf = getattr(spec, "source_file", None) if spec else None
        return str(sf) if sf else None
    except Exception:  # noqa: BLE001
        return None


async def propose_graduation_pr(
    flag: str,
    *,
    soak_evidence: Sequence[Dict[str, Any]],
    session_ids: Sequence[str],
    required_clean: int,
    ttft_ceiling_ms: float,
    repo_root: str,
    registry: Any = None,
    reviewer: Any = None,
    generated_at: Optional[str] = None,
) -> ProposalResult:
    """Compose: verify merge-recommendation → locate source → flip default →
    render manifest → open the [SOVEREIGN GRADUATION] PR via OrangePRReviewer.

    Gated (graduation_pr_enabled). Returns a structured result; NEVER raises.
    Does NOT merge — the human/OCA gate is preserved by construction."""
    from backend.core.ouroboros.governance.graduation.telemetry_manifest import (
        manifest_recommends_merge,
        render_graduation_manifest,
    )

    if not graduation_pr_enabled():
        return ProposalResult(False, flag, "", None, "gate_disabled")

    # The crucible only PROPOSES when the evidence clears the math veto.
    if not manifest_recommends_merge(soak_evidence, required_clean=required_clean):
        return ProposalResult(False, flag, "", None, "evidence_did_not_clear_veto")

    if registry is None:
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                ensure_seeded,
            )
            registry = ensure_seeded()
        except Exception as exc:  # noqa: BLE001
            return ProposalResult(False, flag, "", None, f"registry_unavailable:{exc}")

    source_file = _resolve_source_file(flag, registry)
    if not source_file:
        return ProposalResult(False, flag, "", None, "source_file_unknown")

    abs_path = os.path.join(repo_root, source_file)
    try:
        with open(abs_path, "r", encoding="utf-8") as fh:
            original = fh.read()
    except OSError as exc:
        return ProposalResult(False, flag, source_file, None, f"read_failed:{exc}")

    rw = flip_default_to_true(original, flag)
    if not rw.changed:
        return ProposalResult(
            False, flag, source_file, None, f"rewrite_abstained:{rw.detail}",
        )

    body = render_graduation_manifest(
        flag,
        soak_evidence=soak_evidence,
        session_ids=session_ids,
        required_clean=required_clean,
        source_file=source_file,
        ttft_ceiling_ms=ttft_ceiling_ms,
        generated_at=generated_at,
    )

    if reviewer is None:
        try:
            import pathlib
            from backend.core.ouroboros.governance.orange_pr_reviewer import (
                OrangePRReviewer,
            )
            reviewer = OrangePRReviewer(pathlib.Path(repo_root))
        except Exception as exc:  # noqa: BLE001
            return ProposalResult(False, flag, source_file, None, f"reviewer_unavailable:{exc}")

    try:
        pr = await reviewer.create_review_pr(
            op_id=f"sovereign-graduation-{flag.lower()}",
            description=f"[SOVEREIGN GRADUATION] Activated {flag}",
            files=[(source_file, rw.new_text)],
            evidence={"flag": flag, "soaks": list(session_ids)},
            risk_tier_name="APPROVAL_REQUIRED",
            body_override=body,
            title_override=f"[SOVEREIGN GRADUATION] Activated {flag}",
        )
    except TypeError:
        # OrangePRReviewer without body_override support — fall back to default
        # body (the manifest is still in the evidence + commit).
        pr = await reviewer.create_review_pr(
            op_id=f"sovereign-graduation-{flag.lower()}",
            description=f"[SOVEREIGN GRADUATION] Activated {flag}",
            files=[(source_file, rw.new_text)],
            evidence={"flag": flag, "manifest": body},
            risk_tier_name="APPROVAL_REQUIRED",
        )
    except Exception as exc:  # noqa: BLE001
        return ProposalResult(False, flag, source_file, None, f"pr_create_failed:{exc}")

    url = getattr(pr, "url", None) if pr else None
    return ProposalResult(
        bool(pr), flag, source_file, url,
        "pr_opened" if pr else "pr_create_returned_none",
    )


__all__ = [
    "RewriteResult",
    "flip_default_to_true",
    "graduation_pr_enabled",
    "ProposalResult",
    "propose_graduation_pr",
]
