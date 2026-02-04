"""
Adaptive Backpressure Controller for JARVIS Loading Server v212.0
=================================================================

AIMD (Additive Increase Multiplicative Decrease) backpressure for WebSocket.

Features:
- TCP-style congestion control
- Per-client backpressure tracking
- Automatic slow client detection
- Dynamic rate adjustment
- Queue depth monitoring
- Fair bandwidth allocation

Usage:
    from backend.loading_server.backpressure import (
        AdaptiveBackpressureController,
        ClientBackpressureTracker,
    )

    controller = AdaptiveBackpressureController()
    if controller.should_send():
        # Send message
        pass
    controller.report_congestion(queue_depth)

Author: JARVIS Trinity System
Version: 212.0.0
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Set

logger = logging.getLogger("LoadingServer.Backpressure")


@dataclass
class AdaptiveBackpressureController:
    """
    AIMD (Additive Increase Multiplicative Decrease) backpressure for WebSocket.

    Dynamically adjusts broadcast rate based on client processing capability.
    Uses TCP-style congestion control algorithms adapted for WebSocket messaging.

    Algorithm:
    - On congestion (queue depth > threshold): Rate *= 0.5 (multiplicative decrease)
    - On clear channel: Rate += additive_increase (additive increase)

    Attributes:
        initial_rate: Starting messages per second
        max_rate: Maximum messages per second
        min_rate: Minimum messages per second
        queue_depth_threshold: Queue depth that triggers congestion
        additive_increase: Rate increase per clear period
        multiplicative_decrease: Rate decrease factor on congestion
    """

    initial_rate: float = 10.0
    max_rate: float = 50.0
    min_rate: float = 1.0
    queue_depth_threshold: int = 10
    additive_increase: float = 1.0
    multiplicative_decrease: float = 0.5

    current_rate: float = field(init=False)
    _last_send_time: float = field(init=False, default=0.0)
    _congestion_detected: bool = field(init=False, default=False)
    _consecutive_clears: int = field(init=False, default=0)
    _rate_history: Deque[float] = field(init=False, default_factory=lambda: deque(maxlen=100))

    def __post_init__(self):
        """Initialize controller."""
        self.current_rate = self.initial_rate

    def should_send(self) -> bool:
        """
        Check if we should send based on current rate limit.

        Call this before each send to implement rate limiting.

        Returns:
            True if enough time has passed since last send
        """
        now = time.time()
        interval = 1.0 / self.current_rate

        if now - self._last_send_time >= interval:
            self._last_send_time = now
            return True

        return False

    def get_send_interval(self) -> float:
        """
        Get the current interval between sends.

        Returns:
            Seconds between sends at current rate
        """
        return 1.0 / self.current_rate

    def report_congestion(self, queue_depth: int) -> None:
        """
        Report queue depth to adjust rate.

        Call this after attempting to send to report the current state.

        Args:
            queue_depth: Number of messages waiting in queue
        """
        if queue_depth > self.queue_depth_threshold:
            # Multiplicative decrease
            old_rate = self.current_rate
            self.current_rate = max(self.min_rate, self.current_rate * self.multiplicative_decrease)
            self._congestion_detected = True
            self._consecutive_clears = 0

            if old_rate != self.current_rate:
                logger.debug(
                    f"[Backpressure] Congestion detected (depth={queue_depth}), "
                    f"rate: {old_rate:.1f} -> {self.current_rate:.1f}/s"
                )
        else:
            # Additive increase (slow start)
            if self._congestion_detected:
                self._consecutive_clears += 1

                # Only increase after consecutive clear periods
                if self._consecutive_clears >= 3:
                    old_rate = self.current_rate
                    self.current_rate = min(self.max_rate, self.current_rate + self.additive_increase)

                    if old_rate != self.current_rate:
                        logger.debug(
                            f"[Backpressure] Clear channel, rate: {old_rate:.1f} -> {self.current_rate:.1f}/s"
                        )

        self._rate_history.append(self.current_rate)

    def report_success(self) -> None:
        """Report a successful send (no congestion)."""
        self.report_congestion(0)

    def report_failure(self) -> None:
        """Report a failed send (treat as severe congestion)."""
        self.report_congestion(self.queue_depth_threshold * 2)

    def reset(self) -> None:
        """Reset to initial state."""
        self.current_rate = self.initial_rate
        self._congestion_detected = False
        self._consecutive_clears = 0
        self._rate_history.clear()

    def get_stats(self) -> dict:
        """
        Get current backpressure statistics.

        Returns:
            Dict with rate, congestion state, history
        """
        return {
            "current_rate": self.current_rate,
            "min_rate": self.min_rate,
            "max_rate": self.max_rate,
            "congestion_detected": self._congestion_detected,
            "consecutive_clears": self._consecutive_clears,
            "rate_history": list(self._rate_history)[-10:],  # Last 10
        }


@dataclass
class ClientBackpressureTracker:
    """
    Per-client backpressure tracking for slow client detection.

    Tracks each client's processing capability and maintains
    fair bandwidth allocation across clients.
    """

    slow_threshold_ms: float = 100.0  # Consider slow if >100ms per message
    disconnect_threshold: int = 100  # Disconnect after 100 backed-up messages

    _client_queues: Dict[str, int] = field(init=False, default_factory=dict)
    _client_latencies: Dict[str, Deque[float]] = field(init=False, default_factory=dict)
    _slow_clients: Set[str] = field(init=False, default_factory=set)
    _client_last_ack: Dict[str, float] = field(init=False, default_factory=dict)

    def register_client(self, client_id: str) -> None:
        """
        Register a new client for tracking.

        Args:
            client_id: Unique client identifier
        """
        self._client_queues[client_id] = 0
        self._client_latencies[client_id] = deque(maxlen=50)
        self._client_last_ack[client_id] = time.time()

    def unregister_client(self, client_id: str) -> None:
        """
        Remove a client from tracking.

        Args:
            client_id: Client to remove
        """
        self._client_queues.pop(client_id, None)
        self._client_latencies.pop(client_id, None)
        self._client_last_ack.pop(client_id, None)
        self._slow_clients.discard(client_id)

    def message_sent(self, client_id: str) -> None:
        """
        Record that a message was sent to a client.

        Args:
            client_id: Client that message was sent to
        """
        if client_id in self._client_queues:
            self._client_queues[client_id] += 1

    def message_acknowledged(self, client_id: str, latency_ms: float) -> None:
        """
        Record that a client acknowledged a message.

        Args:
            client_id: Client that sent acknowledgment
            latency_ms: Round-trip latency in milliseconds
        """
        if client_id not in self._client_queues:
            return

        self._client_queues[client_id] = max(0, self._client_queues[client_id] - 1)
        self._client_latencies[client_id].append(latency_ms)
        self._client_last_ack[client_id] = time.time()

        # Update slow client status
        avg_latency = self._get_average_latency(client_id)
        if avg_latency > self.slow_threshold_ms:
            if client_id not in self._slow_clients:
                self._slow_clients.add(client_id)
                logger.debug(f"[Backpressure] Client {client_id[:8]} marked as slow (avg: {avg_latency:.0f}ms)")
        else:
            self._slow_clients.discard(client_id)

    def _get_average_latency(self, client_id: str) -> float:
        """Get average latency for a client."""
        latencies = self._client_latencies.get(client_id)
        if not latencies:
            return 0.0
        return sum(latencies) / len(latencies)

    def is_slow_client(self, client_id: str) -> bool:
        """
        Check if a client is considered slow.

        Args:
            client_id: Client to check

        Returns:
            True if client is slow
        """
        return client_id in self._slow_clients

    def should_disconnect(self, client_id: str) -> bool:
        """
        Check if a client should be disconnected due to backlog.

        Args:
            client_id: Client to check

        Returns:
            True if client should be disconnected
        """
        queue_depth = self._client_queues.get(client_id, 0)
        return queue_depth >= self.disconnect_threshold

    def get_client_queue_depth(self, client_id: str) -> int:
        """Get current queue depth for a client."""
        return self._client_queues.get(client_id, 0)

    def get_total_queue_depth(self) -> int:
        """Get total queue depth across all clients."""
        return sum(self._client_queues.values())

    def get_slow_client_ids(self) -> Set[str]:
        """Get set of slow client IDs."""
        return self._slow_clients.copy()

    def get_stats(self) -> dict:
        """
        Get backpressure statistics across all clients.

        Returns:
            Dict with client counts, queue depths, slow clients
        """
        return {
            "total_clients": len(self._client_queues),
            "slow_clients": len(self._slow_clients),
            "total_queue_depth": self.get_total_queue_depth(),
            "max_queue_depth": max(self._client_queues.values()) if self._client_queues else 0,
            "client_stats": {
                cid[:8]: {
                    "queue_depth": self._client_queues.get(cid, 0),
                    "avg_latency_ms": self._get_average_latency(cid),
                    "is_slow": cid in self._slow_clients,
                }
                for cid in self._client_queues
            },
        }


@dataclass
class TokenBucketRateLimiter:
    """
    Token bucket rate limiter for fine-grained control.

    Allows bursting up to bucket size while maintaining average rate.
    """

    rate: float = 10.0  # Tokens per second
    bucket_size: int = 20  # Maximum burst size

    _tokens: float = field(init=False)
    _last_update: float = field(init=False)

    def __post_init__(self):
        """Initialize bucket."""
        self._tokens = float(self.bucket_size)
        self._last_update = time.time()

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self._last_update
        self._tokens = min(self.bucket_size, self._tokens + elapsed * self.rate)
        self._last_update = now

    def acquire(self, tokens: int = 1) -> bool:
        """
        Try to acquire tokens.

        Args:
            tokens: Number of tokens to acquire

        Returns:
            True if tokens were available, False otherwise
        """
        self._refill()

        if self._tokens >= tokens:
            self._tokens -= tokens
            return True

        return False

    async def acquire_async(self, tokens: int = 1, timeout: float = 1.0) -> bool:
        """
        Async version - waits for tokens to become available.

        Args:
            tokens: Number of tokens to acquire
            timeout: Maximum time to wait

        Returns:
            True if tokens were acquired, False if timeout
        """
        start = time.time()

        while time.time() - start < timeout:
            if self.acquire(tokens):
                return True
            await asyncio.sleep(0.01)

        return False

    def get_available_tokens(self) -> float:
        """Get current number of available tokens."""
        self._refill()
        return self._tokens
