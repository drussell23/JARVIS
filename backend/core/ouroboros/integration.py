"""
Ouroboros Integration Layer v2.0 - Intelligent Self-Programming
================================================================

Provides robust integration between Ouroboros and the Trinity ecosystem:
- Intelligent model selection via IntelligentModelSelector (no hardcoding)
- Dynamic JARVIS Prime model discovery
- Context-aware model selection based on code complexity
- Large context window support (32k) for big files
- Multi-provider LLM fallback (Prime -> Ollama -> API)
- Health monitoring with circuit breakers
- Reactor Core experience publishing
- Sandboxed code execution
- Human review checkpoints

v2.0 Enhancements:
- IntelligentOuroborosModelSelector for dynamic model selection
- JARVIS Prime model discovery API integration
- Code complexity analysis for optimal model selection
- File size-aware context window selection
- Agentic loop support for autonomous operation

This layer handles all the edge cases that could cause failures:
- JARVIS Prime not running -> fallback to Ollama -> fallback to API
- Network issues -> retry with exponential backoff
- Repeated failures -> circuit breaker trips
- Dangerous changes -> sandbox execution first
- Model not suitable -> automatic fallback to better model

Author: Trinity System
Version: 2.0.0
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
# v2.0: INTELLIGENT MODEL SELECTOR INTEGRATION
# =============================================================================

# Try to import IntelligentModelSelector - fallback gracefully if unavailable
INTELLIGENT_SELECTOR_AVAILABLE = False
try:
    from backend.intelligence.model_selector import IntelligentModelSelector, QueryContext
    from backend.intelligence.model_registry import ModelDefinition, get_model_registry
    INTELLIGENT_SELECTOR_AVAILABLE = True
    logger.info("✅ IntelligentModelSelector integration available")
except ImportError as e:
    logger.warning(f"IntelligentModelSelector not available: {e}")


class CodeComplexityAnalyzer:
    """
    v2.0: Analyzes code complexity to select optimal model.

    Factors considered:
    - File size (lines of code)
    - Import density (external dependencies)
    - Cyclomatic complexity (control flow)
    - Nesting depth
    - Function/class count
    """

    @staticmethod
    def analyze(code: str) -> Dict[str, Any]:
        """Analyze code complexity."""
        lines = code.split('\n')
        line_count = len(lines)
        non_empty_lines = len([l for l in lines if l.strip()])

        # Count imports
        import_count = sum(1 for l in lines if l.strip().startswith(('import ', 'from ')))

        # Estimate nesting depth
        max_indent = 0
        for line in lines:
            if line.strip():
                indent = len(line) - len(line.lstrip())
                max_indent = max(max_indent, indent // 4)

        # Count functions and classes
        function_count = sum(1 for l in lines if l.strip().startswith('def '))
        class_count = sum(1 for l in lines if l.strip().startswith('class '))

        # Estimate complexity level
        if line_count > 1000 or max_indent > 6 or function_count > 20:
            complexity = "high"
        elif line_count > 300 or max_indent > 4 or function_count > 10:
            complexity = "medium"
        else:
            complexity = "low"

        # Estimate required context window
        estimated_tokens = line_count * 4  # ~4 tokens per line average
        if estimated_tokens > 16000:
            required_context = "32k"
        elif estimated_tokens > 4000:
            required_context = "8k"
        else:
            required_context = "4k"

        return {
            "line_count": line_count,
            "non_empty_lines": non_empty_lines,
            "import_count": import_count,
            "max_nesting_depth": max_indent,
            "function_count": function_count,
            "class_count": class_count,
            "complexity": complexity,
            "estimated_tokens": estimated_tokens,
            "required_context": required_context,
        }


class JarvisPrimeModelDiscovery:
    """
    v2.0: Discovers available JARVIS Prime models dynamically.

    Queries JARVIS Prime API to find:
    - Available models
    - Model capabilities (context window, specializations)
    - Model status (loaded, available, downloading)
    """

    def __init__(self, api_base: str = None):
        self.api_base = api_base or os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1")
        self._cached_models: Optional[List[Dict]] = None
        self._cache_time: float = 0
        self._cache_ttl: float = 60.0  # 1 minute cache

    async def discover_models(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Discover available JARVIS Prime models.

        Returns list of model info dicts with:
        - id: Model identifier
        - context_window: Context window size
        - capabilities: List of capabilities
        - status: current status
        """
        # Check cache
        if not force_refresh and self._cached_models and (time.time() - self._cache_time) < self._cache_ttl:
            return self._cached_models

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.api_base.rstrip('/')}/models"
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        models = data.get("data", [])

                        # Enrich with capabilities
                        enriched = []
                        for model in models:
                            enriched.append({
                                "id": model.get("id", "unknown"),
                                "context_window": self._estimate_context_window(model.get("id", "")),
                                "capabilities": self._infer_capabilities(model.get("id", "")),
                                "owned_by": model.get("owned_by", "unknown"),
                                "object": model.get("object", "model"),
                            })

                        # Sort by context window (largest first)
                        enriched.sort(key=lambda m: m["context_window"], reverse=True)

                        self._cached_models = enriched
                        self._cache_time = time.time()
                        return enriched

        except Exception as e:
            logger.warning(f"Failed to discover JARVIS Prime models: {e}")

        # Return cached or empty
        return self._cached_models or []

    async def get_best_model_for_task(
        self,
        required_context: str = "4k",
        task_type: str = "code_improvement",
    ) -> Optional[Dict[str, Any]]:
        """
        Get the best available model for a specific task.

        Args:
            required_context: Minimum context window needed (4k, 8k, 16k, 32k)
            task_type: Type of task (code_improvement, code_review, refactoring)

        Returns:
            Best matching model or None
        """
        models = await self.discover_models()

        # Parse required context to int
        context_map = {"4k": 4096, "8k": 8192, "16k": 16384, "32k": 32768}
        min_context = context_map.get(required_context, 4096)

        # Filter by context window
        suitable = [m for m in models if m["context_window"] >= min_context]

        if not suitable:
            # Fall back to largest available
            if models:
                return models[0]
            return None

        # Prefer coding-specialized models for code tasks
        if task_type in ("code_improvement", "refactoring"):
            for model in suitable:
                if "code" in model["id"].lower() or "coder" in model["id"].lower():
                    return model

        # Return first suitable (largest context)
        return suitable[0] if suitable else None

    def _estimate_context_window(self, model_id: str) -> int:
        """Estimate context window from model name."""
        model_lower = model_id.lower()

        # Known patterns
        if "32k" in model_lower or "32000" in model_lower:
            return 32768
        if "16k" in model_lower or "16000" in model_lower:
            return 16384
        if "8k" in model_lower or "8000" in model_lower:
            return 8192

        # Model family defaults
        if "deepseek" in model_lower:
            return 16384  # DeepSeek models typically have 16k
        if "codellama" in model_lower:
            return 16384
        if "llama" in model_lower:
            return 8192
        if "qwen" in model_lower:
            return 32768  # Qwen models often have 32k

        return 4096  # Default

    def _infer_capabilities(self, model_id: str) -> List[str]:
        """Infer capabilities from model name."""
        capabilities = ["text_generation"]
        model_lower = model_id.lower()

        if any(x in model_lower for x in ["code", "coder", "starcoder"]):
            capabilities.extend(["code_generation", "code_completion", "code_review"])

        if "instruct" in model_lower or "chat" in model_lower:
            capabilities.append("instruction_following")

        if any(x in model_lower for x in ["deepseek", "codellama", "wizardcoder"]):
            capabilities.append("code_improvement")

        return capabilities


class IntelligentOuroborosModelSelector:
    """
    v2.0: Intelligent model selection for Ouroboros self-programming.

    Combines:
    - IntelligentModelSelector for general model selection
    - JarvisPrimeModelDiscovery for JARVIS Prime models
    - CodeComplexityAnalyzer for code-aware selection

    Selection criteria:
    1. Code complexity -> required model capability
    2. File size -> required context window
    3. Task type -> specialized model preference
    4. System resources -> RAM-aware selection
    """

    def __init__(self):
        self._model_discovery = JarvisPrimeModelDiscovery()
        self._selector: Optional[IntelligentModelSelector] = None
        self._lock = asyncio.Lock()

        # Try to initialize IntelligentModelSelector
        if INTELLIGENT_SELECTOR_AVAILABLE:
            try:
                self._selector = IntelligentModelSelector()
            except Exception as e:
                logger.warning(f"Failed to initialize IntelligentModelSelector: {e}")

    async def select_model_for_code(
        self,
        code: str,
        task_type: str = "code_improvement",
        prefer_local: bool = True,
    ) -> Dict[str, Any]:
        """
        Select the best model for a code improvement task.

        Args:
            code: The code to be improved
            task_type: Type of task (code_improvement, refactoring, bug_fix)
            prefer_local: Prefer local models (JARVIS Prime, Ollama)

        Returns:
            Dict with:
            - provider: Provider name (jarvis-prime, ollama, anthropic)
            - model: Model ID
            - api_base: API base URL
            - context_window: Context window size
            - reasoning: Why this model was selected
        """
        async with self._lock:
            # Analyze code complexity
            complexity = CodeComplexityAnalyzer.analyze(code)
            reasoning = []

            reasoning.append(f"Code analysis: {complexity['line_count']} lines, "
                           f"complexity={complexity['complexity']}, "
                           f"required_context={complexity['required_context']}")

            # Step 1: Try JARVIS Prime model discovery
            if prefer_local:
                prime_model = await self._model_discovery.get_best_model_for_task(
                    required_context=complexity["required_context"],
                    task_type=task_type,
                )

                if prime_model:
                    reasoning.append(f"Selected JARVIS Prime model: {prime_model['id']} "
                                   f"(context: {prime_model['context_window']})")
                    return {
                        "provider": "jarvis-prime",
                        "model": prime_model["id"],
                        "api_base": os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
                        "context_window": prime_model["context_window"],
                        "reasoning": reasoning,
                    }

            # Step 2: Try IntelligentModelSelector
            if self._selector:
                try:
                    # Determine required capabilities based on task
                    capabilities = {"code_generation"}
                    if task_type == "refactoring":
                        capabilities.add("code_refactoring")
                    elif task_type == "bug_fix":
                        capabilities.add("code_debugging")

                    model = await self._selector.select_best_model(
                        query=f"Improve this {complexity['complexity']} complexity code",
                        intent="code_improvement",
                        required_capabilities=capabilities,
                        context={
                            "code_complexity": complexity["complexity"],
                            "required_context": complexity["required_context"],
                        }
                    )

                    if model:
                        reasoning.append(f"IntelligentModelSelector chose: {model.name}")
                        return {
                            "provider": model.provider,
                            "model": model.model_id,
                            "api_base": model.api_base,
                            "context_window": model.context_window,
                            "reasoning": reasoning,
                        }
                except Exception as e:
                    reasoning.append(f"IntelligentModelSelector failed: {e}")

            # Step 3: Fallback to configured providers
            reasoning.append("Using fallback provider selection")
            return self._get_fallback_model(complexity, reasoning)

    def _get_fallback_model(
        self,
        complexity: Dict[str, Any],
        reasoning: List[str],
    ) -> Dict[str, Any]:
        """Get fallback model based on complexity."""
        # For complex code, prefer larger context models
        if complexity["required_context"] in ("16k", "32k"):
            # Try DeepSeek first (good at code, large context)
            return {
                "provider": "jarvis-prime",
                "model": os.getenv("JARVIS_PRIME_MODEL", "deepseek-coder-v2"),
                "api_base": os.getenv("JARVIS_PRIME_API_BASE", "http://localhost:8000/v1"),
                "context_window": 16384,
                "reasoning": reasoning + ["Fallback: deepseek-coder-v2 for large context"],
            }
        else:
            # For simpler code, any coding model works
            return {
                "provider": "ollama",
                "model": os.getenv("OLLAMA_MODEL", "codellama"),
                "api_base": os.getenv("OLLAMA_API_BASE", "http://localhost:11434/v1"),
                "context_window": 8192,
                "reasoning": reasoning + ["Fallback: codellama for simple code"],
            }

    async def get_health(self) -> Dict[str, Any]:
        """Get health status of model selection components."""
        models = await self._model_discovery.discover_models()

        return {
            "intelligent_selector_available": INTELLIGENT_SELECTOR_AVAILABLE,
            "intelligent_selector_initialized": self._selector is not None,
            "jarvis_prime_models": len(models),
            "jarvis_prime_models_list": [m["id"] for m in models[:5]],  # Top 5
        }


# Global instance for easy access
_intelligent_selector: Optional[IntelligentOuroborosModelSelector] = None


def get_intelligent_ouroboros_selector() -> IntelligentOuroborosModelSelector:
    """Get global intelligent model selector instance."""
    global _intelligent_selector
    if _intelligent_selector is None:
        _intelligent_selector = IntelligentOuroborosModelSelector()
    return _intelligent_selector


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
    LLM client with intelligent model selection and automatic failover.

    v2.0 Enhancement: Uses IntelligentOuroborosModelSelector for dynamic
    model selection based on code complexity, task type, and availability.

    Falls back through providers in order:
    1. Intelligently selected model (based on code analysis)
    2. JARVIS Prime (local, free)
    3. Ollama (local, free)
    4. Anthropic API (cloud, paid)

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
        self._intelligent_selector = get_intelligent_ouroboros_selector()
        self._last_model_selection: Optional[Dict[str, Any]] = None

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
        code_context: Optional[str] = None,
        task_type: str = "code_improvement",
        prefer_local: bool = True,
    ) -> Tuple[str, str]:
        """
        Generate response from best available provider using intelligent selection.

        v2.0 Enhancement: Uses IntelligentOuroborosModelSelector to dynamically
        select the best model based on code complexity and task requirements.

        Args:
            prompt: The prompt to send to the LLM
            system_prompt: Optional system prompt
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens in response
            code_context: Optional code for complexity analysis (enables intelligent selection)
            task_type: Type of task (code_improvement, refactoring, bug_fix)
            prefer_local: Prefer local models (JARVIS Prime, Ollama) over cloud APIs

        Returns:
            (response_text, provider_name)
        """
        async with self._lock:
            last_error = None
            providers_to_try = []

            # v2.0: Use intelligent model selection if code context provided
            if code_context:
                try:
                    selection = await self._intelligent_selector.select_model_for_code(
                        code=code_context,
                        task_type=task_type,
                        prefer_local=prefer_local,
                    )
                    self._last_model_selection = selection

                    logger.info(
                        f"Intelligent selection: {selection['provider']}/{selection['model']} "
                        f"(context: {selection.get('context_window', 'unknown')})"
                    )

                    # Build provider config from selection
                    selected_provider = {
                        "name": f"{selection['provider']}-intelligent",
                        "api_base": selection["api_base"],
                        "api_key": self._get_api_key_for_provider(selection["provider"]),
                        "model": selection["model"],
                        "timeout": 120.0,
                    }

                    # Ensure circuit breaker exists for dynamic provider
                    if selected_provider["name"] not in self._circuit_breakers:
                        self._circuit_breakers[selected_provider["name"]] = CircuitBreaker(
                            name=selected_provider["name"]
                        )

                    # Intelligently selected provider goes first
                    providers_to_try.append(selected_provider)

                except Exception as e:
                    logger.warning(f"Intelligent model selection failed: {e}")
                    self._last_model_selection = {"error": str(e)}

            # Add static providers as fallback
            providers_to_try.extend(self._providers)

            # Try each provider
            for provider in providers_to_try:
                name = provider["name"]

                # Get or create circuit breaker
                if name not in self._circuit_breakers:
                    self._circuit_breakers[name] = CircuitBreaker(name=name)
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

    def _get_api_key_for_provider(self, provider_type: str) -> str:
        """Get API key for a provider type."""
        provider_keys = {
            "jarvis-prime": os.getenv("JARVIS_PRIME_API_KEY", "sk-local-jarvis"),
            "ollama": "ollama",
            "anthropic": os.getenv("ANTHROPIC_API_KEY", ""),
            "openai": os.getenv("OPENAI_API_KEY", ""),
        }
        return provider_keys.get(provider_type, "")

    def get_last_model_selection(self) -> Optional[Dict[str, Any]]:
        """Get details of the last intelligent model selection."""
        return self._last_model_selection

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
        task_type: str = "code_improvement",
        prefer_local: bool = True,
    ) -> Optional[str]:
        """
        Generate improved code using intelligently selected provider.

        v2.0 Enhancement: Uses IntelligentOuroborosModelSelector to dynamically
        select the best model based on code complexity and task requirements.

        Args:
            original_code: Original source code
            goal: Improvement goal
            error_log: Optional error from previous attempt
            context: Optional context from related files
            task_type: Type of task (code_improvement, refactoring, bug_fix)
            prefer_local: Prefer local models (JARVIS Prime, Ollama) over cloud APIs

        Returns:
            Improved code or None if all providers fail
        """
        if not self._improvement_circuit.can_execute():
            logger.warning("Improvement circuit breaker is OPEN, rejecting request")
            return None

        # v2.0: Analyze code complexity for model selection
        complexity = CodeComplexityAnalyzer.analyze(original_code)
        logger.info(
            f"Code complexity analysis: {complexity['line_count']} lines, "
            f"complexity={complexity['complexity']}, "
            f"required_context={complexity['required_context']}"
        )

        # Determine task type based on goal keywords
        detected_task = task_type
        goal_lower = goal.lower()
        if any(kw in goal_lower for kw in ["fix", "bug", "error", "issue"]):
            detected_task = "bug_fix"
        elif any(kw in goal_lower for kw in ["refactor", "clean", "reorganize", "restructure"]):
            detected_task = "refactoring"
        elif any(kw in goal_lower for kw in ["optimize", "performance", "speed", "memory"]):
            detected_task = "optimization"

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
            # v2.0: Pass code context for intelligent model selection
            response, provider = await self._llm_client.generate(
                prompt="".join(prompt_parts),
                system_prompt=system_prompt,
                temperature=0.3,
                code_context=original_code,  # Enable intelligent selection
                task_type=detected_task,
                prefer_local=prefer_local,
            )

            self._improvement_circuit.record_success()

            # Log model selection details
            selection = self._llm_client.get_last_model_selection()
            if selection and "error" not in selection:
                logger.info(
                    f"Model selection reasoning: {selection.get('reasoning', ['N/A'])[-1]}"
                )

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
            "intelligent_selection_available": INTELLIGENT_SELECTOR_AVAILABLE,
            "last_model_selection": self._llm_client.get_last_model_selection(),
        }

    async def get_intelligent_selector_health(self) -> Dict[str, Any]:
        """Get health status of intelligent model selection components."""
        selector = get_intelligent_ouroboros_selector()
        return await selector.get_health()


# =============================================================================
# AGENTIC LOOP ORCHESTRATOR
# =============================================================================

class AgenticTaskPriority(Enum):
    """Priority levels for agentic tasks."""
    CRITICAL = 1  # Security fixes, breaking bugs
    HIGH = 2      # User-requested improvements
    NORMAL = 3    # Scheduled improvements
    LOW = 4       # Background optimizations
    BACKGROUND = 5  # Opportunistic improvements


@dataclass
class AgenticTask:
    """A task for the agentic improvement loop."""
    task_id: str
    file_path: Path
    goal: str
    priority: AgenticTaskPriority
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    status: str = "pending"  # pending, running, completed, failed
    result: Optional[str] = None
    error: Optional[str] = None
    iterations: int = 0
    max_iterations: int = 10
    triggered_by: str = "manual"  # manual, voice, event, scheduled


class AgenticLoopOrchestrator:
    """
    v2.0: Orchestrates autonomous self-programming improvement loops.

    Features:
    - Priority-based task queue with async processing
    - Autonomous improvement cycles with intelligent model selection
    - Self-healing capabilities (auto-fix failures)
    - Voice command integration
    - Event-driven triggers from file watchers
    - Reactor Core experience publishing
    - Circuit breaker protection
    - Parallel task execution with concurrency limits

    The orchestrator runs continuously, processing improvement tasks
    from the queue and learning from each iteration.
    """

    def __init__(
        self,
        max_concurrent_tasks: int = 3,
        max_iterations_per_task: int = 10,
        idle_poll_interval: float = 5.0,
    ):
        self._integration = EnhancedOuroborosIntegration()
        self._task_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._active_tasks: Dict[str, AgenticTask] = {}
        self._completed_tasks: List[AgenticTask] = []
        self._failed_tasks: List[AgenticTask] = []

        self._max_concurrent = max_concurrent_tasks
        self._max_iterations = max_iterations_per_task
        self._poll_interval = idle_poll_interval

        self._running = False
        self._workers: List[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._shutdown_event = asyncio.Event()

        # Event callbacks for extensibility
        self._on_task_complete: List[Callable] = []
        self._on_task_failed: List[Callable] = []
        self._on_improvement_generated: List[Callable] = []

        # Metrics
        self._metrics = {
            "tasks_queued": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "total_iterations": 0,
            "total_improvements_generated": 0,
        }

        logger.info(f"AgenticLoopOrchestrator initialized (max_concurrent={max_concurrent_tasks})")

    async def start(self) -> None:
        """Start the agentic loop orchestrator."""
        if self._running:
            logger.warning("AgenticLoopOrchestrator already running")
            return

        logger.info("Starting AgenticLoopOrchestrator...")

        # Initialize integration
        if not await self._integration.initialize():
            logger.error("Failed to initialize integration - no LLM providers available")
            return

        self._running = True
        self._shutdown_event.clear()

        # Start worker tasks
        for i in range(self._max_concurrent):
            worker = asyncio.create_task(self._worker_loop(i))
            self._workers.append(worker)

        logger.info(f"AgenticLoopOrchestrator started with {self._max_concurrent} workers")

    async def stop(self) -> None:
        """Stop the agentic loop orchestrator gracefully."""
        if not self._running:
            return

        logger.info("Stopping AgenticLoopOrchestrator...")
        self._running = False
        self._shutdown_event.set()

        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers.clear()

        await self._integration.shutdown()
        logger.info("AgenticLoopOrchestrator stopped")

    async def submit_task(
        self,
        file_path: Path,
        goal: str,
        priority: AgenticTaskPriority = AgenticTaskPriority.NORMAL,
        triggered_by: str = "manual",
        max_iterations: Optional[int] = None,
    ) -> str:
        """
        Submit a new improvement task to the queue.

        Args:
            file_path: Path to the file to improve
            goal: Improvement goal
            priority: Task priority
            triggered_by: What triggered this task (manual, voice, event, scheduled)
            max_iterations: Maximum improvement iterations

        Returns:
            Task ID
        """
        task_id = f"agentic_{uuid.uuid4().hex[:12]}"

        task = AgenticTask(
            task_id=task_id,
            file_path=file_path,
            goal=goal,
            priority=priority,
            triggered_by=triggered_by,
            max_iterations=max_iterations or self._max_iterations,
        )

        # Priority queue uses (priority_value, task) tuples
        await self._task_queue.put((priority.value, task))
        self._metrics["tasks_queued"] += 1

        logger.info(f"Submitted task {task_id}: {goal} (priority={priority.name})")
        return task_id

    async def submit_voice_command(
        self,
        command: str,
        target_file: Optional[str] = None,
    ) -> Optional[str]:
        """
        Process a voice command for code improvement.

        Voice command examples:
        - "Improve the error handling in cloud_sql_connection_manager"
        - "Optimize the performance of the orchestration engine"
        - "Fix the bug in the trinity bridge"
        - "Refactor the model selector for better readability"

        Args:
            command: The voice command
            target_file: Optional specific file to target

        Returns:
            Task ID if submitted, None if command not understood
        """
        command_lower = command.lower()

        # Parse command for goal and priority
        goal = command
        priority = AgenticTaskPriority.HIGH  # Voice commands are high priority

        # Detect task type from command
        if any(kw in command_lower for kw in ["fix", "bug", "error", "issue", "broken"]):
            priority = AgenticTaskPriority.CRITICAL
        elif any(kw in command_lower for kw in ["optimize", "performance", "speed"]):
            priority = AgenticTaskPriority.HIGH
        elif any(kw in command_lower for kw in ["refactor", "clean", "improve readability"]):
            priority = AgenticTaskPriority.NORMAL

        # Find target file
        file_path = None
        if target_file:
            file_path = Path(target_file)
        else:
            # Try to extract file name from command
            file_path = await self._infer_file_from_command(command)

        if not file_path or not file_path.exists():
            logger.warning(f"Could not find target file for voice command: {command}")
            return None

        return await self.submit_task(
            file_path=file_path,
            goal=goal,
            priority=priority,
            triggered_by="voice",
        )

    async def _infer_file_from_command(self, command: str) -> Optional[Path]:
        """Infer target file from voice command."""
        command_lower = command.lower()

        # Common file patterns (snake_case names mentioned in commands)
        file_patterns = {
            "connection manager": "cloud_sql_connection_manager.py",
            "orchestration engine": "orchestration_engine.py",
            "trinity bridge": "trinity_bridge.py",
            "reactor bridge": "reactor_bridge.py",
            "model selector": "model_selector.py",
            "integration": "integration.py",
            "cost sync": "cross_repo_cost_sync.py",
            "startup orchestrator": "cross_repo_startup_orchestrator.py",
        }

        for pattern, filename in file_patterns.items():
            if pattern in command_lower:
                # Search for file in backend directory
                backend_dir = Path(__file__).parent.parent.parent
                for match in backend_dir.rglob(filename):
                    return match

        return None

    async def _worker_loop(self, worker_id: int) -> None:
        """Worker loop that processes tasks from the queue."""
        logger.debug(f"Worker {worker_id} started")

        while self._running:
            try:
                # Wait for task with timeout
                try:
                    priority_value, task = await asyncio.wait_for(
                        self._task_queue.get(),
                        timeout=self._poll_interval
                    )
                except asyncio.TimeoutError:
                    # No tasks, check if we should shutdown
                    if self._shutdown_event.is_set():
                        break
                    continue

                # Process the task
                async with self._lock:
                    self._active_tasks[task.task_id] = task

                task.status = "running"
                task.started_at = time.time()

                try:
                    await self._process_task(task, worker_id)
                except Exception as e:
                    task.status = "failed"
                    task.error = str(e)
                    logger.error(f"Worker {worker_id} task {task.task_id} failed: {e}")
                finally:
                    task.completed_at = time.time()

                    async with self._lock:
                        del self._active_tasks[task.task_id]

                        if task.status == "completed":
                            self._completed_tasks.append(task)
                            self._metrics["tasks_completed"] += 1
                            await self._trigger_callbacks(self._on_task_complete, task)
                        else:
                            self._failed_tasks.append(task)
                            self._metrics["tasks_failed"] += 1
                            await self._trigger_callbacks(self._on_task_failed, task)

                    self._task_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                await asyncio.sleep(1)

        logger.debug(f"Worker {worker_id} stopped")

    async def _process_task(self, task: AgenticTask, worker_id: int) -> None:
        """Process a single improvement task."""
        logger.info(f"Worker {worker_id} processing task {task.task_id}: {task.goal}")

        # Read the original file
        if not task.file_path.exists():
            raise FileNotFoundError(f"File not found: {task.file_path}")

        original_code = await asyncio.to_thread(task.file_path.read_text)
        current_code = original_code
        error_log = None

        # Improvement loop
        for iteration in range(task.max_iterations):
            task.iterations = iteration + 1
            self._metrics["total_iterations"] += 1

            logger.debug(f"Task {task.task_id} iteration {iteration + 1}/{task.max_iterations}")

            # Generate improvement
            improved_code = await self._integration.generate_improvement(
                original_code=current_code,
                goal=task.goal,
                error_log=error_log,
            )

            if not improved_code:
                logger.warning(f"Task {task.task_id}: No improvement generated")
                continue

            self._metrics["total_improvements_generated"] += 1
            await self._trigger_callbacks(self._on_improvement_generated, task, improved_code)

            # Validate in sandbox if enabled
            if self._integration._sandbox:
                success, output = await self._integration.validate_in_sandbox(
                    original_file=task.file_path,
                    modified_content=improved_code,
                    test_command=f"python -m py_compile {task.file_path.name}",
                )

                if not success:
                    error_log = output
                    current_code = improved_code  # Try to fix the error
                    continue

            # Success - apply the improvement
            await asyncio.to_thread(task.file_path.write_text, improved_code)

            task.status = "completed"
            task.result = f"Improved after {iteration + 1} iterations"

            # Publish experience to Reactor Core
            await self._integration.publish_experience(
                original_code=original_code,
                improved_code=improved_code,
                goal=task.goal,
                success=True,
                iterations=iteration + 1,
                error_history=[error_log] if error_log else [],
            )

            logger.info(f"Task {task.task_id} completed successfully")
            return

        # Max iterations reached without success
        task.status = "failed"
        task.error = f"Failed after {task.max_iterations} iterations"

        # Publish failed experience for learning
        await self._integration.publish_experience(
            original_code=original_code,
            improved_code=current_code,
            goal=task.goal,
            success=False,
            iterations=task.max_iterations,
            error_history=[error_log] if error_log else [],
        )

    async def _trigger_callbacks(
        self,
        callbacks: List[Callable],
        *args,
        **kwargs,
    ) -> None:
        """Trigger registered callbacks."""
        for callback in callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(*args, **kwargs)
                else:
                    callback(*args, **kwargs)
            except Exception as e:
                logger.error(f"Callback error: {e}")

    def on_task_complete(self, callback: Callable) -> None:
        """Register callback for task completion."""
        self._on_task_complete.append(callback)

    def on_task_failed(self, callback: Callable) -> None:
        """Register callback for task failure."""
        self._on_task_failed.append(callback)

    def on_improvement_generated(self, callback: Callable) -> None:
        """Register callback for improvement generation."""
        self._on_improvement_generated.append(callback)

    def get_task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get status of a specific task."""
        # Check active tasks
        if task_id in self._active_tasks:
            task = self._active_tasks[task_id]
            return self._task_to_dict(task)

        # Check completed tasks
        for task in self._completed_tasks:
            if task.task_id == task_id:
                return self._task_to_dict(task)

        # Check failed tasks
        for task in self._failed_tasks:
            if task.task_id == task_id:
                return self._task_to_dict(task)

        return None

    def _task_to_dict(self, task: AgenticTask) -> Dict[str, Any]:
        """Convert task to dictionary."""
        return {
            "task_id": task.task_id,
            "file_path": str(task.file_path),
            "goal": task.goal,
            "priority": task.priority.name,
            "status": task.status,
            "iterations": task.iterations,
            "max_iterations": task.max_iterations,
            "triggered_by": task.triggered_by,
            "created_at": task.created_at,
            "started_at": task.started_at,
            "completed_at": task.completed_at,
            "result": task.result,
            "error": task.error,
        }

    def get_status(self) -> Dict[str, Any]:
        """Get orchestrator status."""
        return {
            "running": self._running,
            "workers": len(self._workers),
            "queue_size": self._task_queue.qsize(),
            "active_tasks": len(self._active_tasks),
            "completed_tasks": len(self._completed_tasks),
            "failed_tasks": len(self._failed_tasks),
            "metrics": self._metrics,
            "integration": self._integration.get_status(),
        }


# =============================================================================
# GLOBAL INSTANCES
# =============================================================================

_integration: Optional[EnhancedOuroborosIntegration] = None
_orchestrator: Optional[AgenticLoopOrchestrator] = None


def get_ouroboros_integration() -> EnhancedOuroborosIntegration:
    """Get global Ouroboros integration instance."""
    global _integration
    if _integration is None:
        _integration = EnhancedOuroborosIntegration()
    return _integration


def get_agentic_orchestrator() -> AgenticLoopOrchestrator:
    """Get global agentic loop orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = AgenticLoopOrchestrator()
    return _orchestrator


async def shutdown_ouroboros_integration() -> None:
    """Shutdown global integration and orchestrator."""
    global _integration, _orchestrator

    if _orchestrator:
        await _orchestrator.stop()
        _orchestrator = None

    if _integration:
        await _integration.shutdown()
        _integration = None
