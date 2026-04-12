"""
OpportunityMinerSensor (Sensor D) — Multi-strategy code intelligence scanner.

Safety invariant (AC2): ALL miner-generated envelopes require human
acknowledgment before execution.  AI-discovered opportunities must
always be human-approved — auto-submit does NOT apply to this sensor.

Analysis strategies (run in rotation):
  1. Cyclomatic Complexity (CC) — branch-heavy files that need simplification
  2. Function Length — oversized functions that violate SRP
  3. Cognitive Complexity — deeply nested, hard-to-reason-about code
  4. Duplication Density — files with repetitive patterns (DRY violations)
  5. Import Fan-Out — files coupled to too many modules (coupling smell)
  6. TODO/FIXME Density — files with known technical debt markers

Diversity mechanisms:
  - Per-module rotation: scans a different package subtree each cycle
  - Cooldown tracking: recently-queued files are suppressed for N cycles
  - Weighted random sampling: not always top-N, introduces exploration
  - Strategy rotation: different analysis lens each scan

Confidence formula:
    confidence = analysis_evidence_score (full weight)
    Used for envelope prioritisation; does NOT affect requires_human_ack.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auto-ack lane configuration (Task #69 — C1+C3 fix)
# ---------------------------------------------------------------------------
# The auto-ack lane lets the OpportunityMiner rescue coalesced graph batches
# from the AC2 pending_ack gate when the batch passes a strict guard set.
# Without it, every miner batch sits at pending_ack forever and the loop
# never makes safe-module progress (empirically: enqueued=0 across all
# observed cycles in bt-2026-04-12-005521 / bt-2026-04-12-025527).
#
# Why guards matter: the orchestrator's _build_profile (orchestrator.py:4892)
# does NOT thread `source` into OperationProfile, so source-specific risk
# rules are bypassed for ai_miner ops. The empirical risk-tier audit (Apr 11)
# showed 1-file plain miner refactors land in SAFE_AUTO and 2-file in
# NOTIFY_APPLY — silent or near-silent auto-apply. The lane enforces N>=3
# so re-ingested batches always trip the classifier's `too_many_files` /
# `blast_radius_exceeded` rules and route to APPROVAL_REQUIRED (Orange).
#
# Master switches:
#   JARVIS_MINER_AUTO_ACK_LANE        — default false; flip on with
#                                        JARVIS_MINER_GRAPH_AUTO_SUBMIT=true
#   JARVIS_MINER_AUTO_ACK_MIN_FILES   — default 3 (matches the classifier's
#                                        too_many_files threshold)
#   JARVIS_MINER_AUTO_ACK_MAX_FILES   — default 8 (cap blast radius)
#
# Hard constants (not env-tunable; changing them is a code review concern):
_AUTO_ACK_LANE_ENABLED: bool = (
    os.environ.get("JARVIS_MINER_AUTO_ACK_LANE", "false").strip().lower()
    in ("1", "true", "yes", "on")
)
_AUTO_ACK_MIN_FILES: int = int(os.environ.get("JARVIS_MINER_AUTO_ACK_MIN_FILES", "3"))
_AUTO_ACK_MAX_FILES: int = int(os.environ.get("JARVIS_MINER_AUTO_ACK_MAX_FILES", "8"))

# Allowlist of path prefixes for the lane. Excludes scripts/ for v1 because
# loose scripts are a broader blast radius than packaged backend code.
_AUTO_ACK_ALLOWED_PREFIXES: Tuple[str, ...] = ("backend/", "tests/")

# Defense-in-depth fragments — even though the risk classifier already blocks
# these, the lane double-checks before bypassing the AC2 gate. Order matters
# for grep-friendly diagnostics: kernel sentinels first, then security.
_AUTO_ACK_FORBIDDEN_FRAGMENTS: Tuple[str, ...] = (
    "supervisor",
    "auth/",
    "credential",
    "secret",
    "token",
    "encrypt",
    ".env",
)


# ---------------------------------------------------------------------------
# Analysis result types
# ---------------------------------------------------------------------------

@dataclass
class StaticCandidate:
    file_path: str
    cyclomatic_complexity: int
    static_evidence_score: float
    # Extended analysis fields
    strategy: str = "complexity"
    analysis_detail: str = ""


@dataclass
class _FileAnalysis:
    """Full multi-dimensional analysis of a single Python file."""
    file_path: str
    cyclomatic_complexity: int = 0
    max_function_length: int = 0
    cognitive_complexity: int = 0
    duplicate_block_count: int = 0
    import_fan_out: int = 0
    todo_fixme_count: int = 0
    total_lines: int = 0

    @property
    def composite_score(self) -> float:
        """Weighted composite across all dimensions (0.0–1.0 scale)."""
        # Each dimension normalized to 0–1, then weighted
        cc_norm = min(1.0, self.cyclomatic_complexity / 300.0)
        fn_norm = min(1.0, self.max_function_length / 200.0)
        cog_norm = min(1.0, self.cognitive_complexity / 100.0)
        dup_norm = min(1.0, self.duplicate_block_count / 10.0)
        import_norm = min(1.0, self.import_fan_out / 30.0)
        todo_norm = min(1.0, self.todo_fixme_count / 15.0)

        return (
            0.25 * cc_norm
            + 0.20 * fn_norm
            + 0.20 * cog_norm
            + 0.15 * dup_norm
            + 0.10 * import_norm
            + 0.10 * todo_norm
        )


@dataclass
class _CycleCounters:
    """Per-cycle drain counters for safe-module starvation diagnostics (Task #69).

    Definitions are pinned here AND in
    tests/test_ouroboros_governance/test_opportunity_miner_cycle_summary.py;
    keep them in sync. Each counter measures one specific gate so the
    summary line tells you which layer is closed:

      mined           — analyses surviving Phase 1 strategy thresholds
      eligible        — analyses surviving cooldown + seen-file filter
                        (returned by _select_diverse_candidates so the
                         metric cannot drift from the predicate it measures)
      selected        — analyses surviving cap + module diversity + explore
      graph_built     — 1 if MinerGraphCoalescer.coalesce() returned a
                        non-None CoalescedBatch (else 0). Distinguishes
                        "coalesce attempted and built a batch" from
                        "coalesce not attempted / failed".
      graph_submitted — batch.submitted_to_scheduler if a batch exists
                        (gated by JARVIS_MINER_GRAPH_AUTO_SUBMIT + scheduler)
      enqueued        — UnifiedIntakeRouter.ingest() returned "enqueued"
                        (past pending_ack gate, on the priority queue)
      pending_ack     — ingest() returned "pending_ack" (parked, AC2 gate)
      queued_behind   — ingest() returned "queued_behind" (file conflict)
      deduplicated    — ingest() returned "deduplicated" (dedup window hit)
      backpressure    — ingest() returned "backpressure" (queue full)
      auto_acked      — Auto-ack lane successfully rescued a parked envelope
                        via router.acknowledge() (Task #69 C1+C3 fix). When
                        non-zero, expect enqueued >= auto_acked because each
                        rescue increments both counters.

    Reading the summary line:
      mined=N selected=M graph_built=1 graph_submitted=0 …
        → C1: JARVIS_MINER_GRAPH_AUTO_SUBMIT=false is the wall.
      … pending_ack=K enqueued=0 auto_acked=0
        → C3: AC2 requires_human_ack=True is the wall AND lane is off/blocked.
      … pending_ack=K enqueued=K auto_acked=K
        → Lane is working: every parked batch was rescued.
      … enqueued=K (K small relative to mined)
        → C2 (JARVIS_MINER_MAX_PER_SCAN + module_cap) is the next knob.
    """

    mined: int = 0
    eligible: int = 0
    selected: int = 0
    graph_built: int = 0
    graph_submitted: int = 0
    enqueued: int = 0
    pending_ack: int = 0
    queued_behind: int = 0
    deduplicated: int = 0
    backpressure: int = 0
    auto_acked: int = 0



# ---------------------------------------------------------------------------
# Analysis functions
# ---------------------------------------------------------------------------

def _cyclomatic_complexity(tree: ast.AST) -> int:
    """Count branching nodes (if/elif/for/while/with/try/except/and/or)."""
    _BRANCH_NODES = (
        ast.If, ast.For, ast.While, ast.With,
        ast.ExceptHandler, ast.BoolOp,
    )
    count = 1  # baseline
    for node in ast.walk(tree):
        if isinstance(node, _BRANCH_NODES):
            count += 1
    return count


def _max_function_length(tree: ast.AST) -> int:
    """Longest function/method body in lines."""
    max_len = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if hasattr(node, "end_lineno") and node.end_lineno and node.lineno:
                length = node.end_lineno - node.lineno + 1
                max_len = max(max_len, length)
    return max_len


def _cognitive_complexity(tree: ast.AST) -> int:
    """Simplified cognitive complexity: nesting depth * branch count.

    True cognitive complexity (Sonar-style) requires tracking nesting
    increments per scope. This approximation counts branches weighted
    by their nesting depth, which catches the worst offenders.
    """
    score = 0

    def _walk(node: ast.AST, depth: int) -> None:
        nonlocal score
        _NESTING = (ast.If, ast.For, ast.While, ast.With, ast.ExceptHandler)
        _INCREMENT = (ast.If, ast.For, ast.While, ast.With, ast.ExceptHandler, ast.BoolOp)
        child_depth = depth
        if isinstance(node, _NESTING):
            child_depth = depth + 1
        if isinstance(node, _INCREMENT):
            score += 1 + depth  # base increment + nesting penalty
        for child in ast.iter_child_nodes(node):
            _walk(child, child_depth)

    _walk(tree, 0)
    return score


def _duplicate_block_count(source: str) -> int:
    """Count near-duplicate code blocks (simplified line-hash approach).

    Hashes consecutive 4-line windows; counts how many hashes appear >1 time.
    This catches copy-pasted blocks without expensive AST comparison.
    """
    lines = [ln.strip() for ln in source.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if len(lines) < 4:
        return 0

    window_hashes: Dict[str, int] = defaultdict(int)
    for i in range(len(lines) - 3):
        window = "\n".join(lines[i:i + 4])
        h = hashlib.md5(window.encode(), usedforsecurity=False).hexdigest()
        window_hashes[h] += 1

    return sum(1 for count in window_hashes.values() if count > 1)


def _import_fan_out(tree: ast.AST) -> int:
    """Count distinct modules imported (both `import X` and `from X import Y`)."""
    modules: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    return len(modules)


def _todo_fixme_count(source: str) -> int:
    """Count TODO, FIXME, HACK, XXX markers in comments."""
    return len(re.findall(r"#\s*(?:TODO|FIXME|HACK|XXX)\b", source, re.IGNORECASE))


def _analyze_file(file_path: str, source: str, tree: ast.AST) -> _FileAnalysis:
    """Run all analysis dimensions on a parsed file."""
    return _FileAnalysis(
        file_path=file_path,
        cyclomatic_complexity=_cyclomatic_complexity(tree),
        max_function_length=_max_function_length(tree),
        cognitive_complexity=_cognitive_complexity(tree),
        duplicate_block_count=_duplicate_block_count(source),
        import_fan_out=_import_fan_out(tree),
        todo_fixme_count=_todo_fixme_count(source),
        total_lines=len(source.splitlines()),
    )


# ---------------------------------------------------------------------------
# Strategy definitions
# ---------------------------------------------------------------------------

_STRATEGIES: List[Tuple[str, str, str]] = [
    # (strategy_name, sort_field, description_template)
    ("complexity", "cyclomatic_complexity", "High cyclomatic complexity in {path} (CC={value})"),
    ("long_functions", "max_function_length", "Oversized function in {path} (max {value} lines)"),
    ("cognitive_load", "cognitive_complexity", "High cognitive complexity in {path} (score={value})"),
    ("duplication", "duplicate_block_count", "Code duplication detected in {path} ({value} duplicate blocks)"),
    ("coupling", "import_fan_out", "High import fan-out in {path} ({value} modules imported)"),
    ("tech_debt", "todo_fixme_count", "Technical debt markers in {path} ({value} TODO/FIXME)"),
]


# ---------------------------------------------------------------------------
# OpportunityMinerSensor
# ---------------------------------------------------------------------------

class OpportunityMinerSensor:
    """Multi-strategy code intelligence scanner with diversity mechanisms.

    Parameters
    ----------
    repo_root:
        Repository root.
    router:
        UnifiedIntakeRouter.
    scan_paths:
        List of paths (relative to repo_root) to scan recursively for .py files.
    complexity_threshold:
        Minimum cyclomatic complexity to produce an envelope (legacy compat).
    repo:
        Repository name.
    poll_interval_s:
        Seconds between scans in background mode.
    max_candidates_per_scan:
        Cap on candidates per scan cycle (0 = no cap).
    """

    def __init__(
        self,
        repo_root: Path,
        router: Any,
        scan_paths: Optional[List[str]] = None,
        complexity_threshold: int = 10,
        repo: str = "jarvis",
        poll_interval_s: float = 3600.0,
        max_candidates_per_scan: int = 0,
        graph_coalescer: Optional[Any] = None,
    ) -> None:
        self._repo_root = repo_root
        self._router = router
        self._scan_paths = scan_paths or ["."]
        self._threshold = complexity_threshold
        self._repo = repo
        self._poll_interval_s = poll_interval_s
        self._running = False
        self._seen_file_paths: set[str] = set()
        # Graph coalescer: when >=2 candidates are selected in a scan, they
        # are merged into a single ExecutionGraph envelope instead of N
        # independent ops (Manifesto §3 parallel DAG execution).
        self._graph_coalescer = graph_coalescer

        # Per-scan cap
        self._max_per_scan = max_candidates_per_scan or int(
            os.environ.get("JARVIS_MINER_MAX_PER_SCAN", "10")
        )

        # --- Diversity state ---
        # Cooldown: file_path → cycle_number when it was last queued
        self._cooldown_map: Dict[str, int] = {}
        # Number of cycles before a file can be re-queued
        self._cooldown_cycles: int = int(
            os.environ.get("JARVIS_MINER_COOLDOWN_CYCLES", "5")
        )
        # Current scan cycle number
        self._scan_cycle: int = 0
        # Strategy rotation index
        self._strategy_index: int = 0
        # Module rotation: tracks which top-level packages have been scanned
        self._module_scan_history: List[str] = []
        # Full analysis cache (rebuilt each scan, not persisted)
        self._analysis_cache: Dict[str, _FileAnalysis] = {}
        # Exploration ratio: fraction of candidates chosen by weighted random
        # instead of pure top-N. Higher = more diverse, lower = more focused.
        self._explore_ratio: float = float(
            os.environ.get("JARVIS_MINER_EXPLORE_RATIO", "0.4")
        )

    # v350.4: Third-party / non-project directory segments
    _NON_PROJECT_SEGMENTS = frozenset({
        "venv", ".venv", "env", ".env",
        "site-packages", "dist-packages",
        "node_modules", ".git", "__pycache__",
        ".tox", ".nox", ".mypy_cache", ".pytest_cache",
        "tests", "test", "testing", "fixtures",
        "build", "dist", "eggs", ".eggs",
    })

    def _is_production_code(self, py_file: Path, scan_root: Path) -> bool:
        """Return True if the file is production code, not a loose script."""
        parts = py_file.relative_to(self._repo_root).parts if self._repo_root in py_file.parents else py_file.parts
        if self._NON_PROJECT_SEGMENTS.intersection(parts):
            return False
        name = py_file.name
        if name.startswith("test_") or name.endswith("_test.py"):
            return False
        if "scripts" in parts:
            return False
        if name in ("__init__.py", "__main__.py", "conftest.py"):
            return True
        try:
            relative = py_file.relative_to(self._repo_root)
            depth = len(relative.parts) - 1
            return depth >= 2
        except ValueError:
            return True

    def _get_module_name(self, rel_path: str) -> str:
        """Extract the top-level module (e.g., 'backend/core/foo.py' → 'backend.core')."""
        parts = Path(rel_path).parts
        if len(parts) >= 2:
            return ".".join(parts[:2])
        return parts[0] if parts else "unknown"

    def _is_on_cooldown(self, rel_path: str) -> bool:
        """Check if a file is within its cooldown window."""
        if rel_path not in self._cooldown_map:
            return False
        cycles_since = self._scan_cycle - self._cooldown_map[rel_path]
        return cycles_since < self._cooldown_cycles

    def _select_diverse_candidates(
        self,
        analyses: List[_FileAnalysis],
        sort_field: str,
    ) -> Tuple[int, List[_FileAnalysis]]:
        """Select candidates using exploit/explore strategy with module diversity.

        - Top portion (1 - explore_ratio) chosen by strategy score (exploit)
        - Bottom portion (explore_ratio) chosen by weighted random (explore)
        - Module dedup: at most 2 files per top-level module

        Returns ``(eligible_count, selected)``. ``eligible_count`` is the size
        of the post-cooldown / post-seen pool that selection actually sees —
        the same predicate the function applies, returned alongside so the
        ``_CycleCounters.eligible`` metric can never drift from behavior.
        """
        if not analyses:
            return 0, []

        # Filter out cooled-down and already-seen files
        eligible = [
            a for a in analyses
            if not self._is_on_cooldown(a.file_path)
            and a.file_path not in self._seen_file_paths
        ]
        if not eligible:
            # If all are on cooldown, relax cooldown constraint
            eligible = [
                a for a in analyses
                if a.file_path not in self._seen_file_paths
            ]
        if not eligible:
            return 0, []
        eligible_count = len(eligible)

        # Sort by the strategy's primary metric
        eligible.sort(key=lambda a: getattr(a, sort_field, 0), reverse=True)

        n_total = min(self._max_per_scan, len(eligible))
        n_exploit = max(1, int(n_total * (1.0 - self._explore_ratio)))
        n_explore = n_total - n_exploit

        selected: List[_FileAnalysis] = []
        module_counts: Dict[str, int] = defaultdict(int)
        module_cap = 2  # max files per module

        # Exploit: take top-N by strategy metric, with module diversity
        for a in eligible:
            if len(selected) >= n_exploit:
                break
            module = self._get_module_name(a.file_path)
            if module_counts[module] >= module_cap:
                continue
            selected.append(a)
            module_counts[module] += 1

        # Explore: weighted random from remaining pool
        remaining = [a for a in eligible if a not in selected]
        if remaining and n_explore > 0:
            # Weight by composite score so we're biased toward interesting
            # files but not locked to the absolute top
            weights = [max(0.01, a.composite_score) for a in remaining]
            n_sample = min(n_explore, len(remaining))
            try:
                explored = random.choices(remaining, weights=weights, k=n_sample)
                # Deduplicate (choices can repeat)
                seen_in_explore: Set[str] = set()
                for a in explored:
                    if a.file_path not in seen_in_explore:
                        module = self._get_module_name(a.file_path)
                        if module_counts[module] < module_cap + 1:  # slightly relaxed for explore
                            selected.append(a)
                            seen_in_explore.add(a.file_path)
                            module_counts[module] += 1
            except (ValueError, IndexError):
                pass

        return eligible_count, selected[:self._max_per_scan]

    async def scan_once(self) -> List[StaticCandidate]:
        """Run one multi-strategy analysis scan with diversity mechanisms.

        Rotates through analysis strategies each cycle, applies cooldowns,
        and uses exploit/explore selection for diverse coverage.
        """
        self._scan_cycle += 1

        # Pick the strategy for this cycle (round-robin)
        strategy_name, sort_field, desc_template = _STRATEGIES[
            self._strategy_index % len(_STRATEGIES)
        ]
        self._strategy_index += 1

        logger.info(
            "OpportunityMinerSensor: cycle %d, strategy=%s, cooldown=%d files",
            self._scan_cycle, strategy_name, len(self._cooldown_map),
        )

        # Phase 1: Full filesystem scan + multi-dimensional analysis
        # Offloaded to a thread so rglob + ast.parse don't block the loop.
        loop = asyncio.get_running_loop()
        scanned, skipped_non_package, errors, analyses = await loop.run_in_executor(
            None,
            self._scan_files_sync,
            sort_field,
        )

        # Phase 2: Diverse candidate selection
        counters = _CycleCounters(mined=len(analyses))
        eligible_count, selected = self._select_diverse_candidates(analyses, sort_field)
        counters.eligible = eligible_count
        counters.selected = len(selected)

        # Phase 2.5: Try graph coalescing — collapse N selected candidates
        # into a single ExecutionGraph envelope (Manifesto §3 parallel DAG).
        ingested: List[StaticCandidate] = []
        if (
            self._graph_coalescer is not None
            and hasattr(self._graph_coalescer, "should_coalesce")
            and self._graph_coalescer.should_coalesce(selected)
        ):
            batch = await self._graph_coalescer.coalesce(
                selected,
                strategy=strategy_name,
                sort_field=sort_field,
                repo=self._repo,
            )
            if batch is not None:
                counters.graph_built = 1
                counters.graph_submitted = 1 if batch.submitted_to_scheduler else 0
                coalesced = await self._ingest_coalesced_batch(
                    batch, selected, strategy_name, counters,
                )
                if coalesced:
                    ingested.extend(coalesced)
                    logger.info(
                        "OpportunityMinerSensor: coalesced %d candidates into "
                        "graph=%s (strategy=%s, submitted=%s)",
                        len(coalesced), batch.graph.graph_id, strategy_name,
                        batch.submitted_to_scheduler,
                    )
                    # Prune old cooldowns, log, and return early — skip the
                    # per-file ingest loop below.
                    self._prune_cooldowns()
                    self._log_scan_complete(
                        strategy_name, scanned, len(analyses),
                        ingested, errors, skipped_non_package,
                    )
                    self._emit_cycle_summary(counters, strategy_name)
                    return ingested
                # Coalescing envelope was rejected — fall through to per-file
                # ingest as a best-effort fallback.
                logger.info(
                    "OpportunityMinerSensor: coalesced envelope rejected, "
                    "falling back to per-file ingest (strategy=%s)",
                    strategy_name,
                )

        # Phase 3: Ingest selected candidates
        for analysis in selected:
            rel = analysis.file_path
            value = getattr(analysis, sort_field, 0)
            confidence = analysis.composite_score

            description = desc_template.format(path=rel, value=value)
            # Add cross-strategy context
            extra_signals = []
            if analysis.cyclomatic_complexity >= self._threshold and strategy_name != "complexity":
                extra_signals.append(f"CC={analysis.cyclomatic_complexity}")
            if analysis.max_function_length >= 80 and strategy_name != "long_functions":
                extra_signals.append(f"max_fn={analysis.max_function_length}L")
            if analysis.todo_fixme_count >= 3 and strategy_name != "tech_debt":
                extra_signals.append(f"{analysis.todo_fixme_count} TODOs")
            if extra_signals:
                description += f" [also: {', '.join(extra_signals)}]"

            envelope = make_envelope(
                source="ai_miner",
                description=description,
                target_files=(rel,),
                repo=self._repo,
                confidence=max(0.1, confidence),
                urgency="low",
                evidence={
                    "strategy": strategy_name,
                    "primary_metric": sort_field,
                    "primary_value": value,
                    "cyclomatic_complexity": analysis.cyclomatic_complexity,
                    "max_function_length": analysis.max_function_length,
                    "cognitive_complexity": analysis.cognitive_complexity,
                    "duplicate_block_count": analysis.duplicate_block_count,
                    "import_fan_out": analysis.import_fan_out,
                    "todo_fixme_count": analysis.todo_fixme_count,
                    "composite_score": round(analysis.composite_score, 4),
                    "total_lines": analysis.total_lines,
                    "scan_cycle": self._scan_cycle,
                    "signature": f"{strategy_name}:{rel}",
                },
                requires_human_ack=True,  # AC2 safety invariant
            )
            try:
                result = await self._router.ingest(envelope)
                self._record_ingest_result(counters, result)
                if result in ("enqueued", "pending_ack"):
                    self._seen_file_paths.add(rel)
                    self._cooldown_map[rel] = self._scan_cycle
                    ingested.append(StaticCandidate(
                        file_path=rel,
                        cyclomatic_complexity=analysis.cyclomatic_complexity,
                        static_evidence_score=confidence,
                        strategy=strategy_name,
                        analysis_detail=description,
                    ))
                    logger.info(
                        "OpportunityMinerSensor: queued %s (strategy=%s, %s=%d, "
                        "composite=%.3f, result=%s)",
                        rel, strategy_name, sort_field, value,
                        analysis.composite_score, result,
                    )
            except Exception:
                logger.exception(
                    "OpportunityMinerSensor: ingest failed for %s", rel
                )

        # Prune old cooldowns + log completion summary.
        self._prune_cooldowns()
        self._log_scan_complete(
            strategy_name, scanned, len(analyses),
            ingested, errors, skipped_non_package,
        )
        self._emit_cycle_summary(counters, strategy_name)
        return ingested

    # ------------------------------------------------------------------
    # Internal helpers (shared between coalesced and per-file paths)
    # ------------------------------------------------------------------

    def _scan_files_sync(
        self, sort_field: str,
    ) -> Tuple[int, int, int, List[_FileAnalysis]]:
        """CPU-bound Phase 1 scan — runs in a thread via run_in_executor."""
        analyses: List[_FileAnalysis] = []
        scanned = 0
        skipped_non_package = 0
        errors = 0

        for scan_path in self._scan_paths:
            root = self._repo_root / scan_path
            if not root.exists():
                continue
            for py_file in root.rglob("*.py"):
                rel = str(py_file.relative_to(self._repo_root))

                if not self._is_production_code(py_file, root):
                    skipped_non_package += 1
                    continue

                scanned += 1
                try:
                    source = py_file.read_text(encoding="utf-8")
                    tree = ast.parse(source)
                except SyntaxError:
                    errors += 1
                    continue
                except (OSError, UnicodeDecodeError):
                    errors += 1
                    continue

                analysis = _analyze_file(rel, source, tree)
                self._analysis_cache[rel] = analysis

                value = getattr(analysis, sort_field, 0)
                if sort_field == "cyclomatic_complexity" and value < self._threshold:
                    continue
                elif sort_field == "max_function_length" and value < 80:
                    continue
                elif sort_field == "cognitive_complexity" and value < 50:
                    continue
                elif sort_field == "duplicate_block_count" and value < 3:
                    continue
                elif sort_field == "import_fan_out" and value < 15:
                    continue
                elif sort_field == "todo_fixme_count" and value < 3:
                    continue

                analyses.append(analysis)

        return scanned, skipped_non_package, errors, analyses

    def _prune_cooldowns(self) -> None:
        """Drop cooldown entries older than 2× cooldown window."""
        if not self._cooldown_map:
            return
        max_history = self._cooldown_cycles * 2
        oldest_allowed = self._scan_cycle - max_history
        self._cooldown_map = {
            k: v for k, v in self._cooldown_map.items()
            if v >= oldest_allowed
        }

    def _log_scan_complete(
        self,
        strategy_name: str,
        scanned: int,
        analyses_count: int,
        ingested: List[StaticCandidate],
        errors: int,
        skipped_non_package: int,
    ) -> None:
        modules_covered: Set[str] = set()
        for c in ingested:
            modules_covered.add(self._get_module_name(c.file_path))
        logger.info(
            "OpportunityMinerSensor: cycle %d complete — scanned %d files, "
            "strategy=%s, candidates=%d, ingested=%d, modules=%s, "
            "errors=%d, skipped=%d, cooldown_pool=%d, seen_total=%d",
            self._scan_cycle, scanned, strategy_name,
            analyses_count, len(ingested),
            sorted(modules_covered) if modules_covered else "none",
            errors, skipped_non_package,
            len(self._cooldown_map), len(self._seen_file_paths),
        )

    @staticmethod
    def _record_ingest_result(counters: _CycleCounters, result: str) -> None:
        """Tally a UnifiedIntakeRouter.ingest() return value.

        The router's five canonical return strings each feed a distinct
        counter so the per-cycle summary can distinguish "parked at the
        AC2 gate" from "dropped by dedup" from "queue full". Unknown
        return values are silently ignored — defense in depth against
        a future router adding a sixth string.
        """
        if result == "enqueued":
            counters.enqueued += 1
        elif result == "pending_ack":
            counters.pending_ack += 1
        elif result == "queued_behind":
            counters.queued_behind += 1
        elif result == "deduplicated":
            counters.deduplicated += 1
        elif result == "backpressure":
            counters.backpressure += 1

    def _emit_cycle_summary(
        self, counters: _CycleCounters, strategy_name: str,
    ) -> None:
        """Emit one deterministic per-cycle summary line for grep + dashboards.

        The key set is stable (same keys every cycle, zeros included) so
        downstream parsers and tests can rely on a single regex. Only
        ``logger.info`` — no policy effects, no metric drift.
        """
        logger.info(
            "OpportunityMinerSensor cycle_summary "
            "cycle=%d strategy=%s max_per_scan=%d "
            "mined=%d eligible=%d selected=%d "
            "graph_built=%d graph_submitted=%d "
            "enqueued=%d pending_ack=%d queued_behind=%d "
            "deduplicated=%d backpressure=%d auto_acked=%d",
            self._scan_cycle, strategy_name, self._max_per_scan,
            counters.mined, counters.eligible, counters.selected,
            counters.graph_built, counters.graph_submitted,
            counters.enqueued, counters.pending_ack, counters.queued_behind,
            counters.deduplicated, counters.backpressure, counters.auto_acked,
        )

    @staticmethod
    def _check_auto_ack_lane(target_files: Tuple[str, ...]) -> Tuple[bool, str]:
        """Decide whether the auto-ack lane may rescue a parked miner batch.

        Returns ``(eligible, reason)``. ``reason`` is a stable, grep-friendly
        token explaining the decision — emitted in the audit log on both
        success and skip so the lane is fully observable.

        The guards (in evaluation order, first failure wins):
          1. Lane disabled by env var → ``lane_disabled``
          2. Below ``_AUTO_ACK_MIN_FILES`` (default 3) → ``below_min_files``
          3. Above ``_AUTO_ACK_MAX_FILES`` (default 8) → ``above_max_files``
          4. Any file outside ``_AUTO_ACK_ALLOWED_PREFIXES`` → ``path_not_allowed``
          5. Any file matches a forbidden fragment → ``forbidden_fragment``
          6. Any file matches a UserPreferenceMemory FORBIDDEN_PATH substring
             → ``forbidden_user_pref``
          7. All passed → ``ok``

        Defense in depth: rules 4–6 enforce a hard floor even if the risk
        classifier is later modified. The lane never widens its own scope.
        """
        if not _AUTO_ACK_LANE_ENABLED:
            return False, "lane_disabled"
        n = len(target_files)
        if n < _AUTO_ACK_MIN_FILES:
            return False, "below_min_files"
        if n > _AUTO_ACK_MAX_FILES:
            return False, "above_max_files"
        for f in target_files:
            if not any(f.startswith(p) for p in _AUTO_ACK_ALLOWED_PREFIXES):
                return False, "path_not_allowed"
            f_lower = f.lower()
            for frag in _AUTO_ACK_FORBIDDEN_FRAGMENTS:
                if frag in f_lower:
                    return False, "forbidden_fragment"
        # FORBIDDEN_PATH check via the same global hook ToolExecutor uses.
        # Fault-isolated: a missing/broken provider must not break the lane.
        try:
            from backend.core.ouroboros.governance.user_preference_memory import (
                get_protected_path_provider,
            )
            provider = get_protected_path_provider()
            if provider is not None:
                forbidden_substrings = list(provider() or ())
                for f in target_files:
                    for sub in forbidden_substrings:
                        if sub and sub in f:
                            return False, "forbidden_user_pref"
        except Exception:
            logger.debug(
                "OpportunityMinerSensor: forbidden-path provider check failed",
                exc_info=True,
            )
        return True, "ok"

    async def _ingest_coalesced_batch(
        self,
        batch: Any,  # graph_coalescer.CoalescedBatch — duck-typed to avoid import cycle
        selected: List[_FileAnalysis],
        strategy_name: str,
        counters: _CycleCounters,
    ) -> List[StaticCandidate]:
        """Ingest a single coalesced envelope carrying all selected files.

        Returns the list of StaticCandidate records (one per file in the
        graph) on success, or an empty list if the router rejects the
        envelope. Marks all files as seen + on cooldown on success.

        ``counters`` is updated with the router's ingest result so the
        coalesced and per-file paths feed the same per-cycle summary.
        """
        selected_by_path: Dict[str, _FileAnalysis] = {a.file_path: a for a in selected}
        envelope = make_envelope(
            source="ai_miner",
            description=batch.description,
            target_files=batch.target_files,
            repo=self._repo,
            confidence=batch.confidence,
            urgency="low",
            evidence={
                **batch.envelope_evidence,
                "scan_cycle": self._scan_cycle,
                "signature": f"{strategy_name}:coalesced:{batch.graph.graph_id}",
            },
            requires_human_ack=True,  # AC2 safety invariant preserved
        )
        try:
            result = await self._router.ingest(envelope)
        except Exception:
            logger.exception(
                "OpportunityMinerSensor: coalesced ingest raised (graph=%s)",
                batch.graph.graph_id,
            )
            return []

        self._record_ingest_result(counters, result)

        # Auto-ack lane: if the batch landed at pending_ack and the lane
        # guards pass, rescue it via router.acknowledge(). The lane is
        # gated on JARVIS_MINER_AUTO_ACK_LANE (default off) and a strict
        # file count + path allowlist + forbidden-fragment check that
        # bounds the bypass to coalesced batches that will route to
        # APPROVAL_REQUIRED in the risk classifier (3+ files trips
        # too_many_files; see Task #69 audit).
        if result == "pending_ack":
            lane_ok, lane_reason = self._check_auto_ack_lane(batch.target_files)
            if lane_ok:
                extra_evidence = {
                    "auto_acked": True,
                    "auto_ack_reason": "miner_graph_lane",
                    "auto_ack_graph_id": batch.graph.graph_id,
                    "auto_ack_file_count": len(batch.target_files),
                }
                try:
                    rescued = await self._router.acknowledge(
                        envelope.idempotency_key,
                        extra_evidence=extra_evidence,
                    )
                except Exception:
                    logger.exception(
                        "OpportunityMinerSensor: auto_ack lane raised "
                        "(graph=%s)", batch.graph.graph_id,
                    )
                    rescued = False
                if rescued:
                    counters.enqueued += 1
                    counters.auto_acked += 1
                    result = "enqueued"  # treat as success below
                    logger.info(
                        "OpportunityMinerSensor auto_ack lane=miner_graph "
                        "graph_id=%s files=%d strategy=%s reason=%s",
                        batch.graph.graph_id, len(batch.target_files),
                        strategy_name, lane_reason,
                    )
                else:
                    logger.info(
                        "OpportunityMinerSensor auto_ack lane=miner_graph "
                        "graph_id=%s files=%d strategy=%s status=reingest_failed",
                        batch.graph.graph_id, len(batch.target_files),
                        strategy_name,
                    )
            else:
                logger.info(
                    "OpportunityMinerSensor auto_ack lane=miner_graph "
                    "graph_id=%s files=%d strategy=%s status=skipped reason=%s",
                    batch.graph.graph_id, len(batch.target_files),
                    strategy_name, lane_reason,
                )

        if result not in ("enqueued", "pending_ack"):
            logger.info(
                "OpportunityMinerSensor: coalesced envelope not accepted "
                "(result=%s, graph=%s)",
                result, batch.graph.graph_id,
            )
            return []

        out: List[StaticCandidate] = []
        for rel in batch.target_files:
            analysis = selected_by_path.get(rel)
            cc = getattr(analysis, "cyclomatic_complexity", 0) if analysis else 0
            score = getattr(analysis, "composite_score", batch.confidence) if analysis else batch.confidence
            self._seen_file_paths.add(rel)
            self._cooldown_map[rel] = self._scan_cycle
            out.append(StaticCandidate(
                file_path=rel,
                cyclomatic_complexity=cc,
                static_evidence_score=score,
                strategy=strategy_name,
                analysis_detail=f"{batch.description} [graph={batch.graph.graph_id}]",
            ))
        return out

    async def start(self) -> None:
        """Start background scanning loop."""
        self._running = True
        asyncio.create_task(self._poll_loop(), name="opportunity_miner_poll")

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, pure reflex)
    # ------------------------------------------------------------------

    async def subscribe_to_bus(self, event_bus: Any) -> None:
        """Subscribe to file system events for instant complexity detection."""
        await event_bus.subscribe("fs.changed.*", self._on_fs_event)
        logger.info("OpportunityMinerSensor: subscribed to fs.changed.* events")

    async def _on_fs_event(self, event: Any) -> None:
        """React to file change — analyze only the changed file."""
        payload = event.payload
        if payload.get("extension") != ".py":
            return
        if payload.get("is_test_file", False):
            return
        if event.topic == "fs.changed.deleted":
            self._seen_file_paths.discard(payload.get("relative_path", ""))
            self._cooldown_map.pop(payload.get("relative_path", ""), None)
            return
        try:
            await self.scan_file(Path(payload["path"]))
        except Exception:
            logger.debug("OpportunityMinerSensor: event-driven scan error", exc_info=True)

    async def scan_file(self, py_file: Path) -> Optional[StaticCandidate]:
        """Analyze a single file for all dimensions (event-driven path)."""
        try:
            rel = str(py_file.relative_to(self._repo_root))
        except ValueError:
            return None
        if rel in self._seen_file_paths:
            return None
        if self._is_on_cooldown(rel):
            return None
        if not self._is_production_code(py_file, self._repo_root):
            return None

        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, OSError, UnicodeDecodeError):
            return None

        analysis = _analyze_file(rel, source, tree)
        self._analysis_cache[rel] = analysis

        # Gate on composite score for event-driven path
        if analysis.composite_score < 0.3:
            return None

        # Pick the most notable dimension for the description
        dims = [
            ("complexity", "cyclomatic_complexity", analysis.cyclomatic_complexity),
            ("long_functions", "max_function_length", analysis.max_function_length),
            ("cognitive_load", "cognitive_complexity", analysis.cognitive_complexity),
        ]
        dims.sort(key=lambda d: d[2], reverse=True)
        best_strategy, best_field, best_value = dims[0]

        candidate = StaticCandidate(
            file_path=rel,
            cyclomatic_complexity=analysis.cyclomatic_complexity,
            static_evidence_score=analysis.composite_score,
            strategy=best_strategy,
        )

        envelope = make_envelope(
            source="ai_miner",
            description=f"Multi-signal analysis: {rel} (composite={analysis.composite_score:.3f}, {best_field}={best_value})",
            target_files=(rel,),
            repo=self._repo,
            confidence=max(0.1, analysis.composite_score),
            urgency="low",
            evidence={
                "strategy": best_strategy,
                "primary_metric": best_field,
                "primary_value": best_value,
                "cyclomatic_complexity": analysis.cyclomatic_complexity,
                "max_function_length": analysis.max_function_length,
                "cognitive_complexity": analysis.cognitive_complexity,
                "duplicate_block_count": analysis.duplicate_block_count,
                "import_fan_out": analysis.import_fan_out,
                "todo_fixme_count": analysis.todo_fixme_count,
                "composite_score": round(analysis.composite_score, 4),
                "total_lines": analysis.total_lines,
                "signature": f"{best_strategy}:{rel}",
            },
            requires_human_ack=True,
        )
        try:
            result = await self._router.ingest(envelope)
            if result in ("enqueued", "pending_ack"):
                self._seen_file_paths.add(rel)
                self._cooldown_map[rel] = self._scan_cycle
                logger.info(
                    "OpportunityMinerSensor: queued %s (event-driven, strategy=%s, "
                    "composite=%.3f, result=%s)",
                    rel, best_strategy, analysis.composite_score, result,
                )
                return candidate
        except Exception:
            logger.debug("OpportunityMinerSensor: ingest failed for %s", rel, exc_info=True)
        return None

    # ------------------------------------------------------------------
    # Poll fallback (safety net when event spine is unavailable)
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("OpportunityMinerSensor: poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
            except asyncio.CancelledError:
                break
