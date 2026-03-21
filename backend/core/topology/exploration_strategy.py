"""ExplorationStrategy — multi-phase capability research using existing Trinity infrastructure.

Reuses WebTool, PrimeClient, ShadowHarness, TestRunner, CommProtocol, and RepoRegistry.
No duplicate implementations. All existing tools are injected, not imported statically.

Phases:
    1. RESEARCH  — parallel web doc fetch + codebase analysis + dependency graph
    2. SYNTHESIZE — J-Prime code generation with full research context
    3. VALIDATE   — ShadowHarness firewall + pytest in scratch dir
    4. PACKAGE    — collect results into ExplorationResult for ArchitecturalProposal
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all from env, no hardcoding
# ---------------------------------------------------------------------------

_DEFAULT_RESEARCH_DOMAINS = frozenset({
    "docs.anthropic.com", "ollama.ai", "huggingface.co",
    "pypi.org", "github.com", "arxiv.org", "docs.python.org",
    "stackoverflow.com", "developer.mozilla.org",
})

_DOMAIN_SEARCH_SEEDS: Dict[str, List[str]] = {
    "llm_routing": ["LLM inference API", "model serving endpoint", "token streaming"],
    "voice": ["speech recognition python", "TTS synthesis", "voice activity detection"],
    "vision": ["screen capture python", "OCR extraction", "image analysis API"],
    "governance": ["code generation validation", "shadow testing", "AST comparison"],
    "infrastructure": ["GCP compute engine API", "cloud SQL proxy", "container orchestration"],
    "neural_mesh": ["multi-agent coordination", "agent scheduling", "task delegation"],
    "data_io": ["file parsing python", "data serialization", "streaming data"],
    "exploration": ["sandboxed code execution", "capability discovery", "automated testing"],
}


@dataclass
class ExplorationConfig:
    max_web_fetches: int = 5
    max_codebase_files: int = 10
    web_fetch_timeout_s: float = 15.0
    synthesis_max_tokens: int = 8192
    synthesis_temperature: float = 0.3
    test_timeout_s: float = 120.0
    research_domains: frozenset = field(default_factory=lambda: _DEFAULT_RESEARCH_DOMAINS)

    @classmethod
    def from_env(cls) -> ExplorationConfig:
        return cls(
            max_web_fetches=int(os.environ.get("JARVIS_EXPLORE_MAX_WEB_FETCHES", "5")),
            max_codebase_files=int(os.environ.get("JARVIS_EXPLORE_MAX_CODEBASE_FILES", "10")),
            web_fetch_timeout_s=float(os.environ.get("JARVIS_EXPLORE_WEB_TIMEOUT", "15.0")),
            synthesis_max_tokens=int(os.environ.get("JARVIS_EXPLORE_MAX_TOKENS", "8192")),
            synthesis_temperature=float(os.environ.get("JARVIS_EXPLORE_TEMPERATURE", "0.3")),
            test_timeout_s=float(os.environ.get("JARVIS_EXPLORE_TEST_TIMEOUT", "120.0")),
        )


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResearchFindings:
    web_docs: Dict[str, str]
    codebase_context: Dict[str, str]
    dependency_analysis: str
    search_queries_used: List[str]
    errors: List[str]


@dataclass(frozen=True)
class SynthesisResult:
    generated_files: Dict[str, str]
    test_files: Dict[str, str]
    explanation: str
    schema_version: str
    provider_used: str
    prompt_tokens: int
    response_tokens: int


@dataclass(frozen=True)
class ValidationResult:
    shadow_passed: bool
    shadow_errors: List[str]
    test_passed: bool
    test_total: int
    test_failed: int
    test_output: str
    test_duration_s: float


@dataclass(frozen=True)
class ExplorationResult:
    research: ResearchFindings
    synthesis: Optional[SynthesisResult]
    validation: Optional[ValidationResult]
    success: bool
    failure_reason: Optional[str]
    elapsed_seconds: float
    phases_completed: List[str]


# ---------------------------------------------------------------------------
# ExplorationStrategy — the multi-phase engine
# ---------------------------------------------------------------------------

class ExplorationStrategy:
    """Multi-phase capability research using existing Trinity infrastructure.

    Injected dependencies (no static imports of heavy modules):
        web_tool:       WebTool instance (fetch docs, search web)
        prime_client:   PrimeClient instance (code synthesis via J-Prime)
        repo_registry:  RepoRegistry instance (cross-repo file access)
        comm_protocol:  CommProtocol instance (lifecycle events)

    All dependencies are Optional — the strategy degrades gracefully:
        No web_tool     -> skip web research, use codebase-only context
        No prime_client -> BLOCKED (can't synthesize without a brain)
        No repo_registry -> use local repo only
        No comm_protocol -> skip lifecycle events (still works)
    """

    def __init__(
        self,
        config: ExplorationConfig,
        scratch_path: str,
        web_tool: Any = None,
        prime_client: Any = None,
        repo_registry: Any = None,
        comm_protocol: Any = None,
    ) -> None:
        self._config = config
        self._scratch = Path(scratch_path)
        self._web = web_tool
        self._prime = prime_client
        self._registry = repo_registry
        self._comm = comm_protocol

    async def run(
        self,
        target: Any,
        hardware: Any,
        semaphore: asyncio.Semaphore,
    ) -> ExplorationResult:
        """Run the full 4-phase exploration pipeline."""
        start = time.monotonic()
        phases_completed: List[str] = []
        op_id = f"explore-{target.capability.name}-{int(time.time())}"

        await self._emit_intent(op_id, target)

        # --- Phase 1: RESEARCH (parallel) ---
        await self._emit_heartbeat(op_id, "RESEARCH", 0.0)
        try:
            research = await self._phase_research(target, semaphore)
            phases_completed.append("RESEARCH")
            await self._emit_heartbeat(op_id, "RESEARCH", 1.0)
        except Exception as exc:
            logger.warning("[ExplorationStrategy] Research failed: %s", exc)
            await self._emit_decision(op_id, "blocked", f"research_failed:{exc}")
            return ExplorationResult(
                research=ResearchFindings({}, {}, "", [], [str(exc)]),
                synthesis=None, validation=None, success=False,
                failure_reason=f"Research phase failed: {exc}",
                elapsed_seconds=time.monotonic() - start,
                phases_completed=phases_completed,
            )

        # --- Phase 2: SYNTHESIZE (needs prime_client) ---
        if self._prime is None:
            await self._emit_decision(op_id, "blocked", "no_prime_client")
            return ExplorationResult(
                research=research, synthesis=None, validation=None,
                success=False, failure_reason="No PrimeClient available",
                elapsed_seconds=time.monotonic() - start,
                phases_completed=phases_completed,
            )

        await self._emit_heartbeat(op_id, "SYNTHESIZE", 0.0)
        try:
            synthesis = await self._phase_synthesize(target, hardware, research)
            phases_completed.append("SYNTHESIZE")
            await self._emit_heartbeat(op_id, "SYNTHESIZE", 1.0)
        except Exception as exc:
            logger.warning("[ExplorationStrategy] Synthesis failed: %s", exc)
            await self._emit_decision(op_id, "blocked", f"synthesis_failed:{exc}")
            return ExplorationResult(
                research=research, synthesis=None, validation=None,
                success=False, failure_reason=f"Synthesis phase failed: {exc}",
                elapsed_seconds=time.monotonic() - start,
                phases_completed=phases_completed,
            )

        if not synthesis.generated_files:
            await self._emit_decision(op_id, "blocked", "no_files_generated")
            return ExplorationResult(
                research=research, synthesis=synthesis, validation=None,
                success=False, failure_reason="J-Prime generated no files",
                elapsed_seconds=time.monotonic() - start,
                phases_completed=phases_completed,
            )

        # --- Phase 3: VALIDATE ---
        await self._emit_heartbeat(op_id, "VALIDATE", 0.0)
        try:
            validation = await self._phase_validate(synthesis)
            phases_completed.append("VALIDATE")
            await self._emit_heartbeat(op_id, "VALIDATE", 1.0)
        except Exception as exc:
            logger.warning("[ExplorationStrategy] Validation failed: %s", exc)
            await self._emit_decision(op_id, "blocked", f"validation_failed:{exc}")
            return ExplorationResult(
                research=research, synthesis=synthesis, validation=None,
                success=False, failure_reason=f"Validation phase failed: {exc}",
                elapsed_seconds=time.monotonic() - start,
                phases_completed=phases_completed,
            )

        # --- Phase 4: PACKAGE ---
        phases_completed.append("PACKAGE")
        success = validation.shadow_passed and validation.test_passed
        outcome = "success" if success else "partial"
        await self._emit_decision(op_id, outcome, f"tests={'passed' if success else 'failed'}")
        await self._emit_postmortem(op_id, target, success, phases_completed)

        return ExplorationResult(
            research=research, synthesis=synthesis, validation=validation,
            success=success,
            failure_reason=None if success else "Tests did not pass",
            elapsed_seconds=time.monotonic() - start,
            phases_completed=phases_completed,
        )

    # -----------------------------------------------------------------------
    # Phase 1: RESEARCH — parallel web fetch + codebase analysis
    # -----------------------------------------------------------------------

    async def _phase_research(
        self, target: Any, semaphore: asyncio.Semaphore,
    ) -> ResearchFindings:
        cap = target.capability
        queries = self._build_search_queries(cap)

        web_task = asyncio.create_task(self._fetch_web_docs(cap, queries, semaphore))
        code_task = asyncio.create_task(self._search_codebase(cap))
        dep_task = asyncio.create_task(self._analyze_dependencies(cap))

        web_docs, web_errors = await web_task
        codebase_ctx, code_errors = await code_task
        dep_analysis = await dep_task

        return ResearchFindings(
            web_docs=web_docs,
            codebase_context=codebase_ctx,
            dependency_analysis=dep_analysis,
            search_queries_used=queries,
            errors=web_errors + code_errors,
        )

    def _build_search_queries(self, cap: Any) -> List[str]:
        queries = [f"python {cap.name.replace('_', ' ')} implementation"]
        domain_seeds = _DOMAIN_SEARCH_SEEDS.get(cap.domain, [])
        queries.extend(domain_seeds[:2])
        queries.append(f"{cap.domain} {cap.name} library")
        return queries[:self._config.max_web_fetches]

    async def _fetch_web_docs(
        self, cap: Any, queries: List[str], semaphore: asyncio.Semaphore,
    ) -> Tuple[Dict[str, str], List[str]]:
        if self._web is None:
            return {}, ["WebTool not available"]

        docs: Dict[str, str] = {}
        errors: List[str] = []

        async def _fetch_one(query: str) -> None:
            async with semaphore:
                try:
                    result = await self._web.search(query, max_results=3)
                    if result.error:
                        errors.append(f"Search '{query}': {result.error}")
                        return
                    for item in result.results[:2]:
                        url = item.get("url", "")
                        if any(d in url for d in self._config.research_domains):
                            try:
                                page = await self._web.fetch(url)
                                if page.content and not page.error:
                                    docs[url] = page.content[:8000]
                            except Exception as e:
                                errors.append(f"Fetch {url}: {e}")
                except Exception as e:
                    errors.append(f"Search '{query}': {e}")

        await asyncio.gather(*[_fetch_one(q) for q in queries], return_exceptions=True)
        return docs, errors

    async def _search_codebase(self, cap: Any) -> Tuple[Dict[str, str], List[str]]:
        context: Dict[str, str] = {}
        errors: List[str] = []

        if self._registry is None:
            return context, ["RepoRegistry not available"]

        try:
            for repo_config in self._registry.list_enabled():
                repo_path = Path(repo_config.local_path)
                if not repo_path.exists():
                    continue
                search_terms = [cap.name, cap.domain]
                found = await self._grep_repo(repo_path, search_terms)
                for fpath in found[:self._config.max_codebase_files]:
                    try:
                        content = Path(fpath).read_text(errors="replace")
                        context[str(fpath)] = content[:4000]
                    except Exception as e:
                        errors.append(f"Read {fpath}: {e}")
        except Exception as e:
            errors.append(f"Codebase search: {e}")

        return context, errors

    async def _grep_repo(self, repo_path: Path, terms: List[str]) -> List[str]:
        found: Set[str] = set()
        for term in terms:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "grep", "-rl", "--include=*.py", term, str(repo_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
                for line in stdout.decode().strip().splitlines():
                    if line:
                        found.add(line)
            except (asyncio.TimeoutError, FileNotFoundError):
                pass
        return sorted(found)[:self._config.max_codebase_files]

    async def _analyze_dependencies(self, cap: Any) -> str:
        lines = [f"Capability: {cap.name} (domain: {cap.domain}, repo: {cap.repo_owner})"]
        lines.append(f"Active: {cap.active}")
        lines.append(f"Exploration attempts: {cap.exploration_attempts}")
        return "\n".join(lines)

    # -----------------------------------------------------------------------
    # Phase 2: SYNTHESIZE — J-Prime code generation
    # -----------------------------------------------------------------------

    async def _phase_synthesize(
        self, target: Any, hardware: Any, research: ResearchFindings,
    ) -> SynthesisResult:
        prompt = self._build_synthesis_prompt(target, hardware, research)
        system_prompt = self._build_system_prompt(target)

        response = await self._prime.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=self._config.synthesis_max_tokens,
            temperature=self._config.synthesis_temperature,
            model_name=None,
            task_profile=None,
        )

        generated, tests, explanation, schema = self._parse_synthesis_response(
            response.content, target.capability,
        )

        return SynthesisResult(
            generated_files=generated, test_files=tests,
            explanation=explanation, schema_version=schema,
            provider_used=response.source,
            prompt_tokens=0, response_tokens=response.tokens_used,
        )

    def _build_system_prompt(self, target: Any) -> str:
        return (
            "You are a capability researcher for the JARVIS Trinity ecosystem. "
            "Generate a Python implementation for a missing capability. "
            "Output MUST be valid JSON with this schema:\n"
            "{\n"
            '  "schema_version": "exploration-1.0",\n'
            '  "files": {"filename.py": "content..."},\n'
            '  "tests": {"test_filename.py": "content..."},\n'
            '  "explanation": "Why this implementation works..."\n'
            "}\n\n"
            "Rules:\n"
            "- Production-quality Python 3 with type hints and docstrings\n"
            "- Comprehensive pytest tests\n"
            "- No hardcoded paths, URLs, or credentials\n"
            "- Use async/await where appropriate\n"
            "- Handle errors with specific exception types\n"
            f"- Target repo: {target.capability.repo_owner}\n"
            f"- Target domain: {target.capability.domain}\n"
        )

    def _build_synthesis_prompt(
        self, target: Any, hardware: Any, research: ResearchFindings,
    ) -> str:
        sections = []
        sections.append(f"## Capability to Implement\n\n{target.rationale}")
        sections.append(f"Name: {target.capability.name}")
        sections.append(f"Domain: {target.capability.domain}")
        sections.append(f"Repo: {target.capability.repo_owner}")

        sections.append(f"\n## Hardware Context")
        sections.append(f"OS: {hardware.os_family}, CPU: {hardware.cpu_logical_cores} cores, "
                        f"RAM: {hardware.ram_total_mb}MB, Tier: {hardware.compute_tier.value}")
        if hardware.gpu:
            sections.append(f"GPU: {hardware.gpu.name} ({hardware.gpu.vram_free_mb}MB free)")

        if research.web_docs:
            sections.append(f"\n## Reference Documentation ({len(research.web_docs)} sources)")
            for url, content in list(research.web_docs.items())[:3]:
                sections.append(f"### {url}\n{content[:2000]}")

        if research.codebase_context:
            sections.append(f"\n## Related Existing Code ({len(research.codebase_context)} files)")
            for fpath, content in list(research.codebase_context.items())[:5]:
                sections.append(f"### {fpath}\n```python\n{content[:1500]}\n```")

        if research.dependency_analysis:
            sections.append(f"\n## Dependencies\n{research.dependency_analysis}")

        sections.append(
            "\n## Instructions\n"
            "Generate the implementation as JSON with files, tests, and explanation."
        )
        return "\n".join(sections)

    def _parse_synthesis_response(
        self, content: str, capability: Any,
    ) -> Tuple[Dict[str, str], Dict[str, str], str, str]:
        try:
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                data = json.loads(content[json_start:json_end])
                return (
                    data.get("files", {}),
                    data.get("tests", {}),
                    data.get("explanation", ""),
                    data.get("schema_version", "exploration-1.0"),
                )
        except json.JSONDecodeError:
            pass
        files, tests = self._extract_code_blocks(content, capability)
        return files, tests, content[:500], "raw-markdown"

    def _extract_code_blocks(
        self, content: str, capability: Any,
    ) -> Tuple[Dict[str, str], Dict[str, str]]:
        blocks = re.findall(r"```python\s*\n(.*?)```", content, re.DOTALL)
        files: Dict[str, str] = {}
        tests: Dict[str, str] = {}
        for i, block in enumerate(blocks):
            block = block.strip()
            if not block:
                continue
            is_test = any(kw in block for kw in ("def test_", "import pytest", "class Test"))
            if is_test:
                tests[f"test_{capability.name}_{i}.py"] = block
            else:
                name = f"{capability.name}.py" if i == 0 else f"{capability.name}_{i}.py"
                files[name] = block
        return files, tests

    # -----------------------------------------------------------------------
    # Phase 3: VALIDATE — ShadowHarness + pytest
    # -----------------------------------------------------------------------

    async def _phase_validate(self, synthesis: SynthesisResult) -> ValidationResult:
        self._scratch.mkdir(parents=True, exist_ok=True)

        all_files = {**synthesis.generated_files, **synthesis.test_files}
        for filename, content in all_files.items():
            fpath = self._scratch / filename
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content)

        # Shadow: compile check with SideEffectFirewall
        shadow_passed = True
        shadow_errors: List[str] = []
        try:
            from backend.core.ouroboros.governance.shadow_harness import SideEffectFirewall
            for filename, content in synthesis.generated_files.items():
                try:
                    with SideEffectFirewall():
                        compile(content, filename, "exec")  # noqa: S102 — compile() not exec()
                except SyntaxError as e:
                    shadow_errors.append(f"{filename}: SyntaxError: {e}")
                    shadow_passed = False
                except Exception as e:
                    shadow_errors.append(f"{filename}: {type(e).__name__}: {e}")
                    shadow_passed = False
        except ImportError:
            shadow_errors.append("ShadowHarness not available")

        # Pytest
        test_passed, test_total, test_failed, test_output, test_duration = (
            await self._run_pytest() if synthesis.test_files else (True, 0, 0, "no tests", 0.0)
        )

        return ValidationResult(
            shadow_passed=shadow_passed, shadow_errors=shadow_errors,
            test_passed=test_passed, test_total=test_total, test_failed=test_failed,
            test_output=test_output, test_duration_s=test_duration,
        )

    async def _run_pytest(self) -> Tuple[bool, int, int, str, float]:
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                "python3", "-m", "pytest", str(self._scratch),
                "-v", "--tb=short", "--no-header", "-q",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self._scratch),
            )
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.test_timeout_s,
            )
            output = stdout_bytes.decode(errors="replace")
            duration = time.monotonic() - start
            total, failed = self._parse_pytest_summary(output)
            return proc.returncode == 0, total, failed, output, duration
        except asyncio.TimeoutError:
            return False, 0, 0, f"pytest timed out after {self._config.test_timeout_s}s", time.monotonic() - start
        except Exception as e:
            return False, 0, 0, f"pytest error: {e}", time.monotonic() - start

    @staticmethod
    def _parse_pytest_summary(output: str) -> Tuple[int, int]:
        total = 0
        failed = 0
        m_passed = re.search(r"(\d+) passed", output)
        m_failed = re.search(r"(\d+) failed", output)
        if m_passed:
            total += int(m_passed.group(1))
        if m_failed:
            failed = int(m_failed.group(1))
            total += failed
        return total, failed

    # -----------------------------------------------------------------------
    # CommProtocol lifecycle events
    # -----------------------------------------------------------------------

    async def _emit_intent(self, op_id: str, target: Any) -> None:
        if self._comm is None:
            return
        try:
            await self._comm.emit_intent(
                op_id=op_id,
                goal=f"Explore capability: {target.capability.name} ({target.capability.domain})",
                target_files=[], risk_tier="exploration", blast_radius=0,
            )
        except Exception as e:
            logger.debug("[ExplorationStrategy] emit_intent error: %s", e)

    async def _emit_heartbeat(self, op_id: str, phase: str, progress: float) -> None:
        if self._comm is None:
            return
        try:
            await self._comm.emit_heartbeat(op_id=op_id, phase=phase, progress_pct=progress)
        except Exception as e:
            logger.debug("[ExplorationStrategy] emit_heartbeat error: %s", e)

    async def _emit_decision(self, op_id: str, outcome: str, reason: str) -> None:
        if self._comm is None:
            return
        try:
            await self._comm.emit_decision(op_id=op_id, outcome=outcome, reason_code=reason, diff_summary=None)
        except Exception as e:
            logger.debug("[ExplorationStrategy] emit_decision error: %s", e)

    async def _emit_postmortem(self, op_id: str, target: Any, success: bool, phases: List[str]) -> None:
        if self._comm is None:
            return
        try:
            await self._comm.emit_postmortem(
                op_id=op_id,
                root_cause="exploration_complete" if success else "exploration_incomplete",
                failed_phase=None if success else (phases[-1] if phases else "RESEARCH"),
                next_safe_action="review_proposal" if success else "retry_or_skip",
            )
        except Exception as e:
            logger.debug("[ExplorationStrategy] emit_postmortem error: %s", e)
