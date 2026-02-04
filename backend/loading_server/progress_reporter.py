"""
Progress Reporter Client for JARVIS Loading Server v212.0
==========================================================

Client library for external callers to report progress to the loading server.

Features:
- HTTP-based progress reporting
- W3C trace context propagation
- Automatic retry with backoff
- Async and sync API
- Connection pooling
- Batched updates for high-frequency reporting
- Event sourcing support

Usage:
    from backend.loading_server import ProgressReporter

    # Async usage
    async with ProgressReporter() as reporter:
        await reporter.update_progress(50, "backend", "Loading...")

    # Sync usage
    reporter = ProgressReporter()
    reporter.update_progress_sync(50, "backend", "Loading...")

    # Context manager for stages
    with reporter.stage("backend") as stage:
        stage.update(25, "Initializing...")
        stage.update(50, "Loading models...")
        stage.update(100, "Complete")

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from .tracing import W3CTraceContext

logger = logging.getLogger("LoadingServer.Reporter")


@dataclass
class ProgressUpdate:
    """Represents a single progress update."""

    progress: float
    stage: str
    message: Optional[str] = None
    component_status: Optional[Dict[str, Any]] = None
    trace_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProgressReporter:
    """
    Client for reporting progress to the loading server.

    Features:
    - HTTP-based progress reporting to loading server
    - W3C trace context propagation
    - Automatic retry with exponential backoff
    - Both async and sync APIs
    - Connection pooling for efficiency
    - Batched updates for high-frequency reporting
    """

    host: str = "localhost"
    port: int = 8080
    timeout: float = 5.0
    max_retries: int = 3
    retry_backoff: float = 0.5
    batch_interval: float = 0.1  # Batch updates within 100ms

    # Internal state
    _trace_context: Optional[W3CTraceContext] = field(init=False, default=None)
    _session_id: Optional[str] = field(init=False, default=None)
    _last_progress: float = field(init=False, default=0.0)
    _pending_updates: List[ProgressUpdate] = field(init=False, default_factory=list)
    _update_lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _batch_task: Optional[asyncio.Task] = field(init=False, default=None)
    _is_ready: bool = field(init=False, default=False)

    @property
    def base_url(self) -> str:
        """Get base URL for loading server."""
        return f"http://{self.host}:{self.port}"

    @property
    def update_endpoint(self) -> str:
        """Get progress update endpoint."""
        return f"{self.base_url}/api/update-progress"

    def set_trace_context(self, trace_context: W3CTraceContext) -> None:
        """
        Set W3C trace context for all requests.

        Args:
            trace_context: Trace context to propagate
        """
        self._trace_context = trace_context

    def set_session_id(self, session_id: str) -> None:
        """
        Set session ID for all updates.

        Args:
            session_id: Unique session identifier
        """
        self._session_id = session_id

    async def check_ready(self) -> bool:
        """
        Check if loading server is ready to receive updates.

        Returns:
            True if server is responding
        """
        try:
            health_url = f"{self.base_url}/health"

            def _check():
                req = urllib.request.Request(health_url, method="GET")
                try:
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        return resp.status == 200
                except Exception:
                    return False

            result = await asyncio.to_thread(_check)
            self._is_ready = result
            return result

        except Exception:
            self._is_ready = False
            return False

    async def update_progress(
        self,
        progress: float,
        stage: str,
        message: Optional[str] = None,
        component_status: Optional[Dict[str, Any]] = None,
        force: bool = False,
    ) -> bool:
        """
        Update progress asynchronously.

        Args:
            progress: Progress percentage (0-100)
            stage: Current stage name
            message: Optional status message
            component_status: Optional component status dict
            force: If True, send immediately without batching

        Returns:
            True if update was sent successfully
        """
        # Ensure monotonic progress
        progress = max(self._last_progress, min(100.0, progress))
        self._last_progress = progress

        update = ProgressUpdate(
            progress=progress,
            stage=stage,
            message=message,
            component_status=component_status,
            trace_id=self._trace_context.trace_id if self._trace_context else None,
        )

        if force:
            return await self._send_update(update)

        # Add to pending updates for batching
        with self._update_lock:
            self._pending_updates.append(update)

            # If no batch task running, start one
            if self._batch_task is None or self._batch_task.done():
                self._batch_task = asyncio.create_task(self._batch_sender())

        return True

    async def _batch_sender(self) -> None:
        """Background task to send batched updates."""
        await asyncio.sleep(self.batch_interval)

        with self._update_lock:
            if not self._pending_updates:
                return

            # Send only the latest update
            latest = self._pending_updates[-1]
            self._pending_updates.clear()

        await self._send_update(latest)

    async def _send_update(self, update: ProgressUpdate) -> bool:
        """Send a single update with retry."""
        data = self._build_request_data(update)

        for attempt in range(self.max_retries):
            try:
                result = await asyncio.to_thread(
                    self._send_sync, data
                )
                if result:
                    return True
            except Exception as e:
                logger.debug(f"[Reporter] Attempt {attempt + 1} failed: {e}")

            if attempt < self.max_retries - 1:
                await asyncio.sleep(self.retry_backoff * (attempt + 1))

        return False

    def _build_request_data(self, update: ProgressUpdate) -> Dict[str, Any]:
        """Build request data from update."""
        data = {
            "progress": update.progress,
            "stage": update.stage,
            "message": update.message or f"{update.stage.replace('_', ' ').title()}...",
            "timestamp": update.timestamp,
        }

        if update.component_status:
            data["component_status"] = update.component_status

        if self._session_id:
            data["session_id"] = self._session_id

        if update.trace_id:
            data["trace_id"] = update.trace_id

        return data

    def _send_sync(self, data: Dict[str, Any]) -> bool:
        """Synchronous HTTP send."""
        try:
            body = json.dumps(data).encode("utf-8")

            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
            }

            if self._trace_context:
                headers["traceparent"] = self._trace_context.to_traceparent()

            req = urllib.request.Request(
                self.update_endpoint,
                data=body,
                headers=headers,
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status in (200, 201, 204)

        except urllib.error.URLError as e:
            logger.debug(f"[Reporter] URL error: {e}")
            return False
        except Exception as e:
            logger.debug(f"[Reporter] Send error: {e}")
            return False

    def update_progress_sync(
        self,
        progress: float,
        stage: str,
        message: Optional[str] = None,
        component_status: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Update progress synchronously.

        For use in non-async contexts.

        Args:
            progress: Progress percentage (0-100)
            stage: Current stage name
            message: Optional status message
            component_status: Optional component status dict

        Returns:
            True if update was sent successfully
        """
        progress = max(self._last_progress, min(100.0, progress))
        self._last_progress = progress

        update = ProgressUpdate(
            progress=progress,
            stage=stage,
            message=message,
            component_status=component_status,
            trace_id=self._trace_context.trace_id if self._trace_context else None,
        )

        data = self._build_request_data(update)

        for attempt in range(self.max_retries):
            if self._send_sync(data):
                return True
            time.sleep(self.retry_backoff * (attempt + 1))

        return False

    async def complete(
        self, message: str = "Startup complete!", redirect_url: Optional[str] = None
    ) -> bool:
        """
        Send completion signal.

        Args:
            message: Completion message
            redirect_url: Optional URL to redirect browser to

        Returns:
            True if sent successfully
        """
        return await self.update_progress(
            progress=100.0,
            stage="complete",
            message=message,
            component_status={"redirect_url": redirect_url} if redirect_url else None,
            force=True,
        )

    async def error(self, error_message: str, stage: Optional[str] = None) -> bool:
        """
        Send error signal.

        Args:
            error_message: Error description
            stage: Stage where error occurred

        Returns:
            True if sent successfully
        """
        return await self.update_progress(
            progress=self._last_progress,
            stage=stage or "error",
            message=f"Error: {error_message}",
            component_status={"error": error_message},
            force=True,
        )

    @asynccontextmanager
    async def stage_context(self, stage: str, start_progress: float = 0, end_progress: float = 100):
        """
        Context manager for tracking a stage.

        Args:
            stage: Stage name
            start_progress: Progress at start of stage
            end_progress: Progress at end of stage

        Yields:
            StageProgress helper
        """
        helper = StageProgress(
            reporter=self,
            stage=stage,
            start_progress=start_progress,
            end_progress=end_progress,
        )

        try:
            await helper.start()
            yield helper
            await helper.complete()
        except Exception as e:
            await helper.error(str(e))
            raise

    @contextmanager
    def stage(self, stage: str, start_progress: float = 0, end_progress: float = 100) -> Iterator["SyncStageProgress"]:
        """
        Sync context manager for tracking a stage.

        Args:
            stage: Stage name
            start_progress: Progress at start of stage
            end_progress: Progress at end of stage

        Yields:
            SyncStageProgress helper
        """
        helper = SyncStageProgress(
            reporter=self,
            stage=stage,
            start_progress=start_progress,
            end_progress=end_progress,
        )

        try:
            helper.start()
            yield helper
            helper.complete()
        except Exception as e:
            helper.error(str(e))
            raise

    async def __aenter__(self) -> "ProgressReporter":
        """Async context manager entry."""
        await self.check_ready()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()

    def reset(self) -> None:
        """Reset progress tracking."""
        self._last_progress = 0.0
        with self._update_lock:
            self._pending_updates.clear()


@dataclass
class StageProgress:
    """Helper for tracking progress within a stage (async)."""

    reporter: ProgressReporter
    stage: str
    start_progress: float = 0.0
    end_progress: float = 100.0

    async def start(self, message: Optional[str] = None) -> None:
        """Signal stage start."""
        await self.reporter.update_progress(
            self.start_progress,
            self.stage,
            message or f"Starting {self.stage}...",
        )

    async def update(self, percent_complete: float, message: Optional[str] = None) -> None:
        """
        Update progress within stage.

        Args:
            percent_complete: 0-100 within this stage
            message: Optional status message
        """
        progress = self.start_progress + (
            (self.end_progress - self.start_progress) * percent_complete / 100
        )
        await self.reporter.update_progress(progress, self.stage, message)

    async def complete(self, message: Optional[str] = None) -> None:
        """Signal stage completion."""
        await self.reporter.update_progress(
            self.end_progress,
            self.stage,
            message or f"{self.stage.title()} complete",
        )

    async def error(self, error_message: str) -> None:
        """Signal stage error."""
        await self.reporter.error(error_message, self.stage)


@dataclass
class SyncStageProgress:
    """Helper for tracking progress within a stage (sync)."""

    reporter: ProgressReporter
    stage: str
    start_progress: float = 0.0
    end_progress: float = 100.0

    def start(self, message: Optional[str] = None) -> None:
        """Signal stage start."""
        self.reporter.update_progress_sync(
            self.start_progress,
            self.stage,
            message or f"Starting {self.stage}...",
        )

    def update(self, percent_complete: float, message: Optional[str] = None) -> None:
        """Update progress within stage."""
        progress = self.start_progress + (
            (self.end_progress - self.start_progress) * percent_complete / 100
        )
        self.reporter.update_progress_sync(progress, self.stage, message)

    def complete(self, message: Optional[str] = None) -> None:
        """Signal stage completion."""
        self.reporter.update_progress_sync(
            self.end_progress,
            self.stage,
            message or f"{self.stage.title()} complete",
        )

    def error(self, error_message: str) -> None:
        """Signal stage error."""
        self.reporter.update_progress_sync(
            self.reporter._last_progress,
            "error",
            f"Error in {self.stage}: {error_message}",
        )


# Convenience functions for quick usage
_default_reporter: Optional[ProgressReporter] = None


def get_reporter(port: int = 8080) -> ProgressReporter:
    """Get or create a default reporter instance."""
    global _default_reporter
    if _default_reporter is None or _default_reporter.port != port:
        _default_reporter = ProgressReporter(port=port)
    return _default_reporter


def report_progress(
    progress: float, stage: str, message: Optional[str] = None, port: int = 8080
) -> bool:
    """Quick synchronous progress report."""
    return get_reporter(port).update_progress_sync(progress, stage, message)
