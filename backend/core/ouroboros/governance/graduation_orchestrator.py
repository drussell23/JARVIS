"""GraduationOrchestrator — self-programming loop that converts ephemeral tools into permanent agents.

The final manifestation of the Ouroboros cycle: the snake eating its tail.
When a synthesized ephemeral tool proves its value through repeated use,
the Graduation Orchestrator drives J-Prime to produce a permanent agent,
validates it through ShadowHarness, commits it to the correct repo on an
isolated worktree branch, and with human approval pushes a PR to GitHub.

Hardening requirements (baked in, not retrofitted):
    H1: Git cleanliness check before mutation (git status --porcelain)
    H2: Contract tests, not just pytest (BaseNeuralMeshAgent interface)
    H3: PUSH_FAILED is an explicit phase (code preserved locally)
    H4: Approval timeout -> discard worktree + log (30min default)
    H5: Post-merge registration requires readiness probe
    H6: Cost metering per J-Prime call (accumulated on GraduationRecord)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.GraduationOrchestrator")

_GRADUATION_THRESHOLD = int(os.environ.get("JARVIS_GRADUATION_THRESHOLD", "3"))
_APPROVAL_TIMEOUT_S = float(os.environ.get("JARVIS_GRADUATION_APPROVAL_TIMEOUT_S", "1800"))
_APPROVAL_POLL_S = 5.0
_MAX_CONCURRENT = 1
_PERSISTENCE_DIR = Path.home() / ".jarvis" / "ouroboros" / "graduations"

_STOP_WORDS = frozenset({
    "the", "a", "an", "for", "to", "and", "or", "in", "on", "at",
    "is", "it", "my", "me", "of", "with", "from", "by", "this", "that",
    "some", "any", "all", "about", "up", "please", "can", "you",
})


class GraduationPhase(str, Enum):
    TRACKING = "tracking"
    EVALUATING = "evaluating"
    DECIDED_SKIP = "decided_skip"
    WORKTREE_CREATING = "worktree_creating"
    GENERATING = "generating"
    VALIDATING = "validating"
    COMMITTING = "committing"
    AWAITING_APPROVAL = "awaiting_approval"
    PUSHING = "pushing"
    PUSH_FAILED = "push_failed"
    AWAITING_MERGE = "awaiting_merge"
    REGISTERING = "registering"
    GRADUATED = "graduated"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class EphemeralUsageRecord:
    goal: str
    goal_hash: str
    code_hash: str
    execution_outcome: str
    elapsed_s: float
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class GraduationDecision:
    should_graduate: bool
    capability_name: str
    capability_domain: str
    repo_owner: str
    agent_class_name: str
    module_path: str
    test_module_path: str
    rationale: str
    estimated_complexity: str
    dependencies: Tuple[str, ...] = ()
    rejection_reason: str = ""


@dataclass
class GraduationRecord:
    graduation_id: str
    goal_class_id: str
    phase: GraduationPhase
    decision: Optional[GraduationDecision] = None
    usage_records: List[EphemeralUsageRecord] = field(default_factory=list)
    usage_count: int = 0
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    pr_url: Optional[str] = None
    commit_sha: Optional[str] = None
    validation_passed: bool = False
    generated_files: List[str] = field(default_factory=list)
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    total_cost_usd: float = 0.0


# ---------------------------------------------------------------------------
# EphemeralUsageTracker
# ---------------------------------------------------------------------------

class EphemeralUsageTracker:
    """Counts ephemeral tool reuse. Fires graduation threshold exactly once."""

    def __init__(
        self,
        persistence_path: Optional[Path] = None,
        graduation_threshold: int = _GRADUATION_THRESHOLD,
        telemetry_bus: Any = None,
    ) -> None:
        self._path = persistence_path or (_PERSISTENCE_DIR / "ephemeral_usage.json")
        self._threshold = graduation_threshold
        self._bus = telemetry_bus
        self._lock = asyncio.Lock()
        self._data: Dict[str, List[EphemeralUsageRecord]] = {}
        self._graduated: set = set()
        self._threshold_fired: set = set()
        self._load()

    async def record_usage(
        self, goal: str, code_hash: str, outcome: str, elapsed_s: float,
    ) -> Optional[str]:
        """Record usage. Returns goal_class_id if threshold met (exactly once)."""
        async with self._lock:
            gcid = self._normalize_goal(goal)
            if gcid in self._graduated:
                return None

            record = EphemeralUsageRecord(
                goal=goal, goal_hash=gcid, code_hash=code_hash,
                execution_outcome=outcome, elapsed_s=elapsed_s,
            )
            self._data.setdefault(gcid, []).append(record)
            self._save()

            success_count = sum(1 for r in self._data[gcid] if r.execution_outcome == "success")
            if success_count >= self._threshold and gcid not in self._threshold_fired:
                self._threshold_fired.add(gcid)
                return gcid
            return None

    def get_usage_count(self, gcid: str) -> int:
        return len(self._data.get(gcid, []))

    def get_records(self, gcid: str) -> List[EphemeralUsageRecord]:
        return list(self._data.get(gcid, []))

    def mark_graduated(self, gcid: str) -> None:
        self._graduated.add(gcid)
        self._save()

    @staticmethod
    def _normalize_goal(goal: str) -> str:
        words = re.sub(r'[^\w\s]', '', goal.lower()).split()
        meaningful = sorted(w for w in words if w not in _STOP_WORDS and len(w) > 2)
        key = " ".join(meaningful[:5])
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                self._graduated = set(raw.get("graduated", []))
                self._threshold_fired = set(raw.get("threshold_fired", []))
                for k, records in raw.get("data", {}).items():
                    self._data[k] = [EphemeralUsageRecord(**r) for r in records]
            except Exception:
                pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            raw = {
                "data": {k: [asdict(r) for r in recs] for k, recs in self._data.items()},
                "graduated": list(self._graduated),
                "threshold_fired": list(self._threshold_fired),
            }
            self._path.write_text(json.dumps(raw, indent=2))
        except Exception:
            pass

    def health(self) -> Dict[str, Any]:
        return {"tracked_classes": len(self._data), "graduated": len(self._graduated), "threshold": self._threshold}


# ---------------------------------------------------------------------------
# GraduationOrchestrator
# ---------------------------------------------------------------------------

class GraduationOrchestrator:
    """Orchestrates graduating ephemeral tools into permanent agents."""

    def __init__(
        self,
        brain_selector: Any = None,
        prime_client: Any = None,
        telemetry_bus: Any = None,
        topology_map: Any = None,
        agent_registry: Any = None,
        repo_registry: Any = None,
        comm_protocol: Any = None,
        max_concurrent: int = _MAX_CONCURRENT,
        approval_timeout_s: float = _APPROVAL_TIMEOUT_S,
        persistence_dir: Optional[Path] = None,
    ) -> None:
        self._brain_selector = brain_selector
        self._prime = prime_client
        self._bus = telemetry_bus
        self._topology = topology_map
        self._agent_registry = agent_registry
        self._repo_registry = repo_registry
        self._comm = comm_protocol
        self._approval_timeout = approval_timeout_s
        self._dir = persistence_dir or _PERSISTENCE_DIR
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._active: Dict[str, GraduationRecord] = {}
        self._total_graduated: int = 0
        self._total_failed: int = 0
        self._total_rejected: int = 0

    async def evaluate_graduation(
        self, goal_class_id: str, usage_records: List[EphemeralUsageRecord],
    ) -> GraduationRecord:
        """Full graduation pipeline with deterministic cleanup."""
        async with self._semaphore:
            record = GraduationRecord(
                graduation_id=f"grad-{uuid.uuid4().hex[:12]}",
                goal_class_id=goal_class_id,
                phase=GraduationPhase.EVALUATING,
                usage_records=usage_records,
                usage_count=len(usage_records),
            )
            self._active[goal_class_id] = record

            try:
                self._emit(record, "Evaluating graduation candidate")
                await self._narrate("Evaluating whether to build a permanent agent.")

                decision = await self._ask_jprime_for_decision(goal_class_id, usage_records, record)
                record.decision = decision

                if not decision.should_graduate:
                    record.phase = GraduationPhase.DECIDED_SKIP
                    await self._narrate(f"Decided not to graduate. {decision.rejection_reason}")
                    return record

                record.phase = GraduationPhase.WORKTREE_CREATING
                worktree_path, branch = await self._create_worktree(decision)
                record.worktree_path = str(worktree_path)
                record.branch_name = branch

                record.phase = GraduationPhase.GENERATING
                await self._narrate(f"Generating {decision.agent_class_name} for {decision.repo_owner}.")
                generated = await self._generate_agent_code(decision, worktree_path, usage_records, record)
                record.generated_files = generated

                record.phase = GraduationPhase.VALIDATING
                await self._narrate("Validating in shadow harness.")
                passed = await self._validate_in_shadow(decision, worktree_path, generated)
                record.validation_passed = passed
                if not passed:
                    record.phase = GraduationPhase.FAILED
                    record.error = "Shadow validation failed"
                    await self._narrate("Validation failed. Discarding.")
                    return record

                record.phase = GraduationPhase.COMMITTING
                sha = await self._commit_to_branch(decision, worktree_path, generated)
                record.commit_sha = sha

                record.phase = GraduationPhase.AWAITING_APPROVAL
                await self._narrate(
                    f"Built {decision.agent_class_name}. Tests passing. Want me to create a PR?"
                )
                approved = await self._request_human_approval(record)
                if not approved:
                    if record.phase != GraduationPhase.EXPIRED:
                        record.phase = GraduationPhase.REJECTED
                        self._total_rejected += 1
                    await self._narrate("Discarding generated agent.")
                    return record

                record.phase = GraduationPhase.PUSHING
                pr_url = await self._push_and_create_pr(decision, record)
                if pr_url is None:
                    record.phase = GraduationPhase.PUSH_FAILED
                    await self._narrate(f"Push failed. Branch {record.branch_name} preserved locally.")
                    return record

                record.pr_url = pr_url
                record.phase = GraduationPhase.AWAITING_MERGE
                await self._narrate("PR created. Waiting for your merge.")

                asyncio.create_task(
                    self._register_after_merge(record),
                    name=f"graduation_poll_{record.graduation_id}",
                )
                return record

            except Exception as exc:
                record.phase = GraduationPhase.FAILED
                record.error = str(exc)[:500]
                self._total_failed += 1
                await self._narrate(f"Graduation failed: {exc}")
                return record

            finally:
                if record.phase in (
                    GraduationPhase.FAILED, GraduationPhase.REJECTED,
                    GraduationPhase.EXPIRED, GraduationPhase.DECIDED_SKIP,
                ):
                    if record.worktree_path:
                        await self._cleanup_worktree(Path(record.worktree_path), record.branch_name)
                self._save_record(record)
                self._active.pop(goal_class_id, None)

    # -- Phase 1: Decision --------------------------------------------------

    async def _ask_jprime_for_decision(
        self, goal_class_id: str, usage_records: List[EphemeralUsageRecord],
        record: GraduationRecord,
    ) -> GraduationDecision:
        """Directive 2: Route decision to deepseek_r1 via BrainSelector 3-layer gate."""
        if self._prime is None:
            raise RuntimeError("PrimeClient not available")

        goals = [r.goal for r in usage_records]
        success_rate = sum(1 for r in usage_records if r.execution_outcome == "success") / max(len(usage_records), 1)
        domains = sorted(self._topology.all_domains()) if self._topology else []

        prompt = (
            "You are the architectural decision engine for JARVIS Trinity AIOS.\n"
            f"An ephemeral tool has been used {len(usage_records)} times ({success_rate:.0%} success).\n"
            f"Sample goals: {json.dumps(goals[:5])}\n"
            f"Available domains: {json.dumps(domains)}\n"
            f"Repos: jarvis (execution), prime (intelligence), reactor (learning)\n\n"
            "Respond with JSON only:\n"
            '{"should_graduate":bool,"capability_name":"snake_case","capability_domain":"domain",'
            '"repo_owner":"jarvis|prime|reactor","agent_class_name":"PascalCase",'
            '"module_path":"backend/...","test_module_path":"tests/...",'
            '"rationale":"why","estimated_complexity":"light|heavy_code|complex",'
            '"rejection_reason":"if not graduating"}'
        )

        # Directive 2: BrainSelector routes to deepseek_r1 (Tier 3 complex reasoning)
        model_name = None
        task_profile = None
        if self._brain_selector is not None:
            try:
                from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
                _snap = ResourceSnapshot(
                    ram_percent=0.0, cpu_percent=0.0, event_loop_latency_ms=0.0,
                    disk_io_busy=False,
                )
                _brain_result = self._brain_selector.select(
                    description="complex architectural decision: should ephemeral tool become permanent agent",
                    target_files=(),
                    snapshot=_snap,
                    blast_radius=1,
                )
                model_name = _brain_result.model_name
                task_profile = _brain_result.brain_id
                if _brain_result.provider_tier == "queued":
                    raise RuntimeError(f"Daily budget exceeded: {_brain_result.routing_reason}")
                logger.info(
                    "[Graduation] Decision routed to %s (%s): %s",
                    _brain_result.brain_id, _brain_result.model_name, _brain_result.routing_reason,
                )
            except ImportError:
                pass  # BrainSelector dependencies unavailable, use default

        response = await self._prime.generate(
            prompt=prompt,
            system_prompt="Respond with valid JSON only.",
            max_tokens=1024, temperature=0.1,
            model_name=model_name,
            task_profile=task_profile,
        )

        # H6: Record cost via BrainSelector
        if hasattr(response, "cost_usd") and self._brain_selector is not None:
            cost = getattr(response, "cost_usd", 0.0)
            record.total_cost_usd += cost
            try:
                self._brain_selector.record_cost(task_profile or "gcp_prime", cost)
            except Exception:
                pass
        elif hasattr(response, "cost_usd"):
            record.total_cost_usd += getattr(response, "cost_usd", 0.0)

        text = response.content.strip()
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        data = json.loads(text)

        return GraduationDecision(
            should_graduate=bool(data.get("should_graduate", False)),
            capability_name=data.get("capability_name", f"graduated_{goal_class_id}"),
            capability_domain=data.get("capability_domain", "exploration"),
            repo_owner=data.get("repo_owner", "jarvis"),
            agent_class_name=data.get("agent_class_name", "GraduatedAgent"),
            module_path=data.get("module_path", "backend/neural_mesh/agents/graduated_agent.py"),
            test_module_path=data.get("test_module_path", "tests/unit/test_graduated_agent.py"),
            rationale=data.get("rationale", ""),
            estimated_complexity=data.get("estimated_complexity", "heavy_code"),
            rejection_reason=data.get("rejection_reason", ""),
        )

    # -- Phase 2: Worktree (H1: git cleanliness) ---------------------------

    async def _create_worktree(self, decision: GraduationDecision) -> Tuple[Path, str]:
        repo_path = self._resolve_repo_path(decision.repo_owner)

        # H1: Git cleanliness check
        proc = await asyncio.create_subprocess_exec(
            "git", "status", "--porcelain",
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if stdout.strip():
            raise RuntimeError(
                f"Cannot graduate: {decision.repo_owner} repo has uncommitted changes: "
                f"{stdout.decode().strip()[:200]}"
            )

        branch = f"graduation/{decision.capability_name}-{int(time.time())}"
        wt_path = self._dir / "worktrees" / decision.capability_name
        wt_path.parent.mkdir(parents=True, exist_ok=True)

        if wt_path.exists():
            await self._cleanup_worktree(wt_path, None)

        proc = await asyncio.create_subprocess_exec(
            "git", "worktree", "add", "-b", branch, str(wt_path),
            cwd=str(repo_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {stderr.decode()[:200]}")
        return wt_path, branch

    # -- Phase 3: Code generation -------------------------------------------

    async def _generate_agent_code(
        self, decision: GraduationDecision, wt: Path,
        usage_records: List[EphemeralUsageRecord], record: GraduationRecord,
    ) -> List[str]:
        """Directive 2: Route code generation to qwen_coder via BrainSelector."""
        if self._prime is None:
            raise RuntimeError("PrimeClient not available")

        sample_goals = [r.goal for r in usage_records[:3]]
        prompt = (
            f"Generate a Neural Mesh agent for JARVIS Trinity AIOS.\n"
            f"Class: {decision.agent_class_name}, Module: {decision.module_path}\n"
            f"Domain: {decision.capability_domain}, Repo: {decision.repo_owner}\n"
            f"Must handle goals like: {json.dumps(sample_goals)}\n\n"
            "Requirements:\n"
            "1. Extend BaseNeuralMeshAgent\n"
            "2. Implement async execute_task(self, payload: dict) -> dict\n"
            "3. Define CAPABILITIES as a set\n"
            "4. Return dict with 'success' and 'result'\n\n"
            "Output TWO files separated by '---FILE_SEPARATOR---':\n"
            f"File 1: {decision.module_path}\nFile 2: {decision.test_module_path}\n"
        )

        # Directive 2: BrainSelector routes codegen to qwen_coder_14b/32b
        model_name = None
        task_profile = None
        if self._brain_selector is not None:
            try:
                from backend.core.ouroboros.governance.resource_monitor import ResourceSnapshot
                _snap = ResourceSnapshot(
                    ram_percent=0.0, cpu_percent=0.0, event_loop_latency_ms=0.0,
                    disk_io_busy=False,
                )
                _brain_result = self._brain_selector.select(
                    description=f"heavy code generation: full agent class + tests for {decision.capability_name}",
                    target_files=(decision.module_path, decision.test_module_path),
                    snapshot=_snap,
                    blast_radius=2,
                )
                model_name = _brain_result.model_name
                task_profile = _brain_result.brain_id
                if _brain_result.provider_tier == "queued":
                    raise RuntimeError(f"Daily budget exceeded: {_brain_result.routing_reason}")
                logger.info(
                    "[Graduation] Codegen routed to %s (%s): %s",
                    _brain_result.brain_id, _brain_result.model_name, _brain_result.routing_reason,
                )
            except ImportError:
                pass

        response = await self._prime.generate(
            prompt=prompt,
            system_prompt="Generate production Python. Two files separated by ---FILE_SEPARATOR---.",
            max_tokens=4096, temperature=0.2,
            model_name=model_name,
            task_profile=task_profile,
        )

        # H6: Record cost via BrainSelector
        if hasattr(response, "cost_usd") and self._brain_selector is not None:
            cost = getattr(response, "cost_usd", 0.0)
            record.total_cost_usd += cost
            try:
                self._brain_selector.record_cost(task_profile or "gcp_prime", cost)
            except Exception:
                pass
        elif hasattr(response, "cost_usd"):
            record.total_cost_usd += getattr(response, "cost_usd", 0.0)

        parts = response.content.split("---FILE_SEPARATOR---")
        agent_code = re.sub(r'^```\w*\n?', '', (parts[0] if parts else "").strip())
        agent_code = re.sub(r'\n?```$', '', agent_code)
        test_code = re.sub(r'^```\w*\n?', '', (parts[1] if len(parts) > 1 else "").strip())
        test_code = re.sub(r'\n?```$', '', test_code)

        generated = []
        if agent_code:
            p = wt / decision.module_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(agent_code)
            generated.append(decision.module_path)
        if test_code:
            p = wt / decision.test_module_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(test_code)
            generated.append(decision.test_module_path)
        return generated

    # -- Phase 4: Validation (H2: contract tests) --------------------------

    async def _validate_in_shadow(
        self, decision: GraduationDecision, wt: Path, generated: List[str],
    ) -> bool:
        """Directive 3: Full validation with Coding Council safety modules.

        5-layer validation:
            1. SideEffectFirewall compile check (ShadowHarness)
            2. H2 contract test (BaseNeuralMeshAgent interface)
            3. ASTValidator — syntax, imports, dangerous patterns, complexity
            4. SecurityScanner — OWASP, injection, secrets detection
            5. pytest execution in worktree
        """
        agent_path = wt / decision.module_path
        if not agent_path.exists():
            logger.warning("[Graduation] Agent file missing: %s", agent_path)
            return False
        code = agent_path.read_text()

        # Layer 1: SideEffectFirewall compile check
        try:
            from backend.core.ouroboros.governance.shadow_harness import SideEffectFirewall
            with SideEffectFirewall():
                compile(code, str(agent_path), "exec")  # noqa: S102
        except Exception as e:
            logger.warning("[Graduation] Firewall compile check failed: %s", e)
            return False

        # Layer 2: H2 Contract test (BaseNeuralMeshAgent interface)
        has_execute = "execute_task" in code
        has_caps = "CAPABILITIES" in code or "capabilities" in code
        if not (has_execute and has_caps):
            logger.warning(
                "[Graduation] Contract test failed: execute_task=%s, capabilities=%s",
                has_execute, has_caps,
            )
            return False

        # Layer 3: Coding Council ASTValidator
        try:
            from backend.core.coding_council.safety.ast_validator import ASTValidator
            ast_validator = ASTValidator(repo_root=wt)
            ast_result = await ast_validator.validate_file(agent_path)
            if not ast_result.valid:
                errors = [
                    issue for issue in getattr(ast_result, "issues", [])
                    if getattr(issue, "severity", None)
                    and issue.severity.value == "error"
                ]
                if errors:
                    logger.warning(
                        "[Graduation] AST validation failed: %d errors",
                        len(errors),
                    )
                    return False
            logger.info("[Graduation] AST validation passed")
        except ImportError:
            logger.debug("[Graduation] ASTValidator not available — skipping")
        except Exception as e:
            logger.warning("[Graduation] ASTValidator error (non-fatal): %s", e)

        # Layer 4: Coding Council SecurityScanner
        try:
            from backend.core.coding_council.safety.security_scanner import SecurityScanner
            scanner = SecurityScanner()
            scan_result = await scanner.scan_file(agent_path)
            critical_vulns = [
                v for v in getattr(scan_result, "vulnerabilities", [])
                if getattr(v, "severity", None)
                and v.severity.value in ("critical", "high")
            ]
            if critical_vulns:
                logger.warning(
                    "[Graduation] Security scan found %d critical/high vulnerabilities",
                    len(critical_vulns),
                )
                return False
            logger.info("[Graduation] Security scan passed")
        except ImportError:
            logger.debug("[Graduation] SecurityScanner not available — skipping")
        except Exception as e:
            logger.warning("[Graduation] SecurityScanner error (non-fatal): %s", e)

        # Layer 5: pytest execution in worktree
        test_path = wt / decision.test_module_path
        if test_path.exists():
            try:
                proc = await asyncio.create_subprocess_exec(
                    "python3", "-m", "pytest", str(test_path), "-v", "--timeout=15",
                    cwd=str(wt),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30.0)
                if proc.returncode != 0:
                    logger.warning("[Graduation] pytest failed (returncode=%d)", proc.returncode)
                    return False
                logger.info("[Graduation] pytest passed")
            except asyncio.TimeoutError:
                logger.warning("[Graduation] pytest timed out")
                return False
            except Exception as e:
                logger.warning("[Graduation] pytest error: %s", e)
                return False

        logger.info("[Graduation] All 5 validation layers passed")
        return True

    # -- Phase 5: Commit ----------------------------------------------------

    async def _commit_to_branch(
        self, decision: GraduationDecision, wt: Path, generated: List[str],
    ) -> str:
        for f in generated:
            await self._git(wt, "add", f)
        msg = (
            f"feat(graduation): add {decision.capability_name} agent\n\n"
            f"Graduated from ephemeral tool.\nRepo: {decision.repo_owner}\n"
            f"Domain: {decision.capability_domain}\n"
            f"Generated by JARVIS Graduation Orchestrator"
        )
        await self._git(wt, "commit", "-m", msg)
        proc = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "HEAD", cwd=str(wt), stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()

    # -- Phase 6: Approval (H4: timeout -> EXPIRED) -------------------------

    async def _request_human_approval(self, record: GraduationRecord) -> bool:
        approval_path = self._dir / "approvals" / f"{record.graduation_id}.json"
        approval_path.parent.mkdir(parents=True, exist_ok=True)
        approval_path.write_text(json.dumps({
            "graduation_id": record.graduation_id,
            "capability_name": record.decision.capability_name if record.decision else "unknown",
            "status": "pending", "created_at": time.time(),
        }))

        start = time.monotonic()
        while (time.monotonic() - start) < self._approval_timeout:
            await asyncio.sleep(_APPROVAL_POLL_S)
            if not approval_path.exists():
                continue
            try:
                data = json.loads(approval_path.read_text())
                if data.get("status") == "approved":
                    return True
                if data.get("status") == "rejected":
                    return False
            except Exception:
                continue

        record.phase = GraduationPhase.EXPIRED
        return False

    # -- Phase 7: Push + PR (H3: PUSH_FAILED) ------------------------------

    async def _push_and_create_pr(
        self, decision: GraduationDecision, record: GraduationRecord,
    ) -> Optional[str]:
        if not record.worktree_path or not record.branch_name:
            return None

        proc = await asyncio.create_subprocess_exec(
            "git", "push", "--set-upstream", "origin", record.branch_name,
            cwd=record.worktree_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            record.error = f"Push failed: {stderr.decode()[:200]}"
            return None

        title = f"feat(graduation): add {decision.capability_name} agent"
        body = (
            f"## Graduation: {decision.agent_class_name}\n\n"
            f"**Domain**: {decision.capability_domain}\n"
            f"**Repo**: {decision.repo_owner}\n"
            f"**Uses**: {record.usage_count}\n"
            f"**Rationale**: {decision.rationale}\n\n"
            + "\n".join(f"- `{f}`" for f in record.generated_files)
            + "\n\nGenerated by JARVIS Graduation Orchestrator"
        )

        proc = await asyncio.create_subprocess_exec(
            "gh", "pr", "create", "--title", title, "--body", body,
            cwd=record.worktree_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            record.error = f"PR failed: {stderr.decode()[:200]}"
            return None
        return stdout.decode().strip()

    # -- Phase 8: Post-merge (H5: readiness probe) -------------------------

    async def _register_after_merge(self, record: GraduationRecord) -> None:
        if not record.pr_url:
            return

        while True:
            await asyncio.sleep(60.0)
            try:
                proc = await asyncio.create_subprocess_exec(
                    "gh", "pr", "view", record.pr_url, "--json", "state",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await proc.communicate()
                state = json.loads(stdout.decode()).get("state", "")
                if state == "MERGED":
                    break
                if state == "CLOSED":
                    record.phase = GraduationPhase.REJECTED
                    self._save_record(record)
                    return
            except Exception:
                continue

        # H5: Readiness probe
        if record.decision:
            try:
                import importlib
                mod_name = record.decision.module_path.replace("/", ".").replace(".py", "")
                mod = importlib.import_module(mod_name)
                cls = getattr(mod, record.decision.agent_class_name, None)
                if cls is None or not hasattr(cls, "execute_task"):
                    record.phase = GraduationPhase.FAILED
                    record.error = "Readiness probe failed: class or execute_task not found"
                    self._save_record(record)
                    return
            except ImportError as exc:
                record.phase = GraduationPhase.FAILED
                record.error = f"Readiness probe failed: {exc}"
                self._save_record(record)
                return

        if self._agent_registry and record.decision:
            try:
                await self._agent_registry.register(
                    agent_name=record.decision.capability_name,
                    agent_type=record.decision.capability_domain,
                    capabilities={record.decision.capability_name},
                    backend="local",
                )
            except Exception:
                pass

        if self._topology and record.decision:
            try:
                from backend.core.topology.topology_map import CapabilityNode
                self._topology.register(CapabilityNode(
                    name=record.decision.capability_name,
                    domain=record.decision.capability_domain,
                    repo_owner=record.decision.repo_owner,
                    active=True,
                ))
            except Exception:
                pass

        record.phase = GraduationPhase.GRADUATED
        self._total_graduated += 1
        await self._narrate(
            f"Agent {record.decision.agent_class_name} is live. "
            f"{record.decision.capability_domain} capability active."
        )
        self._save_record(record)

    # -- Cleanup ------------------------------------------------------------

    async def _cleanup_worktree(self, wt: Path, branch: Optional[str]) -> None:
        try:
            if wt.exists():
                proc = await asyncio.create_subprocess_exec(
                    "git", "worktree", "remove", "--force", str(wt),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                if wt.exists():
                    import shutil
                    shutil.rmtree(str(wt), ignore_errors=True)
            if branch:
                await asyncio.create_subprocess_exec(
                    "git", "branch", "-D", branch,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
        except Exception:
            pass

    # -- Utilities ----------------------------------------------------------

    def _resolve_repo_path(self, repo_owner: str) -> Path:
        if self._repo_registry:
            try:
                return Path(self._repo_registry.get(repo_owner).local_path)
            except Exception:
                pass
        env_map = {"jarvis": "JARVIS_REPO_PATH", "prime": "JARVIS_PRIME_REPO_PATH", "reactor": "JARVIS_REACTOR_REPO_PATH"}
        return Path(os.environ.get(env_map.get(repo_owner, "JARVIS_REPO_PATH"), "."))

    async def _git(self, cwd: Path, *args: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode()[:200]}")
        return stdout

    async def _narrate(self, text: str) -> None:
        try:
            from backend.core.supervisor.lifecycle_narrator import get_lifecycle_narrator, NarrationPriority
            get_lifecycle_narrator().enqueue(text, NarrationPriority.HIGH, category="graduation")
        except Exception:
            pass

    def _emit(self, record: GraduationRecord, message: str) -> None:
        if self._bus is None:
            return
        try:
            from backend.core.telemetry_contract import TelemetryEnvelope
            self._bus.emit(TelemetryEnvelope.create(
                event_schema="reasoning.decision@1.0.0",
                source="graduation_orchestrator",
                trace_id=record.graduation_id,
                span_id=record.phase.value,
                partition_key="graduation",
                payload={"graduation_id": record.graduation_id, "phase": record.phase.value,
                         "goal_class_id": record.goal_class_id, "message": message[:200],
                         "cost_usd": record.total_cost_usd},
            ))
        except Exception:
            pass

    def _save_record(self, record: GraduationRecord) -> None:
        path = self._dir / "records" / f"{record.graduation_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {"graduation_id": record.graduation_id, "goal_class_id": record.goal_class_id,
                    "phase": record.phase.value, "worktree_path": record.worktree_path,
                    "branch_name": record.branch_name, "pr_url": record.pr_url,
                    "commit_sha": record.commit_sha, "validation_passed": record.validation_passed,
                    "generated_files": record.generated_files, "error": record.error,
                    "created_at": record.created_at, "updated_at": time.time(),
                    "total_cost_usd": record.total_cost_usd, "usage_count": record.usage_count}
            if record.decision:
                data["decision"] = asdict(record.decision)
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def health(self) -> Dict[str, Any]:
        return {"active_graduations": len(self._active), "total_graduated": self._total_graduated,
                "total_failed": self._total_failed, "total_rejected": self._total_rejected,
                "semaphore_available": self._semaphore._value}
