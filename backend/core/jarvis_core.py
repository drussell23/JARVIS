"""
JARVIS Core - Integrated system with Model Manager, Memory Controller, and Task Router
Built for scale and memory efficiency

v84.0 - Trinity Integration with Intelligent LLM Routing

Features:
- Intelligent complexity-based routing (J-Prime → Local → Cloud)
- Adaptive latency-based fallback
- Memory-aware model selection
- Parallel inference coordination
- Lock timeout protection
"""

import asyncio
import logging
import os
import time
from typing import Dict, Any, Optional, List, Tuple, Callable
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum, auto
import json
from pathlib import Path

from .model_manager import ModelManager, ModelTier
from .memory_controller import MemoryController, MemoryPressure
from .task_router import TaskRouter, TaskAnalysis

logger = logging.getLogger(__name__)


# =============================================================================
# v84.0: INTELLIGENT LLM ROUTING ENGINE
# =============================================================================

class LLMBackend(Enum):
    """LLM backend options."""
    JPRIME = auto()      # Local J-Prime (fastest, free)
    LOCAL = auto()       # Local GGUF models
    CLOUD_GCP = auto()   # GCP Cloud (Gemini/PaLM)
    CLOUD_ANTHROPIC = auto()  # Anthropic Claude
    CLOUD_OPENAI = auto()     # OpenAI GPT


class ComplexityLevel(Enum):
    """Query complexity levels."""
    TRIVIAL = 1      # Simple greetings, yes/no
    SIMPLE = 2       # Basic questions, short answers
    MODERATE = 3     # Multi-step reasoning, analysis
    COMPLEX = 4      # Deep reasoning, code generation
    EXPERT = 5       # Multi-domain expertise, long-form


@dataclass
class ComplexityAnalysis:
    """Result of query complexity analysis."""
    level: ComplexityLevel
    score: float  # 0.0 - 1.0
    estimated_tokens: int
    reasoning_required: bool
    code_generation: bool
    multi_turn: bool
    domain_expertise: List[str]
    confidence: float


@dataclass
class LLMRoute:
    """Selected LLM route with metadata."""
    backend: LLMBackend
    reason: str
    estimated_latency_ms: float
    estimated_cost: float
    fallback_chain: List[LLMBackend]


class IntelligentLLMRouter:
    """
    v84.0: Intelligent LLM routing based on query complexity.

    Routes queries to the most appropriate backend:
    - TRIVIAL/SIMPLE → J-Prime (fast, free)
    - MODERATE → J-Prime or Local (based on availability)
    - COMPLEX → Cloud (when J-Prime can't handle)
    - EXPERT → Cloud with best model

    Features:
    - Complexity scoring with multiple signals
    - Latency-based adaptive routing
    - Cost optimization
    - Memory-aware decisions
    - Automatic fallback chains
    """

    # Complexity indicators (regex patterns compiled once)
    _SIMPLE_PATTERNS = [
        r"^(hi|hello|hey|thanks|thank you|bye|goodbye)",
        r"^what('s| is) (the )?(time|date|weather)",
        r"^(yes|no|ok|okay|sure)$",
    ]

    _COMPLEX_PATTERNS = [
        r"(explain|analyze|compare|implement|design|architect)",
        r"(step[- ]by[- ]step|detailed|comprehensive|thorough)",
        r"(code|function|class|algorithm|data structure)",
        r"(because|therefore|however|although|consequently)",
        r"\d{3,}",  # Large numbers suggest calculations
    ]

    def __init__(self):
        # Backend availability and latency tracking
        self._backend_latencies: Dict[LLMBackend, float] = {
            LLMBackend.JPRIME: float('inf'),
            LLMBackend.LOCAL: float('inf'),
            LLMBackend.CLOUD_GCP: float('inf'),
        }
        self._backend_available: Dict[LLMBackend, bool] = {
            LLMBackend.JPRIME: False,
            LLMBackend.LOCAL: True,
            LLMBackend.CLOUD_GCP: True,
        }

        # Configuration from environment
        self._jprime_complexity_threshold = float(os.getenv(
            "JARVIS_JPRIME_COMPLEXITY_THRESHOLD", "0.6"
        ))
        self._prefer_local = os.getenv(
            "JARVIS_PREFER_LOCAL", "true"
        ).lower() == "true"
        self._cost_sensitivity = float(os.getenv(
            "JARVIS_COST_SENSITIVITY", "0.5"  # 0=ignore cost, 1=minimize cost
        ))

        # J-Prime client (lazy loaded)
        self._jprime_client = None
        self._jprime_lock = asyncio.Lock()

        # Compiled patterns
        import re
        self._simple_re = [re.compile(p, re.IGNORECASE) for p in self._SIMPLE_PATTERNS]
        self._complex_re = [re.compile(p, re.IGNORECASE) for p in self._COMPLEX_PATTERNS]

    async def get_jprime_client(self):
        """Lazily get or create J-Prime client."""
        if self._jprime_client is None:
            async with self._jprime_lock:
                if self._jprime_client is None:
                    try:
                        from backend.clients.jarvis_prime_client import get_jarvis_prime_client
                        self._jprime_client = await get_jarvis_prime_client()
                        logger.info("[LLMRouter] J-Prime client initialized")
                    except Exception as e:
                        logger.warning(f"[LLMRouter] J-Prime client unavailable: {e}")
        return self._jprime_client

    def analyze_complexity(self, query: str, context: Optional[List[str]] = None) -> ComplexityAnalysis:
        """
        Analyze query complexity using multiple signals.

        Signals:
        - Query length
        - Pattern matching (simple vs complex)
        - Context length
        - Domain indicators
        - Reasoning indicators
        """
        query_lower = query.lower().strip()

        # Initialize scores
        simple_score = 0.0
        complex_score = 0.0

        # Pattern matching
        for pattern in self._simple_re:
            if pattern.search(query_lower):
                simple_score += 0.3

        for pattern in self._complex_re:
            if pattern.search(query_lower):
                complex_score += 0.2

        # Length-based scoring
        word_count = len(query.split())
        if word_count < 5:
            simple_score += 0.2
        elif word_count > 50:
            complex_score += 0.3
        elif word_count > 20:
            complex_score += 0.1

        # Context influence
        context_len = sum(len(c) for c in (context or []))
        if context_len > 1000:
            complex_score += 0.2

        # Code indicators
        code_generation = any(kw in query_lower for kw in [
            "code", "function", "class", "implement", "write a", "create a script"
        ])
        if code_generation:
            complex_score += 0.3

        # Reasoning indicators
        reasoning_required = any(kw in query_lower for kw in [
            "why", "how", "explain", "analyze", "compare", "what if", "should i"
        ])
        if reasoning_required:
            complex_score += 0.2

        # Domain expertise
        domains = []
        domain_keywords = {
            "programming": ["code", "function", "api", "debug", "algorithm"],
            "math": ["calculate", "equation", "solve", "formula", "integrate"],
            "science": ["theory", "hypothesis", "experiment", "research"],
            "business": ["strategy", "market", "revenue", "profit", "roi"],
        }
        for domain, keywords in domain_keywords.items():
            if any(kw in query_lower for kw in keywords):
                domains.append(domain)
                complex_score += 0.1

        # Normalize scores
        total = simple_score + complex_score + 0.001  # Avoid division by zero
        complexity_score = complex_score / total

        # Determine level
        if complexity_score < 0.2:
            level = ComplexityLevel.TRIVIAL
        elif complexity_score < 0.4:
            level = ComplexityLevel.SIMPLE
        elif complexity_score < 0.6:
            level = ComplexityLevel.MODERATE
        elif complexity_score < 0.8:
            level = ComplexityLevel.COMPLEX
        else:
            level = ComplexityLevel.EXPERT

        # Estimate tokens
        estimated_tokens = max(100, word_count * 10 + context_len // 4)

        return ComplexityAnalysis(
            level=level,
            score=complexity_score,
            estimated_tokens=estimated_tokens,
            reasoning_required=reasoning_required,
            code_generation=code_generation,
            multi_turn=len(context or []) > 2,
            domain_expertise=domains,
            confidence=min(1.0, abs(complexity_score - 0.5) * 2 + 0.5),
        )

    async def route(
        self,
        query: str,
        context: Optional[List[str]] = None,
        memory_pressure: Optional[MemoryPressure] = None,
    ) -> LLMRoute:
        """
        Route query to the optimal LLM backend.

        Decision factors:
        1. Query complexity
        2. Backend availability
        3. Memory pressure
        4. Cost sensitivity
        5. Historical latency
        """
        analysis = self.analyze_complexity(query, context)

        # Check J-Prime availability
        jprime_client = await self.get_jprime_client()
        jprime_available = jprime_client is not None and jprime_client.is_online

        # Update availability
        self._backend_available[LLMBackend.JPRIME] = jprime_available

        # Build fallback chain
        fallback_chain = []

        # Decision logic
        if analysis.level in (ComplexityLevel.TRIVIAL, ComplexityLevel.SIMPLE):
            # Simple queries → J-Prime first (fast, free)
            if jprime_available:
                return LLMRoute(
                    backend=LLMBackend.JPRIME,
                    reason=f"Simple query ({analysis.level.name}), J-Prime available",
                    estimated_latency_ms=self._backend_latencies.get(LLMBackend.JPRIME, 500),
                    estimated_cost=0.0,
                    fallback_chain=[LLMBackend.LOCAL, LLMBackend.CLOUD_GCP],
                )
            else:
                return LLMRoute(
                    backend=LLMBackend.LOCAL,
                    reason=f"Simple query, J-Prime unavailable",
                    estimated_latency_ms=1000,
                    estimated_cost=0.0,
                    fallback_chain=[LLMBackend.CLOUD_GCP],
                )

        elif analysis.level == ComplexityLevel.MODERATE:
            # Moderate → J-Prime if available, else Local
            if jprime_available and analysis.score < self._jprime_complexity_threshold:
                return LLMRoute(
                    backend=LLMBackend.JPRIME,
                    reason=f"Moderate query within J-Prime capability",
                    estimated_latency_ms=self._backend_latencies.get(LLMBackend.JPRIME, 1000),
                    estimated_cost=0.0,
                    fallback_chain=[LLMBackend.LOCAL, LLMBackend.CLOUD_GCP],
                )
            else:
                return LLMRoute(
                    backend=LLMBackend.LOCAL,
                    reason=f"Moderate query, using local models",
                    estimated_latency_ms=2000,
                    estimated_cost=0.0,
                    fallback_chain=[LLMBackend.CLOUD_GCP],
                )

        elif analysis.level == ComplexityLevel.COMPLEX:
            # Complex → Cloud preferred, J-Prime as fallback
            if memory_pressure == MemoryPressure.CRITICAL:
                # Memory critical - use cloud to avoid loading models
                return LLMRoute(
                    backend=LLMBackend.CLOUD_GCP,
                    reason=f"Complex query + critical memory pressure",
                    estimated_latency_ms=3000,
                    estimated_cost=0.001 * analysis.estimated_tokens,
                    fallback_chain=[LLMBackend.JPRIME] if jprime_available else [],
                )

            if self._cost_sensitivity > 0.7 and jprime_available:
                # Cost-sensitive + J-Prime available
                return LLMRoute(
                    backend=LLMBackend.JPRIME,
                    reason=f"Complex query, cost-sensitive mode",
                    estimated_latency_ms=2000,
                    estimated_cost=0.0,
                    fallback_chain=[LLMBackend.CLOUD_GCP],
                )

            return LLMRoute(
                backend=LLMBackend.CLOUD_GCP,
                reason=f"Complex query requiring advanced reasoning",
                estimated_latency_ms=3000,
                estimated_cost=0.001 * analysis.estimated_tokens,
                fallback_chain=[LLMBackend.JPRIME, LLMBackend.LOCAL] if jprime_available else [LLMBackend.LOCAL],
            )

        else:  # EXPERT
            # Expert → Always cloud
            return LLMRoute(
                backend=LLMBackend.CLOUD_GCP,
                reason=f"Expert-level query requiring best model",
                estimated_latency_ms=5000,
                estimated_cost=0.002 * analysis.estimated_tokens,
                fallback_chain=[LLMBackend.JPRIME] if jprime_available else [],
            )

    def update_latency(self, backend: LLMBackend, latency_ms: float) -> None:
        """Update latency tracking for adaptive routing."""
        # Exponential moving average
        alpha = 0.3
        current = self._backend_latencies.get(backend, latency_ms)
        self._backend_latencies[backend] = alpha * latency_ms + (1 - alpha) * current

    def get_stats(self) -> Dict[str, Any]:
        """Get routing statistics."""
        return {
            "backend_latencies": {b.name: l for b, l in self._backend_latencies.items()},
            "backend_available": {b.name: a for b, a in self._backend_available.items()},
            "jprime_threshold": self._jprime_complexity_threshold,
            "cost_sensitivity": self._cost_sensitivity,
        }


# Global router instance
_llm_router: Optional[IntelligentLLMRouter] = None


def get_llm_router() -> IntelligentLLMRouter:
    """Get or create the global LLM router."""
    global _llm_router
    if _llm_router is None:
        _llm_router = IntelligentLLMRouter()
    return _llm_router

class JARVISCore:
    """
    Core JARVIS system integrating all components for intelligent,
    memory-efficient operation
    """
    
    def __init__(self, 
                 models_dir: str = "models",
                 config_path: Optional[str] = None):
        """Initialize JARVIS Core with all components"""
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Initialize components
        self.model_manager = ModelManager(models_dir)
        self.memory_controller = MemoryController(
            target_percent=self.config.get("target_memory_percent", 60.0)
        )
        self.task_router = TaskRouter(self.model_manager, self.memory_controller)
        
        # Conversation context
        self.conversation_history: List[Dict[str, str]] = []
        self.max_history = self.config.get("max_history", 10)
        
        # Performance tracking
        self.session_stats = {
            "start_time": datetime.now(),
            "total_queries": 0,
            "model_switches": 0,
            "memory_optimizations": 0,
            "errors": 0
        }
        
        # Setup memory pressure callbacks
        self._setup_memory_callbacks()
        
        # Start monitoring
        asyncio.create_task(self._initialize_async())
        
    async def _initialize_async(self):
        """Async initialization tasks"""
        # Start memory monitoring
        await self.memory_controller.start_monitoring()
        
        # Ensure tiny model is loaded
        logger.info("JARVIS Core initialized and ready")
        
    def _load_config(self, config_path: Optional[str]) -> Dict[str, Any]:
        """Load configuration from file or use defaults"""
        defaults = {
            "target_memory_percent": 60.0,
            "max_history": 10,
            "auto_optimize_memory": True,
            "predictive_loading": True,
            "quality_vs_speed": "balanced"  # "quality", "balanced", "speed"
        }
        
        if config_path and Path(config_path).exists():
            try:
                with open(config_path) as f:
                    loaded = json.load(f)
                    defaults.update(loaded)
            except Exception as e:
                logger.error(f"Failed to load config: {e}")
                
        return defaults
        
    def _setup_memory_callbacks(self):
        """Setup callbacks for memory pressure changes"""
        
        async def on_high_pressure(snapshot):
            """Handle high memory pressure"""
            logger.warning(f"High memory pressure: {snapshot.percent_used:.1f}%")
            
            # Unload advanced model if loaded
            if ModelTier.ADVANCED in self.model_manager.loaded_models:
                await self.model_manager.unload_model(ModelTier.ADVANCED)
                
        async def on_critical_pressure(snapshot):
            """Handle critical memory pressure"""
            logger.error(f"Critical memory pressure: {snapshot.percent_used:.1f}%")
            
            # Keep only tiny model
            for tier in [ModelTier.ADVANCED, ModelTier.STANDARD]:
                if tier in self.model_manager.loaded_models:
                    await self.model_manager.unload_model(tier)
                    
            # Force memory optimization
            if self.config.get("auto_optimize_memory", True):
                await self.memory_controller.optimize_memory(aggressive=True)
                self.session_stats["memory_optimizations"] += 1
                
        # Register callbacks
        self.memory_controller.register_pressure_callback(
            MemoryPressure.HIGH, on_high_pressure
        )
        self.memory_controller.register_pressure_callback(
            MemoryPressure.CRITICAL, on_critical_pressure
        )
        
    async def process_query(self, query: str, **kwargs) -> Dict[str, Any]:
        """
        Process a query through the intelligent routing system
        
        Args:
            query: User query
            **kwargs: Additional parameters (streaming, max_tokens, etc.)
            
        Returns:
            Response dictionary with result and metadata
        """
        start_time = datetime.now()
        self.session_stats["total_queries"] += 1
        
        try:
            # Get conversation context
            context = self._get_context()
            
            # Route to appropriate model
            model, routing_info = await self.task_router.route_task(query, context)
            
            # Track model switches
            if hasattr(self, '_last_model_tier') and self._last_model_tier != routing_info["model_tier"]:
                self.session_stats["model_switches"] += 1
            self._last_model_tier = routing_info["model_tier"]
            
            # Process with selected model
            response_text = await self._generate_response(model, query, context, **kwargs)
            
            # Update conversation history
            self._update_history(query, response_text)
            
            # Calculate processing time
            processing_time = (datetime.now() - start_time).total_seconds()
            
            # Build response
            response = {
                "response": response_text,
                "metadata": {
                    "model_tier": routing_info["model_tier"],
                    "task_analysis": {
                        "type": routing_info["analysis"].task_type.value,
                        "complexity": routing_info["analysis"].complexity,
                        "confidence": routing_info["analysis"].confidence,
                        "reasoning": routing_info["analysis"].reasoning
                    },
                    "memory_state": routing_info["memory_state"],
                    "processing_time": processing_time,
                    "routing_time": routing_info["routing_time"]
                },
                "success": True
            }
            
            # Log performance
            logger.info(f"Query processed in {processing_time:.2f}s using {routing_info['model_tier']} model")
            
            return response
            
        except Exception as e:
            logger.error(f"Error processing query: {e}")
            self.session_stats["errors"] += 1
            
            return {
                "response": "I encountered an error processing your request. Please try again.",
                "error": str(e),
                "success": False
            }
            
    async def _generate_response(self, model: Any, query: str,
                               context: List[str], **kwargs) -> str:
        """
        v84.0: Generate response using intelligent routing.

        Priority:
        1. J-Prime (if available and query is within capability)
        2. Local models (based on complexity)
        3. Cloud fallback (for complex queries)
        """
        # Build prompt with context
        prompt = self._build_prompt(query, context)

        # Get generation parameters
        max_tokens = kwargs.get("max_tokens", 256)
        temperature = kwargs.get("temperature", 0.7)

        # v84.0: Use intelligent routing
        llm_router = get_llm_router()

        # Get current memory pressure
        memory_stats = self.memory_controller.get_memory_stats()
        memory_pressure = MemoryPressure.NORMAL
        if memory_stats["current"]["percent_used"] > 85:
            memory_pressure = MemoryPressure.CRITICAL
        elif memory_stats["current"]["percent_used"] > 70:
            memory_pressure = MemoryPressure.HIGH

        # Route the query
        route = await llm_router.route(query, context, memory_pressure)
        start_time = time.time()

        logger.info(f"[JARVISCore] Routing to {route.backend.name}: {route.reason}")

        # Execute based on route
        response = None
        backends_tried = []

        # Build execution chain (primary + fallbacks)
        execution_chain = [route.backend] + route.fallback_chain

        for backend in execution_chain:
            backends_tried.append(backend.name)
            try:
                if backend == LLMBackend.JPRIME:
                    response = await self._execute_jprime(prompt, max_tokens, temperature)
                elif backend == LLMBackend.LOCAL:
                    response = await self._execute_local(model, prompt, max_tokens, temperature)
                elif backend == LLMBackend.CLOUD_GCP:
                    response = await self._execute_cloud_gcp(prompt, max_tokens, temperature)

                if response:
                    # Track latency for adaptive routing
                    latency_ms = (time.time() - start_time) * 1000
                    llm_router.update_latency(backend, latency_ms)

                    logger.info(
                        f"[JARVISCore] Response from {backend.name} "
                        f"(latency={latency_ms:.0f}ms, tried={backends_tried})"
                    )
                    return response

            except Exception as e:
                logger.warning(f"[JARVISCore] {backend.name} failed: {e}")
                continue

        # All backends failed
        raise RuntimeError(f"All LLM backends failed: {backends_tried}")

    async def _execute_jprime(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Execute query via J-Prime."""
        try:
            from backend.clients.jarvis_prime_client import get_jarvis_prime_client

            client = await get_jarvis_prime_client()
            if not client or not client.is_online:
                return None

            result = await client.inference(
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            )

            if result and result.text:
                return result.text

            return None

        except Exception as e:
            logger.debug(f"[JARVISCore] J-Prime execution failed: {e}")
            return None

    async def _execute_local(
        self,
        model: Any,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Execute query via local model."""
        try:
            response = await asyncio.to_thread(
                model,
                prompt,
                max_tokens=max_tokens,
                temperature=temperature
            )
            return response
        except Exception as e:
            logger.debug(f"[JARVISCore] Local execution failed: {e}")
            return None

    async def _execute_cloud_gcp(
        self,
        prompt: str,
        max_tokens: int,
        temperature: float,
    ) -> Optional[str]:
        """Execute query via GCP Cloud (placeholder for actual implementation)."""
        # TODO: Implement actual GCP Cloud integration
        # For now, return None to fall through to fallbacks
        logger.debug("[JARVISCore] GCP Cloud not yet implemented")
        return None
            
    def _build_prompt(self, query: str, context: List[str]) -> str:
        """Build prompt with context"""
        if not context:
            return query
            
        # Simple context injection
        context_str = "\n".join(context[-3:])  # Use last 3 exchanges
        return f"Context:\n{context_str}\n\nCurrent query: {query}"
        
    def _get_context(self) -> List[str]:
        """Get relevant conversation context"""
        context = []
        for entry in self.conversation_history[-3:]:  # Last 3 exchanges
            context.append(f"User: {entry['user']}")
            context.append(f"Assistant: {entry['assistant']}")
        return context
        
    def _update_history(self, query: str, response: str):
        """Update conversation history"""
        self.conversation_history.append({
            "user": query,
            "assistant": response,
            "timestamp": datetime.now().isoformat()
        })
        
        # Trim history if needed
        if len(self.conversation_history) > self.max_history:
            self.conversation_history = self.conversation_history[-self.max_history:]
            
    async def optimize_system(self) -> Dict[str, Any]:
        """Optimize system performance based on current state"""
        optimization_results = {
            "memory_optimization": None,
            "model_optimization": None,
            "suggestions": []
        }
        
        # Memory optimization
        memory_stats = self.memory_controller.get_memory_stats()
        if memory_stats["current"]["percent_used"] > 70:
            optimization_results["memory_optimization"] = await self.memory_controller.optimize_memory()
            
        # Model optimization based on workload
        recent_tasks = [
            {"type": h.get("task_type", "chat")} 
            for h in self.conversation_history[-10:]
        ]
        await self.model_manager.optimize_for_workload(recent_tasks)
        
        # Get optimization suggestions
        routing_suggestions = self.task_router.suggest_optimization()
        optimization_results["suggestions"].extend(routing_suggestions)
        
        return optimization_results
        
    def get_system_status(self) -> Dict[str, Any]:
        """Get comprehensive system status"""
        return {
            "core": {
                "uptime": (datetime.now() - self.session_stats["start_time"]).total_seconds(),
                "total_queries": self.session_stats["total_queries"],
                "model_switches": self.session_stats["model_switches"],
                "memory_optimizations": self.session_stats["memory_optimizations"],
                "errors": self.session_stats["errors"]
            },
            "models": self.model_manager.get_model_stats(),
            "memory": self.memory_controller.get_memory_stats(),
            "routing": self.task_router.get_routing_stats(),
            "config": self.config
        }
        
    async def shutdown(self):
        """Gracefully shutdown JARVIS Core"""
        logger.info("Shutting down JARVIS Core...")
        
        # Stop memory monitoring
        await self.memory_controller.stop_monitoring()
        
        # Unload all models except tiny
        for tier in [ModelTier.ADVANCED, ModelTier.STANDARD]:
            if tier in self.model_manager.loaded_models:
                await self.model_manager.unload_model(tier)
                
        logger.info("JARVIS Core shutdown complete")
        

class JARVISAssistant:
    """High-level assistant interface for JARVIS Core"""
    
    def __init__(self, core: Optional[JARVISCore] = None):
        self.core = core or JARVISCore()
        
    async def chat(self, message: str, **kwargs) -> str:
        """Simple chat interface"""
        response = await self.core.process_query(message, **kwargs)
        return response["response"]
        
    async def chat_with_info(self, message: str, **kwargs) -> Dict[str, Any]:
        """Chat with full metadata"""
        return await self.core.process_query(message, **kwargs)
        
    def get_status(self) -> Dict[str, Any]:
        """Get system status"""
        return self.core.get_system_status()
        
    async def optimize(self) -> Dict[str, Any]:
        """Run system optimization"""
        return await self.core.optimize_system()