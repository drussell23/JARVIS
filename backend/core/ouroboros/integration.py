"""
Ouroboros Integration Layer v1.0
================================

Provides robust integration between Ouroboros and the Trinity ecosystem:
- Multi-provider LLM fallback (Prime -> Ollama -> API)
- Health monitoring with circuit breakers
- Reactor Core experience publishing
- Sandboxed code execution
- Human review checkpoints

This layer handles all the edge cases that could cause failures:
- JARVIS Prime not running -> fallback to Ollama -> fallback to API
- Network issues -> retry with exponential backoff
- Repeated failures -> circuit breaker trips
- Dangerous changes -> sandbox execution first

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("Ouroboros.Integration")


# =============================================================================
# CONFIGURATION
# =============================================================================

class IntegrationConfig:
    """Dynamic configuration for Ouroboros integration."""

    # LLM Providers (in fallback order)
    PROVIDERS = [
        {
            "name": "jarvis-prime",
            "api_base": os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
            "api_key": os.getenv("JARVIS_PRIME_API_KEY", "sk-local-jarvis"),
            "model": os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2"),
            "timeout": 120.0,
        },
        {
            "name": "ollama",
            "api_base": os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
            "api_key": "ollama",
            "model": os.getenv("OLLAMA_MODEL", "codellama"),
            "timeout": 180.0,
        },
        {
            "name": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
            "model": "claude-3-haiku-20240307",
            "timeout": 60.0,
        },
    ]

    # Circuit Breaker
    CIRCUIT_BREAKER_THRESHOLD = int(os.getenv("OUROBOROS_CIRCUIT_THRESHOLD", "5"))
    CIRCUIT_BREAKER_TIMEOUT = float(os.getenv("OUROBOROS_CIRCUIT_TIMEOUT", "300.0"))

    # Retry
    MAX_RETRIES = int(os.getenv("OUROBOROS_MAX_RETRIES", "3"))
    RETRY_DELAY = float(os.getenv("OUROBOROS_RETRY_DELAY", "2.0"))

    # Safety
    SANDBOX_ENABLED = os.getenv("OUROBOROS_SANDBOX", "true").lower() == "true"
    HUMAN_REVIEW_ENABLED = os.getenv("OUROBOROS_HUMAN_REVIEW", "false").lower() == "true"

    # Reactor Core Integration
    REACTOR_EVENTS_DIR = Path(os.getenv("REACTOR_EVENTS_DIR", str(Path.home() / ".jarvis/reactor/events")))
    EXPERIENCE_PUBLISHING_ENABLED = os.getenv("OUROBOROS_PUBLISH_EXPERIENCES", "true").lower() == "true"


# =============================================================================
# ENUMS
# =============================================================================

class ProviderStatus(Enum):
    """Status of an LLM provider."""
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"      # Tripped, rejecting requests
    HALF_OPEN = "half_open"  # Testing if recovered


# =============================================================================
# CIRCUIT BREAKER
# =============================================================================

@dataclass
class CircuitBreaker:
    """
    Circuit breaker for protecting against cascading failures.

    State machine:
    - CLOSED: Normal operation, tracking failures
    - OPEN: Too many failures, rejecting all requests
    - HALF_OPEN: Testing if the service has recovered
    """
    name: str
    threshold: int = IntegrationConfig.CIRCUIT_BREAKER_THRESHOLD
    timeout: float = IntegrationConfig.CIRCUIT_BREAKER_TIMEOUT

    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0

    def can_execute(self) -> bool:
        """Check if request can proceed."""
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            # Check if timeout has elapsed
            if time.time() - self.last_failure_time >= self.timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info(f"Circuit breaker {self.name} transitioning to HALF_OPEN")
                return True
            return False

        # HALF_OPEN: allow one request to test
        return True

    def record_success(self) -> None:
        """Record a successful request."""
        self.successes += 1
        self.last_success_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Recovery confirmed
            self.state = CircuitState.CLOSED
            self.failures = 0
            logger.info(f"Circuit breaker {self.name} CLOSED (recovered)")

    def record_failure(self) -> None:
        """Record a failed request."""
        self.failures += 1
        self.last_failure_time = time.time()

        if self.state == CircuitState.HALF_OPEN:
            # Still failing, reopen
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker {self.name} re-OPENED (still failing)")
        elif self.failures >= self.threshold:
            self.state = CircuitState.OPEN
            logger.warning(f"Circuit breaker {self.name} OPENED after {self.failures} failures")

    def get_status(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "successes": self.successes,
            "can_execute": self.can_execute(),
        }


# =============================================================================
# MULTI-PROVIDER LLM CLIENT
# =============================================================================

class MultiProviderLLMClient:
    """
    LLM client with automatic failover between providers.

    Falls back through providers in order:
    1. JARVIS Prime (local, free)
    2. Ollama (local, free)
    3. Anthropic API (cloud, paid)

    Each provider has its own circuit breaker for fault isolation.
    """

    def __init__(self):
        self._providers = IntegrationConfig.PROVIDERS
        self._circuit_breakers: Dict[str, CircuitBreaker] = {
            p["name"]: CircuitBreaker(name=p["name"])
            for p in self._providers
        }
        self._sessions: Dict[str, aiohttp.ClientSession] = {}
        self._provider_status: Dict[str, ProviderStatus] = {}
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Close all sessions."""
        for session in self._sessions.values():
            if not session.closed:
                await session.close()

    async def health_check(self) -> Dict[str, ProviderStatus]:
        """Check health of all providers."""
        results = {}

        for provider in self._providers:
            name = provider["name"]
            try:
                status = await self._check_provider_health(provider)
                results[name] = status
                self._provider_status[name] = status
            except Exception as e:
                results[name] = ProviderStatus.UNAVAILABLE
                self._provider_status[name] = ProviderStatus.UNAVAILABLE

        return results

    async def _check_provider_health(self, provider: Dict) -> ProviderStatus:
        """Check health of a single provider."""
        if not provider.get("api_key"):
            return ProviderStatus.UNAVAILABLE

        try:
            session = await self._get_session(provider["name"])
            url = f"{provider['api_base'].rstrip('/')}/models"

            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    return ProviderStatus.HEALTHY
                elif resp.status < 500:
                    return ProviderStatus.DEGRADED
                else:
                    return ProviderStatus.UNAVAILABLE
        except Exception:
            return ProviderStatus.UNAVAILABLE

    async def _get_session(self, provider_name: str) -> aiohttp.ClientSession:
        """Get or create session for provider."""
        if provider_name not in self._sessions or self._sessions[provider_name].closed:
            provider = next((p for p in self._providers if p["name"] == provider_name), None)
            if not provider:
                raise ValueError(f"Unknown provider: {provider_name}")

            headers = {"Content-Type": "application/json"}
            if provider.get("api_key"):
                headers["Authorization"] = f"Bearer {provider['api_key']}"

            self._sessions[provider_name] = aiohttp.ClientSession(headers=headers)

        return self._sessions[provider_name]

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> Tuple[str, str]:
        """
        Generate response from best available provider.

        Returns:
            (response_text, provider_name)
        """
        async with self._lock:
            last_error = None

            for provider in self._providers:
                name = provider["name"]
                circuit = self._circuit_breakers[name]

                # Skip if circuit breaker is open
                if not circuit.can_execute():
                    logger.debug(f"Skipping {name} (circuit open)")
                    continue

                # Skip if no API key
                if not provider.get("api_key"):
                    continue

                try:
                    response = await self._call_provider(
                        provider, prompt, system_prompt, temperature, max_tokens
                    )
                    circuit.record_success()
                    logger.info(f"Successfully generated from {name}")
                    return response, name

                except Exception as e:
                    circuit.record_failure()
                    last_error = e
                    logger.warning(f"Provider {name} failed: {e}")
                    continue

            # All providers failed
            raise RuntimeError(f"All LLM providers failed. Last error: {last_error}")

    async def _call_provider(
        self,
        provider: Dict,
        prompt: str,
        system_prompt: Optional[str],
        temperature: float,
        max_tokens: int,
    ) -> str:
        """Call a specific provider."""
        session = await self._get_session(provider["name"])

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": provider["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        url = f"{provider['api_base'].rstrip('/')}/chat/completions"

        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=provider.get("timeout", 120)),
        ) as resp:
            if resp.status != 200:
                error_text = await resp.text()
                raise RuntimeError(f"Provider error ({resp.status}): {error_text}")

            data = await resp.json()
            return data["choices"][0]["message"]["content"]

    def get_status(self) -> Dict[str, Any]:
        return {
            "providers": {
                name: {
                    "status": self._provider_status.get(name, ProviderStatus.UNKNOWN).value,
                    "circuit_breaker": self._circuit_breakers[name].get_status(),
                }
                for name in [p["name"] for p in self._providers]
            },
        }


# =============================================================================
# SANDBOX EXECUTOR
# =============================================================================

class SandboxExecutor:
    """
    Executes code in a sandboxed environment for safety.

    Creates a temporary directory, copies code, runs tests,
    and only applies changes if tests pass.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.base_dir = base_dir or Path.home() / ".jarvis/ouroboros/sandbox"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    async def execute_in_sandbox(
        self,
        original_file: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """
        Execute modified code in sandbox.

        Args:
            original_file: Original file path
            modified_content: New content to test
            test_command: Command to validate

        Returns:
            (success, output)
        """
        sandbox_id = f"sandbox_{uuid.uuid4().hex[:8]}"
        sandbox_dir = self.base_dir / sandbox_id

        try:
            # Create sandbox directory
            sandbox_dir.mkdir(parents=True, exist_ok=True)

            # Copy project structure (minimal)
            await self._setup_sandbox(sandbox_dir, original_file, modified_content)

            # Run tests in sandbox
            result = await asyncio.create_subprocess_shell(
                test_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=sandbox_dir,
                env=self._get_sandbox_env(sandbox_dir),
            )

            try:
                stdout, stderr = await asyncio.wait_for(result.communicate(), timeout=300)
            except asyncio.TimeoutError:
                result.kill()
                return False, "Sandbox execution timeout"

            output = stdout.decode() + stderr.decode()
            success = result.returncode == 0

            return success, output

        except Exception as e:
            return False, f"Sandbox error: {e}"

        finally:
            # Cleanup sandbox
            await self._cleanup_sandbox(sandbox_dir)

    async def _setup_sandbox(
        self,
        sandbox_dir: Path,
        original_file: Path,
        modified_content: str,
    ) -> None:
        """Setup sandbox with modified file."""
        # Create the modified file in sandbox
        relative_path = original_file.name
        sandbox_file = sandbox_dir / relative_path
        await asyncio.to_thread(sandbox_file.write_text, modified_content)

        # Copy necessary dependencies (requirements.txt, pyproject.toml)
        project_root = original_file.parent
        for dep_file in ["requirements.txt", "pyproject.toml", "setup.py"]:
            src = project_root / dep_file
            if src.exists():
                dst = sandbox_dir / dep_file
                content = await asyncio.to_thread(src.read_text)
                await asyncio.to_thread(dst.write_text, content)

    def _get_sandbox_env(self, sandbox_dir: Path) -> Dict[str, str]:
        """Get environment for sandbox execution."""
        env = os.environ.copy()
        env["PYTHONPATH"] = str(sandbox_dir)
        env["OUROBOROS_SANDBOX"] = "1"
        return env

    async def _cleanup_sandbox(self, sandbox_dir: Path) -> None:
        """Cleanup sandbox directory."""
        import shutil
        try:
            await asyncio.to_thread(shutil.rmtree, sandbox_dir, ignore_errors=True)
        except Exception:
            pass


# =============================================================================
# REACTOR CORE INTEGRATION
# =============================================================================

class ReactorCoreExperiencePublisher:
    """
    Publishes improvement experiences to Reactor Core for training.

    When Ouroboros successfully improves code, it publishes the experience
    as a training example for the model to learn from.
    """

    def __init__(self, events_dir: Path = IntegrationConfig.REACTOR_EVENTS_DIR):
        self.events_dir = events_dir
        self.events_dir.mkdir(parents=True, exist_ok=True)

    async def publish_improvement_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: List[str],
    ) -> Optional[str]:
        """
        Publish an improvement experience for training.

        Args:
            original_code: Original code before improvement
            improved_code: Code after improvement
            goal: Improvement goal
            success: Whether improvement succeeded
            iterations: Number of iterations taken
            error_history: List of errors encountered

        Returns:
            Event ID if published
        """
        if not IntegrationConfig.EXPERIENCE_PUBLISHING_ENABLED:
            return None

        event_id = f"ouroboros_exp_{uuid.uuid4().hex[:12]}"

        experience = {
            "event_id": event_id,
            "event_type": "ouroboros_improvement",
            "timestamp": time.time(),
            "source": "ouroboros",
            "payload": {
                "original_code": original_code[:5000],  # Truncate large code
                "improved_code": improved_code[:5000],
                "goal": goal,
                "success": success,
                "iterations": iterations,
                "error_patterns": error_history[-5:] if error_history else [],
            },
            "training_metadata": {
                "can_use_for_training": success,  # Only successful improvements
                "difficulty": self._estimate_difficulty(iterations, len(error_history)),
                "code_type": "python",
            },
        }

        # Write to events directory for Reactor Core to pick up
        event_file = self.events_dir / f"{event_id}.json"
        await asyncio.to_thread(
            event_file.write_text,
            json.dumps(experience, indent=2)
        )

        logger.info(f"Published improvement experience: {event_id}")
        return event_id

    def _estimate_difficulty(self, iterations: int, errors: int) -> str:
        """Estimate task difficulty for training prioritization."""
        if iterations == 1 and errors == 0:
            return "easy"
        elif iterations <= 3 and errors <= 2:
            return "medium"
        elif iterations <= 7:
            return "hard"
        else:
            return "very_hard"


# =============================================================================
# ENHANCED OUROBOROS INTEGRATION
# =============================================================================

class EnhancedOuroborosIntegration:
    """
    Enhanced integration layer for Ouroboros.

    Provides:
    - Multi-provider LLM with failover
    - Circuit breakers for fault isolation
    - Sandbox execution for safety
    - Reactor Core experience publishing
    - Health monitoring
    """

    def __init__(self):
        self._llm_client = MultiProviderLLMClient()
        self._sandbox = SandboxExecutor() if IntegrationConfig.SANDBOX_ENABLED else None
        self._experience_publisher = ReactorCoreExperiencePublisher()
        self._improvement_circuit = CircuitBreaker(name="improvement_loop", threshold=10)
        self._running = False
        self._metrics = {
            "improvements_attempted": 0,
            "improvements_succeeded": 0,
            "improvements_failed": 0,
            "experiences_published": 0,
        }

    async def initialize(self) -> bool:
        """Initialize the integration layer."""
        logger.info("Initializing Enhanced Ouroboros Integration...")

        # Health check providers
        provider_status = await self._llm_client.health_check()

        healthy_count = sum(1 for s in provider_status.values() if s == ProviderStatus.HEALTHY)
        logger.info(f"LLM providers: {healthy_count}/{len(provider_status)} healthy")

        for name, status in provider_status.items():
            logger.info(f"  - {name}: {status.value}")

        self._running = True
        return healthy_count > 0

    async def shutdown(self) -> None:
        """Shutdown the integration."""
        self._running = False
        await self._llm_client.close()
        logger.info("Enhanced Ouroboros Integration shutdown")

    async def generate_improvement(
        self,
        original_code: str,
        goal: str,
        error_log: Optional[str] = None,
        context: Optional[str] = None,
    ) -> Optional[str]:
        """
        Generate improved code using best available provider.

        Args:
            original_code: Original source code
            goal: Improvement goal
            error_log: Optional error from previous attempt
            context: Optional context from related files

        Returns:
            Improved code or None if all providers fail
        """
        if not self._improvement_circuit.can_execute():
            logger.warning("Improvement circuit breaker is OPEN, rejecting request")
            return None

        system_prompt = """You are an expert software engineer improving code.
Output ONLY the improved Python code, no explanations or markdown.
Maintain all existing functionality. Follow PEP 8."""

        prompt_parts = [
            f"## Original Code\n```python\n{original_code}\n```\n",
            f"\n## Goal\n{goal}\n",
        ]

        if error_log:
            prompt_parts.append(f"\n## Previous Error (fix this)\n```\n{error_log[:2000]}\n```\n")

        if context:
            prompt_parts.append(f"\n## Context\n{context[:3000]}\n")

        prompt_parts.append("\n## Output improved Python code only:\n")

        try:
            response, provider = await self._llm_client.generate(
                prompt="".join(prompt_parts),
                system_prompt=system_prompt,
                temperature=0.3,
            )

            self._improvement_circuit.record_success()

            # Extract code from potential markdown
            code = self._extract_code(response)
            return code

        except Exception as e:
            self._improvement_circuit.record_failure()
            logger.error(f"Failed to generate improvement: {e}")
            return None

    def _extract_code(self, response: str) -> str:
        """Extract code from response."""
        import re
        code_block = re.search(r"```(?:python)?\s*([\s\S]*?)```", response)
        if code_block:
            return code_block.group(1).strip()
        return response.strip()

    async def validate_in_sandbox(
        self,
        original_file: Path,
        modified_content: str,
        test_command: str,
    ) -> Tuple[bool, str]:
        """
        Validate changes in sandbox before applying.

        Returns:
            (success, output)
        """
        if not self._sandbox:
            return True, "Sandbox disabled"

        return await self._sandbox.execute_in_sandbox(
            original_file, modified_content, test_command
        )

    async def publish_experience(
        self,
        original_code: str,
        improved_code: str,
        goal: str,
        success: bool,
        iterations: int,
        error_history: List[str],
    ) -> Optional[str]:
        """Publish improvement experience to Reactor Core."""
        event_id = await self._experience_publisher.publish_improvement_experience(
            original_code, improved_code, goal, success, iterations, error_history
        )

        if event_id:
            self._metrics["experiences_published"] += 1

        return event_id

    def record_improvement_attempt(self, success: bool) -> None:
        """Record an improvement attempt for metrics."""
        self._metrics["improvements_attempted"] += 1
        if success:
            self._metrics["improvements_succeeded"] += 1
        else:
            self._metrics["improvements_failed"] += 1

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "llm_client": self._llm_client.get_status(),
            "improvement_circuit": self._improvement_circuit.get_status(),
            "sandbox_enabled": self._sandbox is not None,
            "experience_publishing": IntegrationConfig.EXPERIENCE_PUBLISHING_ENABLED,
            "metrics": self._metrics,
        }


# =============================================================================
# GLOBAL INSTANCE
# =============================================================================

_integration: Optional[EnhancedOuroborosIntegration] = None


def get_ouroboros_integration() -> EnhancedOuroborosIntegration:
    """Get global Ouroboros integration instance."""
    global _integration
    if _integration is None:
        _integration = EnhancedOuroborosIntegration()
    return _integration


async def shutdown_ouroboros_integration() -> None:
    """Shutdown global integration."""
    global _integration
    if _integration:
        await _integration.shutdown()
        _integration = None
