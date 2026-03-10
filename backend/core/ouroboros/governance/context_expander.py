"""
Context Expander — Pre-Generation Context Expansion Loop
=========================================================

Executes up to MAX_ROUNDS bounded expansion rounds before GENERATE.
Each round sends a lightweight planning prompt (description + filenames only,
NO file contents) and reads back additional_files_needed (capped at
MAX_FILES_PER_ROUND per Engineering Mandate).

Governor limits are HARDCODED — they cannot be changed at runtime.
No unconstrained loops. Bounded execution time guaranteed.

Schema version: expansion.1
  {"schema_version": "expansion.1", "additional_files_needed": [...], "reasoning": "..."}
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from backend.core.ouroboros.governance.op_context import OperationContext, OperationPhase

logger = logging.getLogger("Ouroboros.ContextExpander")

# ── Governor limits (Engineering Mandate — hardcoded, not configurable) ──
MAX_ROUNDS: int = 2
MAX_FILES_PER_ROUND: int = 5
MAX_FILES_PER_CATEGORY: int = 10          # Token Explosion Trap limit per category

_EXPANSION_SCHEMA_VERSION = "expansion.1"


class ContextExpander:
    """Drives bounded CONTEXT_EXPANSION rounds, enriching ctx.expanded_context_files.

    Parameters
    ----------
    generator:
        CandidateGenerator (or any object with plan(prompt, deadline) -> str).
    repo_root:
        Root path for resolving and safety-checking additional files.
    """

    def __init__(
        self,
        generator: Any,
        repo_root: Path,
        oracle: Optional[Any] = None,
    ) -> None:
        self._generator = generator
        self._repo_root = repo_root
        self._oracle = oracle

    async def expand(
        self,
        ctx: OperationContext,
        deadline: datetime,
    ) -> OperationContext:
        """Run up to MAX_ROUNDS expansion rounds, enriching ctx.expanded_context_files.

        Each round:
          1. Builds lightweight prompt (description + filenames only — no file contents)
          2. Calls generator.plan(prompt, deadline) -> raw string
          3. Parses expansion.1 JSON response
          4. Resolves file paths against repo_root (missing files silently skipped)
          5. Accumulates confirmed paths

        Stops early if:
          - additional_files_needed is empty
          - generator raises
          - response is invalid JSON or wrong schema_version
          - no confirmed files after resolution

        Returns ctx unchanged if no files were accumulated.
        Returns ctx.with_expanded_files(tuple) otherwise.
        Never raises — all errors produce the unmodified ctx.
        """
        accumulated: List[str] = []

        for round_num in range(MAX_ROUNDS):
            prompt = self._build_expansion_prompt(ctx, accumulated, oracle=self._oracle)

            try:
                raw = await self._generator.plan(prompt, deadline)
            except Exception as exc:
                logger.warning(
                    "[ContextExpander] op=%s round=%d plan() failed: %s; stopping expansion",
                    ctx.op_id, round_num + 1, exc,
                )
                break

            new_paths = self._parse_expansion_response(raw)
            if not new_paths:
                logger.debug(
                    "[ContextExpander] op=%s round=%d: no additional files requested",
                    ctx.op_id, round_num + 1,
                )
                break

            confirmed = self._resolve_files(new_paths)
            if not confirmed:
                logger.debug(
                    "[ContextExpander] op=%s round=%d: none of %d requested files found on disk",
                    ctx.op_id, round_num + 1, len(new_paths),
                )
                break

            accumulated.extend(confirmed)
            logger.info(
                "[ContextExpander] op=%s round=%d: added %d files (%d total accumulated)",
                ctx.op_id, round_num + 1, len(confirmed), len(accumulated),
            )

        if not accumulated:
            return ctx

        # Deduplicate while preserving insertion order
        seen: set = set()
        deduped: List[str] = []
        for p in accumulated:
            if p not in seen:
                seen.add(p)
                deduped.append(p)

        return ctx.with_expanded_files(tuple(deduped))

    def _build_expansion_prompt(
        self,
        ctx: OperationContext,
        already_fetched: List[str],
        oracle: Optional[Any] = None,
    ) -> str:
        """Build a lightweight prompt — filenames only, no file contents."""
        target_list = "\n".join(f"  - {f}" for f in ctx.target_files)
        fetched_list = (
            "\n".join(f"  - {f}" for f in already_fetched)
            if already_fetched
            else "  (none yet)"
        )
        available_section = ""
        if oracle is not None:
            try:
                status = oracle.get_status()
                if status.get("running", False):
                    target_abs = [
                        self._repo_root / f for f in ctx.target_files
                    ]
                    neighborhood = oracle.get_file_neighborhood(target_abs)
                    available_section = self._render_neighborhood_section(neighborhood)
            except Exception:
                available_section = ""  # fall back silently
        return (
            f"Task: {ctx.description}\n\n"
            f"Target files to be modified:\n{target_list}\n\n"
            f"Context files already fetched:\n{fetched_list}\n\n"
            f"{available_section}"
            f"Which additional files (if any) would help understand the context for this task?\n"
            f"List only files that exist in the codebase. Do NOT request the target files themselves.\n\n"
            f"Return ONLY this JSON:\n"
            f'{{"schema_version": "expansion.1", '
            f'"additional_files_needed": ["path/relative/to/repo.py", ...], '
            f'"reasoning": "<one sentence max 200 chars>"}}'
        )

    def _render_neighborhood_section(self, neighborhood: Any) -> str:
        """Render a FileNeighborhood as a labeled multi-section string.

        Each category is limited to MAX_FILES_PER_CATEGORY entries.
        Truncated categories append ``"  ... (and N more)"``.
        Returns empty string if neighborhood has no neighbors.
        """
        try:
            categories = neighborhood.to_dict()
        except Exception:
            return ""
        if not categories:
            return ""

        lines = ["\nStructural file neighborhood (real codebase graph edges):"]
        for category, paths in categories.items():
            label = category.replace("_", " ").title()
            shown = paths[:MAX_FILES_PER_CATEGORY]
            hidden = len(paths) - len(shown)
            lines.append(f"  {label}:")
            for p in shown:
                lines.append(f"    - {p}")
            if hidden > 0:
                lines.append(f"    ... (and {hidden} more)")
        lines.append(
            "\nWhich of these (if any) would help you understand the context?\n"
        )
        return "\n".join(lines)

    def _parse_expansion_response(self, raw: str) -> List[str]:
        """Parse expansion.1 JSON, returning up to MAX_FILES_PER_ROUND paths.

        Returns empty list on any error — expansion is best-effort.
        """
        try:
            stripped = raw.strip()
            # Strip markdown fences if present
            if stripped.startswith("```"):
                lines = stripped.split("\n")
                stripped = "\n".join(
                    line for line in lines if not line.startswith("```")
                ).strip()
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            logger.debug("[ContextExpander] Response is not valid JSON; skipping round")
            return []

        if not isinstance(data, dict):
            return []

        if data.get("schema_version") != _EXPANSION_SCHEMA_VERSION:
            logger.debug(
                "[ContextExpander] Wrong schema_version: %r (expected %r)",
                data.get("schema_version"),
                _EXPANSION_SCHEMA_VERSION,
            )
            return []

        files = data.get("additional_files_needed", [])
        if not isinstance(files, list):
            return []

        valid = [f for f in files if isinstance(f, str) and f.strip()]

        if len(valid) > MAX_FILES_PER_ROUND:
            logger.warning(
                "[ContextExpander] Response requested %d files; truncating to %d (governor limit)",
                len(valid), MAX_FILES_PER_ROUND,
            )
            valid = valid[:MAX_FILES_PER_ROUND]

        return valid

    def _resolve_files(self, paths: List[str]) -> List[str]:
        """Return paths that exist on disk within repo_root.

        Silently skips missing files, symlinks, and paths outside repo_root.
        """
        from backend.core.ouroboros.governance.providers import _safe_context_path
        from backend.core.ouroboros.governance.test_runner import BlockedPathError

        confirmed: List[str] = []
        for p in paths:
            abs_candidate = (self._repo_root / p).resolve()
            try:
                _safe_context_path(self._repo_root, abs_candidate)
            except BlockedPathError:
                logger.debug("[ContextExpander] Skipping blocked path: %s", p)
                continue
            if not abs_candidate.exists():
                logger.debug("[ContextExpander] Skipping missing file: %s", p)
                continue
            confirmed.append(p)

        return confirmed
