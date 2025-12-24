"""
Unified Data Flywheel Orchestrator
===================================

Connects JARVIS-AI-Agent, JARVIS-Prime, and reactor-core into a
continuous self-improving learning loop.

Architecture:
    ┌─────────────────────────────────────────────────────────────────────────┐
    │                     JARVIS UNIFIED DATA FLYWHEEL                         │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                        DATA SOURCES                                  ││
    │  │  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ││
    │  │  │  JARVISConnector │  │  SafeScout       │  │  ChromaDB Memory │  ││
    │  │  │  (Experiences)   │  │  (Web Docs)      │  │  (Context)       │  ││
    │  │  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  ││
    │  └───────────┼─────────────────────┼─────────────────────┼────────────┘│
    │              │                     │                     │              │
    │              └─────────────────────┼─────────────────────┘              │
    │                                    ▼                                    │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      DATASET BUILDER                                ││
    │  │  - PII Anonymization                                                ││
    │  │  - Format Conversion (ChatML/Alpaca)                                ││
    │  │  - Quality Filtering                                                ││
    │  │  - Deduplication                                                    ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      ASYNC TRAINER                                   ││
    │  │  - LoRA/QLoRA Fine-tuning                                           ││
    │  │  - Memory-Efficient Training                                        ││
    │  │  - Progress Tracking                                                ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                      GGUF EXPORTER                                   ││
    │  │  - Quantization (Q4_K_M, Q5_K_M, Q8_0)                              ││
    │  │  - Optimization for llama.cpp                                       ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                  REACTOR-CORE WATCHER                                ││
    │  │  - Auto-deploy to local JARVIS-Prime                                ││
    │  │  - Upload to GCS for Cloud Run                                      ││
    │  │  - Hot-swap notification                                            ││
    │  └───────────────────────────────┬─────────────────────────────────────┘│
    │                                  ▼                                      │
    │  ┌─────────────────────────────────────────────────────────────────────┐│
    │  │                    OBSERVABILITY HUB                                 ││
    │  │  - Langfuse Tracing                                                 ││
    │  │  - Helicone Cost Tracking                                           ││
    │  │  - Performance Metrics                                              ││
    │  └─────────────────────────────────────────────────────────────────────┘│
    └─────────────────────────────────────────────────────────────────────────┘

Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

class FlywheelStage(Enum):
    """Stages of the data flywheel."""
    IDLE = "idle"
    COLLECTING_EXPERIENCES = "collecting_experiences"
    COLLECTING_WEB_DATA = "collecting_web_data"
    BUILDING_DATASET = "building_dataset"
    TRAINING = "training"
    EXPORTING_GGUF = "exporting_gguf"
    DEPLOYING = "deploying"
    COMPLETED = "completed"
    FAILED = "failed"


class DataSourceType(Enum):
    """Types of data sources."""
    JARVIS_EXPERIENCES = "jarvis_experiences"
    WEB_DOCUMENTATION = "web_documentation"
    CHROMADB_MEMORY = "chromadb_memory"
    MANUAL_UPLOAD = "manual_upload"


@dataclass
class FlywheelConfig:
    """Configuration for the unified data flywheel."""

    # Repository paths
    jarvis_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_AI_AGENT_PATH",
            Path.home() / "Documents" / "repos" / "JARVIS-AI-Agent"
        ))
    )
    jarvis_prime_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            Path.home() / "Documents" / "repos" / "jarvis-prime"
        ))
    )
    reactor_core_repo: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            Path.home() / "Documents" / "repos" / "reactor-core"
        ))
    )

    # Data collection settings
    experience_lookback_hours: int = 24
    min_experiences_for_training: int = 100
    min_web_examples_for_training: int = 50

    # Training triggers
    auto_train_enabled: bool = field(
        default_factory=lambda: os.getenv("FLYWHEEL_AUTO_TRAIN", "true").lower() == "true"
    )
    training_cooldown_hours: int = 24  # Minimum hours between auto-trainings

    # Scout settings
    scout_topics: List[str] = field(default_factory=lambda: [
        "Python asyncio best practices",
        "macOS automation AppleScript",
        "Voice authentication security",
        "LLM fine-tuning techniques",
        "LangChain agent patterns",
        "FastAPI async patterns",
    ])
    scout_max_pages_per_topic: int = 10

    # Training settings
    base_model: str = "meta-llama/Llama-3.2-3B"
    output_name: str = "jarvis-prime"
    quantization: str = "Q4_K_M"

    # GCS settings
    gcs_bucket: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_MODELS_GCS_BUCKET",
            "gs://jarvis-473803-deployments/models"
        )
    )


@dataclass
class FlywheelProgress:
    """Progress tracking for flywheel execution."""
    stage: FlywheelStage = FlywheelStage.IDLE
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    # Data collection stats
    experiences_collected: int = 0
    corrections_found: int = 0
    web_pages_scraped: int = 0
    examples_synthesized: int = 0

    # Dataset stats
    dataset_examples: int = 0
    dataset_size_mb: float = 0.0

    # Training stats
    training_epochs: int = 0
    current_epoch: int = 0
    current_loss: float = 0.0
    best_loss: float = float('inf')

    # Deployment stats
    model_size_mb: float = 0.0
    deployed_local: bool = False
    deployed_cloud: bool = False

    # Errors
    errors: List[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.completed_at or datetime.now()
        return (end - self.started_at).total_seconds()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage.value,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "experiences_collected": self.experiences_collected,
            "corrections_found": self.corrections_found,
            "web_pages_scraped": self.web_pages_scraped,
            "examples_synthesized": self.examples_synthesized,
            "dataset_examples": self.dataset_examples,
            "dataset_size_mb": self.dataset_size_mb,
            "training_epochs": self.training_epochs,
            "current_epoch": self.current_epoch,
            "current_loss": self.current_loss,
            "best_loss": self.best_loss,
            "model_size_mb": self.model_size_mb,
            "deployed_local": self.deployed_local,
            "deployed_cloud": self.deployed_cloud,
            "errors": self.errors[-10:],
        }


@dataclass
class FlywheelResult:
    """Result of a flywheel execution."""
    success: bool
    run_id: str
    progress: FlywheelProgress
    model_path: Optional[str] = None
    gcs_path: Optional[str] = None
    error: Optional[str] = None


# =============================================================================
# Unified Data Flywheel Orchestrator
# =============================================================================

class UnifiedDataFlywheel:
    """
    Orchestrates the complete data flywheel across all JARVIS repositories.

    Connects:
    - JARVIS-AI-Agent (experience telemetry, observability)
    - reactor-core (Scout scraping, training, GGUF export)
    - JARVIS-Prime (model deployment, inference)
    """

    def __init__(self, config: Optional[FlywheelConfig] = None):
        self.config = config or FlywheelConfig()
        self._progress = FlywheelProgress()
        self._running = False
        self._cancelled = False
        self._progress_callbacks: List[Callable[[FlywheelProgress], None]] = []

        # Components (lazy loaded)
        self._jarvis_connector = None
        self._scout = None
        self._trainer = None
        self._watcher = None
        self._observability = None

        # Last training timestamp
        self._last_training: Optional[datetime] = None

        logger.info("[UnifiedDataFlywheel] Initialized")

    @property
    def progress(self) -> FlywheelProgress:
        """Get current progress."""
        return self._progress

    @property
    def is_running(self) -> bool:
        """Check if flywheel is currently running."""
        return self._running

    def register_progress_callback(
        self,
        callback: Callable[[FlywheelProgress], None]
    ) -> None:
        """Register a callback for progress updates."""
        self._progress_callbacks.append(callback)

    def _notify_progress(self) -> None:
        """Notify all progress callbacks."""
        for callback in self._progress_callbacks:
            try:
                callback(self._progress)
            except Exception as e:
                logger.warning(f"Progress callback error: {e}")

    def _update_stage(self, stage: FlywheelStage) -> None:
        """Update current stage."""
        self._progress.stage = stage
        self._notify_progress()
        logger.info(f"[Flywheel] Stage: {stage.value}")

    async def _init_components(self) -> None:
        """Initialize all components lazily."""
        # Add reactor-core to path if not already
        reactor_core_path = str(self.config.reactor_core_repo)
        if reactor_core_path not in sys.path:
            sys.path.insert(0, reactor_core_path)

        # Import reactor-core components
        try:
            from reactor_core.integration.jarvis_connector import (
                JARVISConnector,
                JARVISConnectorConfig,
            )
            self._jarvis_connector = JARVISConnector(
                JARVISConnectorConfig(
                    jarvis_repo_path=self.config.jarvis_repo,
                    lookback_hours=self.config.experience_lookback_hours,
                )
            )
            logger.info("[Flywheel] JARVISConnector initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] JARVISConnector not available: {e}")

        try:
            from reactor_core.scout.safe_scout_orchestrator import (
                SafeScoutOrchestrator,
                ScoutConfig,
            )
            self._scout = SafeScoutOrchestrator(
                ScoutConfig(
                    work_dir=self.config.reactor_core_repo / "work",
                    max_pages_per_topic=self.config.scout_max_pages_per_topic,
                )
            )
            logger.info("[Flywheel] SafeScoutOrchestrator initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] SafeScoutOrchestrator not available: {e}")

        try:
            from reactor_core.training.trainer import AsyncTrainer, TrainingConfig
            self._trainer_class = AsyncTrainer
            self._training_config_class = TrainingConfig
            logger.info("[Flywheel] AsyncTrainer initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] AsyncTrainer not available: {e}")
            self._trainer_class = None

        # Import local components
        try:
            from backend.autonomy.reactor_core_watcher import get_reactor_core_watcher
            self._watcher = get_reactor_core_watcher()
            logger.info("[Flywheel] ReactorCoreWatcher initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] ReactorCoreWatcher not available: {e}")

        try:
            from backend.observability import get_observability_hub
            self._observability = get_observability_hub()
            logger.info("[Flywheel] ObservabilityHub initialized")
        except ImportError as e:
            logger.warning(f"[Flywheel] ObservabilityHub not available: {e}")

    async def _cleanup_components(self) -> None:
        """Cleanup all components."""
        if self._scout:
            try:
                await self._scout.cancel()
            except Exception:
                pass

        if self._watcher:
            try:
                await self._watcher.stop()
            except Exception:
                pass

    async def run_full_cycle(
        self,
        include_web_scraping: bool = True,
        include_training: bool = True,
        force: bool = False,
    ) -> FlywheelResult:
        """
        Run a full data flywheel cycle.

        Args:
            include_web_scraping: Whether to scrape web documentation
            include_training: Whether to run training after data collection
            force: Force training even if cooldown hasn't elapsed

        Returns:
            FlywheelResult with execution details
        """
        import uuid
        run_id = f"flywheel_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        if self._running:
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error="Flywheel already running",
            )

        # Check cooldown
        if (
            not force
            and self._last_training
            and (datetime.now() - self._last_training).total_seconds()
            < self.config.training_cooldown_hours * 3600
        ):
            hours_remaining = (
                self.config.training_cooldown_hours
                - (datetime.now() - self._last_training).total_seconds() / 3600
            )
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error=f"Training cooldown: {hours_remaining:.1f}h remaining",
            )

        self._running = True
        self._cancelled = False
        self._progress = FlywheelProgress(started_at=datetime.now())

        try:
            # Initialize components
            await self._init_components()

            # Start watcher for auto-deployment
            if self._watcher:
                await self._watcher.start()

            # Phase 1: Collect experiences from JARVIS
            self._update_stage(FlywheelStage.COLLECTING_EXPERIENCES)
            experiences = await self._collect_experiences()

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 2: Collect web documentation (optional)
            web_examples = []
            if include_web_scraping and self._scout:
                self._update_stage(FlywheelStage.COLLECTING_WEB_DATA)
                web_examples = await self._collect_web_data()

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 3: Build dataset
            self._update_stage(FlywheelStage.BUILDING_DATASET)
            dataset_path = await self._build_dataset(experiences, web_examples)

            if not dataset_path:
                self._update_stage(FlywheelStage.FAILED)
                return FlywheelResult(
                    success=False,
                    run_id=run_id,
                    progress=self._progress,
                    error="Failed to build dataset",
                )

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 4: Train model (optional)
            model_path = None
            if include_training and self._trainer_class:
                # Check if we have enough data
                if (
                    self._progress.experiences_collected >= self.config.min_experiences_for_training
                    or self._progress.examples_synthesized >= self.config.min_web_examples_for_training
                ):
                    self._update_stage(FlywheelStage.TRAINING)
                    model_path = await self._train_model(dataset_path)

                    if model_path:
                        self._last_training = datetime.now()
                else:
                    logger.info(
                        f"[Flywheel] Insufficient data for training: "
                        f"{self._progress.experiences_collected} experiences, "
                        f"{self._progress.examples_synthesized} web examples"
                    )

            if self._cancelled:
                return self._create_cancelled_result(run_id)

            # Phase 5: Export to GGUF and deploy
            gcs_path = None
            if model_path:
                self._update_stage(FlywheelStage.EXPORTING_GGUF)
                gguf_path = await self._export_gguf(model_path)

                if gguf_path:
                    self._update_stage(FlywheelStage.DEPLOYING)
                    gcs_path = await self._deploy_model(gguf_path)

            # Complete
            self._update_stage(FlywheelStage.COMPLETED)
            self._progress.completed_at = datetime.now()

            # Record to observability
            if self._observability:
                await self._record_flywheel_run(run_id)

            return FlywheelResult(
                success=True,
                run_id=run_id,
                progress=self._progress,
                model_path=model_path,
                gcs_path=gcs_path,
            )

        except Exception as e:
            logger.exception(f"[Flywheel] Error: {e}")
            self._progress.errors.append(str(e))
            self._update_stage(FlywheelStage.FAILED)
            return FlywheelResult(
                success=False,
                run_id=run_id,
                progress=self._progress,
                error=str(e),
            )
        finally:
            self._running = False
            await self._cleanup_components()

    def _create_cancelled_result(self, run_id: str) -> FlywheelResult:
        """Create a cancelled result."""
        self._progress.completed_at = datetime.now()
        return FlywheelResult(
            success=False,
            run_id=run_id,
            progress=self._progress,
            error="Cancelled by user",
        )

    async def cancel(self) -> None:
        """Cancel the current flywheel execution."""
        self._cancelled = True
        if self._scout:
            await self._scout.cancel()
        logger.info("[Flywheel] Cancellation requested")

    # =========================================================================
    # Phase Implementations
    # =========================================================================

    async def _collect_experiences(self) -> List[Dict[str, Any]]:
        """Collect experiences from JARVIS-AI-Agent logs."""
        if not self._jarvis_connector:
            logger.warning("[Flywheel] JARVISConnector not available")
            return []

        try:
            # Get successful interactions
            interactions = await self._jarvis_connector.get_successful_interactions(
                min_confidence=0.85,
            )
            self._progress.experiences_collected = len(interactions)

            # Get corrections (valuable for training)
            corrections = await self._jarvis_connector.get_corrections()
            self._progress.corrections_found = len(corrections)

            # Convert to training format
            examples = []
            for event in interactions + corrections:
                if event.user_input and event.jarvis_response:
                    examples.append({
                        "instruction": event.user_input,
                        "output": event.jarvis_response,
                        "context": event.system_context,
                        "is_correction": event.is_correction,
                        "confidence": event.confidence,
                    })

            logger.info(
                f"[Flywheel] Collected {len(interactions)} experiences, "
                f"{len(corrections)} corrections"
            )
            return examples

        except Exception as e:
            logger.error(f"[Flywheel] Experience collection failed: {e}")
            self._progress.errors.append(f"Experience collection: {e}")
            return []

    async def _collect_web_data(self) -> List[Dict[str, Any]]:
        """Collect documentation from web scraping."""
        if not self._scout:
            logger.warning("[Flywheel] Scout not available")
            return []

        try:
            # Add topics to queue
            for topic in self.config.scout_topics:
                await self._scout.add_topic(topic)

            # Run scraping
            results = await self._scout.run()

            self._progress.web_pages_scraped = results.pages_fetched
            self._progress.examples_synthesized = results.examples_synthesized

            # Get synthesized examples
            examples = []
            output_dir = self.config.reactor_core_repo / "work" / "output"
            if output_dir.exists():
                for json_file in output_dir.glob("*.json"):
                    try:
                        data = json.loads(json_file.read_text())
                        if isinstance(data, list):
                            examples.extend(data)
                        elif isinstance(data, dict) and "pairs" in data:
                            examples.extend(data["pairs"])
                    except Exception:
                        continue

            logger.info(
                f"[Flywheel] Scraped {results.pages_fetched} pages, "
                f"synthesized {len(examples)} examples"
            )
            return examples

        except Exception as e:
            logger.error(f"[Flywheel] Web scraping failed: {e}")
            self._progress.errors.append(f"Web scraping: {e}")
            return []

    async def _build_dataset(
        self,
        experiences: List[Dict[str, Any]],
        web_examples: List[Dict[str, Any]],
    ) -> Optional[Path]:
        """Build training dataset from collected data."""
        all_examples = experiences + web_examples

        if not all_examples:
            logger.warning("[Flywheel] No examples to build dataset from")
            return None

        # Output path
        output_dir = self.config.reactor_core_repo / "datasets"
        output_dir.mkdir(parents=True, exist_ok=True)

        dataset_path = output_dir / f"jarvis_dataset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

        # Convert to ChatML format and write
        with open(dataset_path, "w") as f:
            for example in all_examples:
                # Build ChatML conversation
                messages = []

                # Add system message if context exists
                if example.get("context"):
                    messages.append({
                        "role": "system",
                        "content": f"You are JARVIS, an intelligent AI assistant. Context: {example['context']}"
                    })
                else:
                    messages.append({
                        "role": "system",
                        "content": "You are JARVIS, an intelligent AI assistant."
                    })

                # Add user message
                messages.append({
                    "role": "user",
                    "content": example.get("instruction", example.get("input", ""))
                })

                # Add assistant response
                messages.append({
                    "role": "assistant",
                    "content": example.get("output", example.get("response", ""))
                })

                # Write as JSONL
                f.write(json.dumps({"messages": messages}) + "\n")

        # Update stats
        self._progress.dataset_examples = len(all_examples)
        self._progress.dataset_size_mb = dataset_path.stat().st_size / (1024 * 1024)

        logger.info(
            f"[Flywheel] Built dataset: {len(all_examples)} examples, "
            f"{self._progress.dataset_size_mb:.2f}MB"
        )
        return dataset_path

    async def _train_model(self, dataset_path: Path) -> Optional[str]:
        """Train the model using reactor-core's AsyncTrainer."""
        if not self._trainer_class:
            logger.warning("[Flywheel] AsyncTrainer not available")
            return None

        try:
            # Configure training
            config = self._training_config_class(
                model_name=self.config.base_model,
                num_epochs=3,
                batch_size=4,
                learning_rate=2e-5,
                use_lora=True,
                use_qlora=True,
                lora_rank=64,
            )

            # Create trainer
            trainer = self._trainer_class(config)

            # Register progress callback
            def on_progress(state):
                self._progress.current_epoch = state.get("epoch", 0)
                self._progress.current_loss = state.get("loss", 0.0)
                if state.get("loss", float('inf')) < self._progress.best_loss:
                    self._progress.best_loss = state["loss"]
                self._notify_progress()

            trainer.register_callback(on_progress)

            # Set epochs
            self._progress.training_epochs = config.num_epochs

            # Output directory
            output_dir = self.config.reactor_core_repo / "output"
            output_dir.mkdir(parents=True, exist_ok=True)

            output_name = f"{self.config.output_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

            # Run training
            result = await trainer.train(
                dataset_path=str(dataset_path),
                output_dir=str(output_dir),
                output_name=output_name,
            )

            if result.success:
                logger.info(f"[Flywheel] Training completed: {result.model_path}")
                return result.model_path
            else:
                logger.error(f"[Flywheel] Training failed: {result.error}")
                self._progress.errors.append(f"Training: {result.error}")
                return None

        except Exception as e:
            logger.error(f"[Flywheel] Training error: {e}")
            self._progress.errors.append(f"Training: {e}")
            return None

    async def _export_gguf(self, model_path: str) -> Optional[Path]:
        """Export trained model to GGUF format."""
        try:
            from reactor_core.export.gguf_exporter import GGUFExporter, ExportConfig

            exporter = GGUFExporter(
                ExportConfig(quantization=self.config.quantization)
            )

            output_dir = self.config.reactor_core_repo / "output"
            output_name = Path(model_path).stem

            result = await exporter.export(
                model_path=model_path,
                output_dir=str(output_dir),
                output_name=output_name,
            )

            if result.success:
                gguf_path = Path(result.gguf_path)
                self._progress.model_size_mb = gguf_path.stat().st_size / (1024 * 1024)
                logger.info(f"[Flywheel] GGUF exported: {gguf_path}")
                return gguf_path
            else:
                logger.error(f"[Flywheel] GGUF export failed: {result.error}")
                self._progress.errors.append(f"GGUF export: {result.error}")
                return None

        except ImportError:
            logger.warning("[Flywheel] GGUFExporter not available, skipping export")
            return None
        except Exception as e:
            logger.error(f"[Flywheel] GGUF export error: {e}")
            self._progress.errors.append(f"GGUF export: {e}")
            return None

    async def _deploy_model(self, gguf_path: Path) -> Optional[str]:
        """Deploy GGUF model using ReactorCoreWatcher."""
        if not self._watcher:
            logger.warning("[Flywheel] ReactorCoreWatcher not available")
            return None

        try:
            result = await self._watcher.deploy_model(gguf_path)

            self._progress.deployed_local = result.local_deployed
            self._progress.deployed_cloud = result.gcs_uploaded

            if result.success:
                logger.info(
                    f"[Flywheel] Model deployed: "
                    f"local={result.local_deployed}, cloud={result.gcs_uploaded}"
                )
                return result.gcs_path
            else:
                logger.error(f"[Flywheel] Deployment failed: {result.error}")
                self._progress.errors.append(f"Deployment: {result.error}")
                return None

        except Exception as e:
            logger.error(f"[Flywheel] Deployment error: {e}")
            self._progress.errors.append(f"Deployment: {e}")
            return None

    async def _record_flywheel_run(self, run_id: str) -> None:
        """Record flywheel run to observability hub."""
        if not self._observability:
            return

        try:
            await self._observability.record_event(
                event_type="flywheel_run",
                metadata={
                    "run_id": run_id,
                    "progress": self._progress.to_dict(),
                }
            )
        except Exception as e:
            logger.warning(f"[Flywheel] Failed to record to observability: {e}")

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def collect_only(self) -> FlywheelResult:
        """Run data collection only (no training)."""
        return await self.run_full_cycle(
            include_web_scraping=True,
            include_training=False,
        )

    async def train_from_experiences_only(self) -> FlywheelResult:
        """Train from experiences only (no web scraping)."""
        return await self.run_full_cycle(
            include_web_scraping=False,
            include_training=True,
        )

    async def quick_retrain(self) -> FlywheelResult:
        """Quick retrain using only recent experiences."""
        # Temporarily reduce lookback
        original_lookback = self.config.experience_lookback_hours
        self.config.experience_lookback_hours = 6

        try:
            return await self.run_full_cycle(
                include_web_scraping=False,
                include_training=True,
                force=True,
            )
        finally:
            self.config.experience_lookback_hours = original_lookback


# =============================================================================
# Singleton Access
# =============================================================================

_flywheel_instance: Optional[UnifiedDataFlywheel] = None


def get_data_flywheel() -> UnifiedDataFlywheel:
    """Get the global UnifiedDataFlywheel instance."""
    global _flywheel_instance
    if _flywheel_instance is None:
        _flywheel_instance = UnifiedDataFlywheel()
    return _flywheel_instance


async def run_flywheel_cycle(
    include_web_scraping: bool = True,
    include_training: bool = True,
    force: bool = False,
) -> FlywheelResult:
    """Run a data flywheel cycle using the global instance."""
    flywheel = get_data_flywheel()
    return await flywheel.run_full_cycle(
        include_web_scraping=include_web_scraping,
        include_training=include_training,
        force=force,
    )


async def get_flywheel_status() -> Dict[str, Any]:
    """Get current flywheel status."""
    flywheel = get_data_flywheel()
    return {
        "running": flywheel.is_running,
        "progress": flywheel.progress.to_dict(),
    }
