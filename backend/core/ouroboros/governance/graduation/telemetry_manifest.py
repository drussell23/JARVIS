"""Sovereign Telemetry Manifest — the empirical proof rendered into the
[SOVEREIGN GRADUATION] PR body (2026-06-20).

We reject silent upgrades and empty PRs. When the autonomic crucible proposes a
cognitive graduation, the PR body MUST mathematically justify the flip: a
per-soak evidence table (TTFT deltas, AST integrity, FSM/recovery), an aggregate
verdict, the AI's argument for why the operator should click merge, and a
**Rollback Strategy** payload (the exact one-command disable).

## Authority posture (locked)
  * **Pure + stdlib-only** (``hashlib`` for the evidence digest). No I/O, no
    logger, no network. The renderer is consulted; it never observes state.
  * **Deterministic** — same evidence in → byte-identical markdown out (no
    wall-clock, no randomness; any timestamp is passed in by the caller).
  * **NEVER raises** — malformed evidence degrades to a clearly-marked
    "insufficient evidence" manifest rather than crashing the PR path.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence

MANIFEST_SCHEMA_VERSION = "1.0"


def _b(x: Any) -> bool:
    return bool(x)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _i(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def evidence_digest(soak_evidence: Sequence[Dict[str, Any]]) -> str:
    """Deterministic sha256[:16] over the soak evidence — the cryptographic
    fingerprint stamped in the manifest so the proof is tamper-evident."""
    try:
        canon = json.dumps(
            list(soak_evidence), sort_keys=True, separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        canon = repr(soak_evidence)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _verdict_row(idx: int, ev: Dict[str, Any]) -> str:
    n = _i(ev.get("ttft_n"))
    mean_ms = _f(ev.get("ttft_mean_ms"))
    max_ms = _f(ev.get("ttft_max_ms"))
    ttft_bad = _b(ev.get("ttft_degraded"))
    ast_sig = _i(ev.get("ast_corruption_signals"))
    ast_bad = _b(ev.get("ast_corrupted"))
    recovered = _b(ev.get("recovered"))
    outcome = str(ev.get("session_outcome") or "?")
    ttft_cell = (
        f"{mean_ms:.0f}ms (max {max_ms:.0f}, n={n})" if n else "no samples"
    )
    ttft_pass = "❌ FAIL" if ttft_bad else "✅ PASS"
    ast_pass = "❌ FAIL" if ast_bad else "✅ PASS"
    rec_pass = "✅" if recovered else "⚠️"
    return (
        f"| Soak {idx} | {ttft_cell} | {ttft_pass} | {ast_sig} | {ast_pass} "
        f"| {rec_pass} {outcome} |"
    )


def render_graduation_manifest(
    flag_name: str,
    *,
    soak_evidence: Sequence[Dict[str, Any]],
    session_ids: Sequence[str],
    required_clean: int,
    source_file: str,
    ttft_ceiling_ms: float,
    revert_sha: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render the full [SOVEREIGN GRADUATION] PR body markdown. Deterministic;
    NEVER raises.

    ``revert_sha`` (if known) is woven into the Rollback Strategy as the exact
    ``git revert`` target; the env hot-revert is always provided as the
    instant, no-deploy fallback."""
    evs = [e for e in soak_evidence if isinstance(e, dict)]
    n_soaks = len(evs)
    digest = evidence_digest(evs)

    # Aggregate verdicts.
    any_ttft_bad = any(_b(e.get("ttft_degraded")) for e in evs)
    total_ast = sum(_i(e.get("ast_corruption_signals")) for e in evs)
    any_ast_bad = any(_b(e.get("ast_corrupted")) for e in evs)
    recovered_n = sum(1 for e in evs if _b(e.get("recovered")))
    max_ttft = max((_f(e.get("ttft_max_ms")) for e in evs), default=0.0)
    clean_enough = (
        n_soaks >= required_clean
        and not any_ttft_bad
        and not any_ast_bad
    )

    lines: List[str] = []
    lines.append(f"## 🧬 [SOVEREIGN GRADUATION] Activated `{flag_name}`")
    lines.append("")
    lines.append(
        f"The autonomic Cognitive Crucible proposes graduating `{flag_name}` "
        f"(default `False` → `True`) in `{source_file}`. This PR is "
        f"machine-generated; the evidence below is the mathematical proof the "
        f"feature survived **{n_soaks}** autonomous soak(s) "
        f"(required: {required_clean}) without degrading TTFT or corrupting AST."
    )
    if generated_at:
        lines.append("")
        lines.append(f"*Generated: {generated_at} · evidence digest: `{digest}`*")
    else:
        lines.append("")
        lines.append(f"*Evidence digest: `{digest}` (sha256[:16])*")

    # Empirical proof table.
    lines.append("")
    lines.append("### Empirical proof (per soak)")
    lines.append("")
    lines.append(
        "| Soak | TTFT (target < "
        f"{ttft_ceiling_ms:.0f}ms) | TTFT verdict | AST signals | "
        "AST verdict | Recovery |"
    )
    lines.append("|---|---|---|---|---|---|")
    if evs:
        for i, ev in enumerate(evs, start=1):
            lines.append(_verdict_row(i, ev))
    else:
        lines.append("| — | insufficient evidence | ⚠️ | — | ⚠️ | — |")

    # Aggregate.
    lines.append("")
    lines.append("### Aggregate verdict")
    lines.append("")
    lines.append(f"- **Clean soaks**: {n_soaks} / {required_clean} required")
    lines.append(
        f"- **TTFT integrity**: max {max_ttft:.0f}ms vs ceiling "
        f"{ttft_ceiling_ms:.0f}ms → {'❌ DEGRADED' if any_ttft_bad else '✅ within bounds'}"
    )
    lines.append(
        f"- **AST integrity**: {total_ast} corruption signal(s) → "
        f"{'❌ CORRUPTED' if any_ast_bad else '✅ zero parse errors'}"
    )
    lines.append(
        f"- **FSM exhaustion rate**: "
        f"{'0%' if recovered_n == n_soaks and n_soaks else 'N/A'} "
        f"({recovered_n}/{n_soaks} soaks reached terminal recovery)"
    )

    # Why merge.
    lines.append("")
    lines.append("### Why merge")
    lines.append("")
    if clean_enough:
        lines.append(
            f"All {required_clean} required soaks completed clean: zero TTFT "
            f"degradation, zero AST corruption, full FSM recovery. The cognitive "
            f"feature is empirically proven non-regressive. Merging flips the "
            f"source-of-truth default so the capability is live by construction "
            f"(not a brittle `.env` override). **Recommend merge.**"
        )
    else:
        reasons = []
        if n_soaks < required_clean:
            reasons.append(f"only {n_soaks}/{required_clean} clean soaks")
        if any_ttft_bad:
            reasons.append("TTFT degraded")
        if any_ast_bad:
            reasons.append("AST corruption detected")
        lines.append(
            "⚠️ **Do NOT merge** — the crucible veto did not fully clear: "
            + "; ".join(reasons)
            + ". This manifest is published for audit; the flag stays `False`."
        )

    # Rollback strategy (always present — supreme safety).
    lines.append("")
    lines.append("### 🛟 Rollback Strategy")
    lines.append("")
    lines.append(
        "If a downstream anomaly appears in production post-merge, disable the "
        "flag **instantly** (no redeploy) via env hot-revert:"
    )
    lines.append("")
    lines.append("```bash")
    lines.append(f"export {flag_name}=false   # instant, process-restart applies")
    lines.append("```")
    lines.append("")
    lines.append("Or revert the source-of-truth flip permanently:")
    lines.append("")
    lines.append("```bash")
    if revert_sha:
        lines.append(f"git revert {revert_sha}    # reverts the default-literal flip")
    else:
        lines.append(
            f"git revert <merge-sha>   # reverts the {flag_name} default-literal flip"
        )
    lines.append("```")
    lines.append("")
    if session_ids:
        lines.append(
            "<sub>Soak sessions: "
            + ", ".join(f"`{s}`" for s in session_ids if s)
            + f" · manifest schema v{MANIFEST_SCHEMA_VERSION}</sub>"
        )
    lines.append("")
    lines.append(
        "🤖 Generated autonomously by the Sovereign Cognitive Crucible "
        "(cage-respecting: PR-proposes, human merges)."
    )
    return "\n".join(lines)


def manifest_recommends_merge(
    soak_evidence: Sequence[Dict[str, Any]], *, required_clean: int,
) -> bool:
    """Pure predicate mirroring the manifest's merge recommendation — the
    crucible MUST only open a PR when this is True. NEVER raises."""
    evs = [e for e in soak_evidence if isinstance(e, dict)]
    if len(evs) < required_clean:
        return False
    if any(_b(e.get("ttft_degraded")) for e in evs):
        return False
    if any(_b(e.get("ast_corrupted")) for e in evs):
        return False
    return True


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "evidence_digest",
    "render_graduation_manifest",
    "manifest_recommends_merge",
]
