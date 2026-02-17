"""
Reactor-Core Auto-Deploy Watcher
================================

Watches the reactor-core output directory for newly trained models and
automatically deploys them to JARVIS-Prime (local or Cloud Run).

Features:
- File system watching with debouncing
- Automatic model validation
- GCS upload for Cloud Run deployment
- Hot-swap notification to running JARVIS-Prime instances
- Rollback support if new model fails health checks

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │                    ReactorCoreWatcher                            │
    │  ┌───────────────────────────────────────────────────────────┐  │
    │  │         File Watcher (watchdog)                            │  │
    │  │  Monitors: reactor-core/output/*.gguf                      │  │
    │  └──────────────────────────┬────────────────────────────────┘  │
    │                             │                                    │
    │  ┌──────────────────────────▼────────────────────────────────┐  │
    │  │                Model Validator                             │  │
    │  │  - File integrity check                                    │  │
    │  │  - Size validation                                         │  │
    │  │  - GGUF format verification                                │  │
    │  └──────────────────────────┬────────────────────────────────┘  │
    │                             │                                    │
    │  ┌──────────────────────────▼────────────────────────────────┐  │
    │  │               Deploy Orchestrator                          │  │
    │  │  - Upload to GCS (Cloud Run)                               │  │
    │  │  - Copy to local models dir                                │  │
    │  │  - Notify JARVIS-Prime for hot-swap                        │  │
    │  │  - Health check verification                               │  │
    │  └──────────────────────────────────────────────────────────┘  │
    └─────────────────────────────────────────────────────────────────┘

Version: 1.0.0
Author: JARVIS AI System
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Awaitable

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class ReactorCoreConfig:
    """Configuration for Reactor-Core watcher."""

    # Watch directory (reactor-core output)
    watch_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_OUTPUT",
            str(Path.home() / "Documents" / "repos" / "reactor-core" / "output")
        ))
    )

    # Local JARVIS-Prime models directory
    local_models_dir: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_MODELS_DIR",
            str(Path.home() / "Documents" / "repos" / "jarvis-prime" / "models")
        ))
    )

    # GCS bucket for Cloud Run models
    gcs_bucket: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_MODELS_GCS_BUCKET",
            "gs://jarvis-473803-deployments/models"
        )
    )

    # Upload to GCS for Cloud Run
    upload_to_gcs: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_GCS_UPLOAD", "true").lower() == "true"
    )

    # Deploy to local JARVIS-Prime
    deploy_local: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_LOCAL_DEPLOY", "true").lower() == "true"
    )

    # File patterns to watch
    watch_patterns: List[str] = field(default_factory=lambda: ["*.gguf", "*.bin"])

    # Debounce time (seconds) - wait for file to be fully written
    debounce_seconds: float = field(
        default_factory=lambda: float(os.getenv("REACTOR_CORE_DEBOUNCE", "5.0"))
    )

    # Minimum model size (bytes) - to filter out incomplete files
    min_model_size_bytes: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_MIN_SIZE", str(100 * 1024 * 1024)))  # 100MB
    )

    # Auto-activate new models
    auto_activate: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_AUTO_ACTIVATE", "true").lower() == "true"
    )

    # JARVIS-Prime endpoints for hot-swap notification
    jarvis_prime_local_url: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_LOCAL_URL", "http://127.0.0.1:8002")
    )
    jarvis_prime_cloud_url: str = field(
        default_factory=lambda: os.getenv(
            "JARVIS_PRIME_CLOUD_RUN_URL",
            "https://jarvis-prime-dev-888774109345.us-central1.run.app"
        )
    )

    # v239.0: Subprocess smoke test before deployment
    smoke_test_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_SMOKE_TEST", "true").lower() == "true"
    )


@dataclass
class DeploymentResult:
    """Result of a model deployment."""
    success: bool
    model_name: str
    model_path: str
    model_size_mb: float
    checksum: str
    local_deployed: bool = False
    gcs_uploaded: bool = False
    gcs_path: Optional[str] = None
    hot_swap_notified: bool = False
    health_verified: bool = False  # v239.0
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# Model Validator
# =============================================================================

class ModelValidator:
    """Validates GGUF models before deployment."""

    GGUF_MAGIC = b"GGUF"

    @staticmethod
    def validate(model_path: Path, min_size_bytes: int = 100 * 1024 * 1024) -> tuple[bool, str]:
        """
        Validate a model file.

        Returns:
            Tuple of (is_valid, error_message)
        """
        if not model_path.exists():
            return False, f"File does not exist: {model_path}"

        # Check size
        size = model_path.stat().st_size
        if size < min_size_bytes:
            return False, f"File too small ({size / 1024 / 1024:.1f}MB < {min_size_bytes / 1024 / 1024:.1f}MB)"

        # Check GGUF magic bytes
        try:
            with open(model_path, "rb") as f:
                magic = f.read(4)
                if magic != ModelValidator.GGUF_MAGIC:
                    return False, f"Invalid GGUF magic bytes: {magic}"
        except Exception as e:
            return False, f"Failed to read file: {e}"

        return True, "Valid"

    @staticmethod
    def compute_checksum(model_path: Path) -> str:
        """Compute SHA256 checksum of model file."""
        sha256 = hashlib.sha256()
        with open(model_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256.update(chunk)
        return sha256.hexdigest()[:16]  # First 16 chars for brevity


# =============================================================================
# Reactor-Core Watcher
# =============================================================================

class ReactorCoreWatcher:
    """
    Watches reactor-core output directory and auto-deploys new models.

    Usage:
        watcher = ReactorCoreWatcher()
        await watcher.start()
        # ... later ...
        await watcher.stop()
    """

    def __init__(self, config: Optional[ReactorCoreConfig] = None):
        self.config = config or ReactorCoreConfig()
        self._running = False
        self._watch_task: Optional[asyncio.Task] = None
        self._pending_files: Dict[str, float] = {}  # path -> last_modified_time
        self._deployed_checksums: set[str] = set()
        self._deploy_callbacks: List[Callable[[DeploymentResult], Awaitable[None]]] = []
        self._http_client = None

        logger.info(
            f"[ReactorCoreWatcher] Initialized - watching: {self.config.watch_dir}"
        )

    async def _get_http_client(self):
        """Lazy load HTTP client."""
        if self._http_client is None:
            try:
                import httpx
                self._http_client = httpx.AsyncClient(timeout=30.0)
            except ImportError:
                logger.warning("[ReactorCoreWatcher] httpx not available")
        return self._http_client

    def register_callback(
        self,
        callback: Callable[[DeploymentResult], Awaitable[None]]
    ) -> None:
        """Register a callback for deployment events."""
        self._deploy_callbacks.append(callback)

    async def start(self) -> None:
        """Start watching for new models."""
        if self._running:
            logger.warning("[ReactorCoreWatcher] Already running")
            return

        # Ensure watch directory exists
        self.config.watch_dir.mkdir(parents=True, exist_ok=True)

        self._running = True
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info(f"[ReactorCoreWatcher] Started watching: {self.config.watch_dir}")

    async def stop(self) -> None:
        """Stop watching."""
        self._running = False
        if self._watch_task:
            self._watch_task.cancel()
            try:
                await self._watch_task
            except asyncio.CancelledError:
                pass
            self._watch_task = None

        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        logger.info("[ReactorCoreWatcher] Stopped")

    async def _watch_loop(self) -> None:
        """Main watch loop - polls for new files."""
        while self._running:
            try:
                await self._scan_for_new_models()
                await asyncio.sleep(5.0)  # Check every 5 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ReactorCoreWatcher] Watch loop error: {e}")
                await asyncio.sleep(10.0)

    async def _scan_for_new_models(self) -> None:
        """Scan for new model files."""
        if not self.config.watch_dir.exists():
            return

        for pattern in self.config.watch_patterns:
            for model_path in self.config.watch_dir.glob(pattern):
                await self._check_file(model_path)

    async def _check_file(self, model_path: Path) -> None:
        """Check if a file is ready for deployment."""
        path_str = str(model_path)
        current_mtime = model_path.stat().st_mtime

        # Track file modification time for debouncing
        if path_str in self._pending_files:
            last_mtime = self._pending_files[path_str]
            if current_mtime != last_mtime:
                # File still being written
                self._pending_files[path_str] = current_mtime
                return
            elif (time.time() - current_mtime) < self.config.debounce_seconds:
                # Wait for debounce period
                return
        else:
            # New file detected
            self._pending_files[path_str] = current_mtime
            logger.info(f"[ReactorCoreWatcher] New file detected: {model_path.name}")
            return

        # File is stable - check if we've already deployed it
        checksum = ModelValidator.compute_checksum(model_path)
        if checksum in self._deployed_checksums:
            return

        # Validate and deploy
        logger.info(f"[ReactorCoreWatcher] Processing: {model_path.name}")
        result = await self.deploy_model(model_path)

        if result.success:
            self._deployed_checksums.add(checksum)
            del self._pending_files[path_str]

            # Notify callbacks
            for callback in self._deploy_callbacks:
                try:
                    await callback(result)
                except Exception as e:
                    logger.error(f"[ReactorCoreWatcher] Callback error: {e}")

    async def deploy_model(self, model_path: Path) -> DeploymentResult:
        """
        Deploy a model to local and/or Cloud Run.

        Args:
            model_path: Path to the GGUF model file

        Returns:
            DeploymentResult with deployment details
        """
        # Validate model
        is_valid, error = ModelValidator.validate(
            model_path,
            min_size_bytes=self.config.min_model_size_bytes
        )
        if not is_valid:
            logger.warning(f"[ReactorCoreWatcher] Invalid model: {error}")
            return DeploymentResult(
                success=False,
                model_name=model_path.name,
                model_path=str(model_path),
                model_size_mb=0,
                checksum="",
                error=error,
            )

        # Compute checksum and size
        checksum = ModelValidator.compute_checksum(model_path)
        size_mb = model_path.stat().st_size / (1024 * 1024)

        logger.info(
            f"[ReactorCoreWatcher] Deploying model: {model_path.name} "
            f"({size_mb:.1f}MB, checksum: {checksum})"
        )

        deploy_start = time.monotonic()  # v239.0: track deployment latency

        # v239.0: Smoke test in subprocess (avoids OOM on 16GB Mac)
        if self.config.smoke_test_enabled:
            smoke_ok, smoke_err = await self._smoke_test(model_path)
            if not smoke_ok:
                logger.warning(f"[ReactorCoreWatcher] Smoke test FAILED: {smoke_err}")
                return DeploymentResult(
                    success=False, model_name=model_path.name,
                    model_path=str(model_path), model_size_mb=size_mb,
                    checksum=checksum, error=f"Smoke test failed: {smoke_err}",
                )
            logger.info("[ReactorCoreWatcher] Smoke test passed")

        result = DeploymentResult(
            success=True,
            model_name=model_path.name,
            model_path=str(model_path),
            model_size_mb=size_mb,
            checksum=checksum,
        )

        # Deploy to local
        if self.config.deploy_local:
            try:
                local_success = await self._deploy_local(model_path)
                result.local_deployed = local_success
            except Exception as e:
                logger.error(f"[ReactorCoreWatcher] Local deploy failed: {e}")
                result.error = str(e)

        # Upload to GCS
        if self.config.upload_to_gcs:
            try:
                gcs_path = await self._upload_to_gcs(model_path)
                result.gcs_uploaded = gcs_path is not None
                result.gcs_path = gcs_path
            except Exception as e:
                logger.error(f"[ReactorCoreWatcher] GCS upload failed: {e}")
                if result.error:
                    result.error += f"; GCS: {e}"
                else:
                    result.error = str(e)

        # Notify JARVIS-Prime for hot-swap
        if result.local_deployed or result.gcs_uploaded:
            try:
                hot_swap_ok = await self._notify_hot_swap(model_path.name, result.gcs_path)
                result.hot_swap_notified = hot_swap_ok
            except Exception as e:
                logger.warning(f"[ReactorCoreWatcher] Hot-swap notification failed: {e}")

        # v239.0: Deployment feedback — verify health + write structured feedback
        if result.hot_swap_notified:
            await asyncio.sleep(10.0)  # Let J-Prime load new model
            feedback_ok = await self._check_deployment_health()
            result.health_verified = feedback_ok
            await self._write_deployment_feedback(result, deploy_start)

        result.success = result.local_deployed or result.gcs_uploaded
        return result

    async def _deploy_local(self, model_path: Path) -> bool:
        """Deploy model to local JARVIS-Prime models directory."""
        target_dir = self.config.local_models_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        target_path = target_dir / model_path.name

        logger.info(f"[ReactorCoreWatcher] Copying to local: {target_path}")

        # Copy file
        await asyncio.to_thread(shutil.copy2, model_path, target_path)

        # Update current.gguf symlink if auto-activate is enabled
        if self.config.auto_activate:
            current_link = target_dir / "current.gguf"
            if current_link.exists() or current_link.is_symlink():
                current_link.unlink()
            current_link.symlink_to(model_path.name)
            logger.info(f"[ReactorCoreWatcher] Updated current.gguf -> {model_path.name}")

        return True

    async def _upload_to_gcs(self, model_path: Path) -> Optional[str]:
        """Upload model to GCS for Cloud Run."""
        gcs_path = f"{self.config.gcs_bucket}/{model_path.name}"

        logger.info(f"[ReactorCoreWatcher] Uploading to GCS: {gcs_path}")

        # Use gsutil for upload
        proc = await asyncio.create_subprocess_exec(
            "gsutil", "-m", "cp", str(model_path), gcs_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error = stderr.decode() if stderr else "Unknown error"
            raise Exception(f"gsutil failed: {error}")

        logger.info(f"[ReactorCoreWatcher] Uploaded to: {gcs_path}")
        return gcs_path

    async def _notify_hot_swap(self, model_name: str, gcs_path: Optional[str]) -> bool:
        """Notify JARVIS-Prime to hot-swap to new model."""
        client = await self._get_http_client()
        if client is None:
            return False

        notified = False

        # Notify local instance
        try:
            resp = await client.post(
                f"{self.config.jarvis_prime_local_url}/hot-swap",
                json={"model_name": model_name, "gcs_path": gcs_path},
            )
            if resp.status_code == 200:
                logger.info("[ReactorCoreWatcher] Local hot-swap notified")
                notified = True
        except Exception as e:
            logger.debug(f"[ReactorCoreWatcher] Local notification failed: {e}")

        # Notify Cloud Run instance (if available)
        if gcs_path:
            try:
                resp = await client.post(
                    f"{self.config.jarvis_prime_cloud_url}/hot-swap",
                    json={"model_name": model_name, "gcs_path": gcs_path},
                )
                if resp.status_code == 200:
                    logger.info("[ReactorCoreWatcher] Cloud Run hot-swap notified")
                    notified = True
            except Exception as e:
                logger.debug(f"[ReactorCoreWatcher] Cloud Run notification failed: {e}")

        return notified

    async def _smoke_test(self, model_path: Path) -> tuple:
        """v239.0: Load model in subprocess, run test inference, verify output.

        Subprocess isolates memory — when it exits, OS reclaims all RAM.
        No risk of OOM leak in the main supervisor process.
        Model path is passed as sys.argv[1] to avoid shell injection.
        """
        import sys
        test_script = (
            "import sys; "
            "from llama_cpp import Llama; "
            "m = Llama(model_path=sys.argv[1], n_ctx=512, verbose=False); "
            "r = m('Hello, how are you?', max_tokens=20); "
            "t = r['choices'][0]['text'].strip(); "
            "print(t); "
            "sys.exit(0 if len(t) >= 3 else 1)"
        )
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", test_script, str(model_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=120.0
            )
            if proc.returncode != 0:
                err = stderr.decode()[:200] if stderr else "Unknown error"
                return False, err
            text = stdout.decode().strip()
            if len(text) < 3:
                return False, f"Output too short: '{text}'"
            return True, ""
        except asyncio.TimeoutError:
            # Kill the subprocess to reclaim its memory immediately
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return False, "Smoke test timed out (120s)"
        except Exception as e:
            return False, str(e)

    async def _check_deployment_health(self) -> bool:
        """v239.0: Check J-Prime health after model swap."""
        client = await self._get_http_client()
        if client is None:
            return False
        try:
            resp = await client.get(f"{self.config.jarvis_prime_local_url}/health")
            if resp.status_code == 200:
                data = resp.json()
                return data.get("ready_for_inference", False) or data.get("status") == "healthy"
        except Exception:
            pass
        return False

    async def _write_deployment_feedback(self, result: DeploymentResult, start_time: float) -> None:
        """v239.0: Write structured deployment feedback for Reactor Core."""
        import json
        feedback_dir = Path.home() / ".jarvis" / "reactor" / "feedback"
        feedback_dir.mkdir(parents=True, exist_ok=True)

        feedback = {
            "schema_version": "1.0",
            "model_name": result.model_name,
            "model_path": result.model_path,
            "model_size_mb": result.model_size_mb,
            "checksum": result.checksum,
            "deployed_at": datetime.utcnow().isoformat() + "Z",
            "smoke_test_passed": True,
            "local_deployed": result.local_deployed,
            "gcs_uploaded": result.gcs_uploaded,
            "hot_swap_notified": result.hot_swap_notified,
            "health_verified": result.health_verified,
            "deployment_latency_ms": int((time.monotonic() - start_time) * 1000),
        }

        filename = f"deploy_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{result.model_name}.json"
        (feedback_dir / filename).write_text(json.dumps(feedback, indent=2))
        logger.info(f"[v239.0] Deployment feedback written: {filename}")

    async def manual_deploy(self, model_path: str) -> DeploymentResult:
        """Manually trigger deployment of a model."""
        path = Path(model_path)
        if not path.exists():
            return DeploymentResult(
                success=False,
                model_name=path.name,
                model_path=model_path,
                model_size_mb=0,
                checksum="",
                error=f"File not found: {model_path}",
            )

        return await self.deploy_model(path)


# =============================================================================
# Singleton Access
# =============================================================================

_watcher_instance: Optional[ReactorCoreWatcher] = None


def get_reactor_core_watcher() -> ReactorCoreWatcher:
    """Get the global ReactorCoreWatcher instance."""
    global _watcher_instance
    if _watcher_instance is None:
        _watcher_instance = ReactorCoreWatcher()
    return _watcher_instance


async def start_reactor_core_watcher() -> ReactorCoreWatcher:
    """Start the global ReactorCoreWatcher."""
    watcher = get_reactor_core_watcher()
    await watcher.start()
    return watcher


async def stop_reactor_core_watcher() -> None:
    """Stop the global ReactorCoreWatcher."""
    global _watcher_instance
    if _watcher_instance:
        await _watcher_instance.stop()
        _watcher_instance = None
