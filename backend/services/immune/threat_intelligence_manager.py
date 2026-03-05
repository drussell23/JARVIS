"""ThreatIntelligenceManager — threat intelligence aggregation and correlation.

Extracted from unified_supervisor.py (lines 44073-44367).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.services.immune._base import (
    CapabilityContract,
    ServiceHealthReport,
    SystemKernelConfig,
    SystemService,
)


# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------

@dataclass
class ThreatIndicator:
    """Represents a threat indicator (IOC)."""
    indicator_id: str
    indicator_type: str  # ip, domain, hash, email, url
    value: str
    threat_type: str  # malware, phishing, c2, etc.
    severity: str
    confidence: float  # 0.0 to 1.0
    source: str
    first_seen: datetime = field(default_factory=datetime.now)
    last_seen: datetime = field(default_factory=datetime.now)
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ThreatIntelligenceManager
# ---------------------------------------------------------------------------

class ThreatIntelligenceManager(SystemService):
    """
    Threat intelligence aggregation and correlation system.

    Aggregates threat intelligence from multiple sources, correlates
    indicators, and provides threat scoring.

    Features:
    - Multiple intelligence source integration
    - IOC (Indicator of Compromise) management
    - Threat scoring and correlation
    - Automatic blocking rules generation
    - Intelligence aging and expiration
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._indicators: Dict[str, ThreatIndicator] = {}
        self._indicators_by_type: Dict[str, Set[str]] = {}
        self._sources: Dict[str, Dict[str, Any]] = {}
        self._correlations: Dict[str, List[str]] = {}
        self._expiration_days: int = 90
        self._logger = logging.getLogger("ThreatIntelligenceManager")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize threat intelligence manager."""
        try:
            async with self._lock:
                # Initialize indicator type indexes
                for ioc_type in ["ip", "domain", "hash", "email", "url"]:
                    self._indicators_by_type[ioc_type] = set()

                self._initialized = True
                self._logger.info("Threat intelligence manager initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize threat intelligence: {e}")
            return False

    async def add_indicator(
        self,
        indicator_type: str,
        value: str,
        threat_type: str,
        severity: str,
        confidence: float,
        source: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ThreatIndicator:
        """
        Add a threat indicator.

        Args:
            indicator_type: Type of indicator (ip, domain, hash, etc.)
            value: The indicator value
            threat_type: Type of threat
            severity: Threat severity
            confidence: Confidence level (0-1)
            source: Intelligence source
            tags: Optional tags
            metadata: Optional metadata

        Returns:
            ThreatIndicator object
        """
        # Normalize value
        value = value.lower().strip()

        # Generate ID
        indicator_id = f"{indicator_type}_{hashlib.md5(value.encode()).hexdigest()[:16]}"

        # Check if exists
        existing = self._indicators.get(indicator_id)
        if existing:
            # Update existing indicator
            existing.last_seen = datetime.now()
            existing.confidence = max(existing.confidence, confidence)
            if tags:
                existing.tags = list(set(existing.tags + tags))
            return existing

        indicator = ThreatIndicator(
            indicator_id=indicator_id,
            indicator_type=indicator_type,
            value=value,
            threat_type=threat_type,
            severity=severity,
            confidence=confidence,
            source=source,
            tags=tags or [],
            metadata=metadata or {},
        )

        async with self._lock:
            self._indicators[indicator_id] = indicator
            self._indicators_by_type[indicator_type].add(indicator_id)

        self._logger.debug(f"Added indicator: {indicator_type}:{value}")
        return indicator

    async def check_indicator(
        self,
        indicator_type: str,
        value: str,
    ) -> Optional[ThreatIndicator]:
        """
        Check if a value matches a known threat indicator.

        Args:
            indicator_type: Type to check
            value: Value to check

        Returns:
            ThreatIndicator if found, None otherwise
        """
        value = value.lower().strip()
        indicator_id = f"{indicator_type}_{hashlib.md5(value.encode()).hexdigest()[:16]}"

        indicator = self._indicators.get(indicator_id)
        if indicator:
            # Check expiration
            age_days = (datetime.now() - indicator.first_seen).days
            if age_days > self._expiration_days:
                return None

            # Update last seen
            indicator.last_seen = datetime.now()
            return indicator

        return None

    async def check_multiple(
        self,
        checks: List[Tuple[str, str]],
    ) -> List[ThreatIndicator]:
        """
        Check multiple indicators at once.

        Args:
            checks: List of (indicator_type, value) tuples

        Returns:
            List of matched ThreatIndicators
        """
        matches = []
        for indicator_type, value in checks:
            match = await self.check_indicator(indicator_type, value)
            if match:
                matches.append(match)
        return matches

    async def correlate_indicators(
        self,
        indicator_ids: List[str],
        correlation_id: str,
    ) -> None:
        """Correlate multiple indicators."""
        async with self._lock:
            for iid in indicator_ids:
                if iid not in self._correlations:
                    self._correlations[iid] = []
                for other_id in indicator_ids:
                    if other_id != iid and other_id not in self._correlations[iid]:
                        self._correlations[iid].append(other_id)

    def get_correlated(self, indicator_id: str) -> List[ThreatIndicator]:
        """Get correlated indicators."""
        correlated_ids = self._correlations.get(indicator_id, [])
        return [self._indicators[iid] for iid in correlated_ids if iid in self._indicators]

    async def cleanup_expired(self) -> int:
        """Remove expired indicators."""
        removed = 0
        now = datetime.now()

        async with self._lock:
            to_remove = []
            for iid, indicator in self._indicators.items():
                age_days = (now - indicator.first_seen).days
                if age_days > self._expiration_days:
                    to_remove.append(iid)

            for iid in to_remove:
                indicator = self._indicators.pop(iid)
                self._indicators_by_type[indicator.indicator_type].discard(iid)
                removed += 1

        self._logger.info(f"Cleaned up {removed} expired indicators")
        return removed

    def get_statistics(self) -> Dict[str, Any]:
        """Get threat intelligence statistics."""
        stats: Dict[str, Any] = {
            "total_indicators": len(self._indicators),
            "by_type": {},
            "by_severity": {},
            "by_threat_type": {},
            "sources": list(self._sources.keys()),
        }

        for ioc_type, ids in self._indicators_by_type.items():
            stats["by_type"][ioc_type] = len(ids)

        severity_counts: Dict[str, int] = {}
        threat_type_counts: Dict[str, int] = {}

        for indicator in self._indicators.values():
            severity_counts[indicator.severity] = severity_counts.get(indicator.severity, 0) + 1
            threat_type_counts[indicator.threat_type] = threat_type_counts.get(indicator.threat_type, 0) + 1

        stats["by_severity"] = severity_counts
        stats["by_threat_type"] = threat_type_counts

        return stats

    def generate_blocking_rules(
        self,
        indicator_type: str,
        min_confidence: float = 0.7,
        min_severity: str = "medium",
    ) -> List[Dict[str, Any]]:
        """Generate blocking rules from indicators."""
        severity_order = ["low", "medium", "high", "critical"]
        min_severity_idx = severity_order.index(min_severity)

        rules = []
        indicator_ids = self._indicators_by_type.get(indicator_type, set())

        for iid in indicator_ids:
            indicator = self._indicators.get(iid)
            if not indicator:
                continue

            if indicator.confidence < min_confidence:
                continue

            try:
                severity_idx = severity_order.index(indicator.severity)
                if severity_idx < min_severity_idx:
                    continue
            except ValueError:
                continue

            rules.append({
                "type": indicator_type,
                "value": indicator.value,
                "action": "block",
                "reason": f"{indicator.threat_type} (confidence: {indicator.confidence:.2f})",
                "source": indicator.source,
                "indicator_id": indicator.indicator_id,
            })

        return rules

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        indicators = len(self._indicators)
        sources = len(self._sources)
        return (True, f"ThreatIntelligenceManager: {indicators} indicators, {sources} sources")

    async def cleanup(self) -> None:
        self._correlations.clear()

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"ThreatIntelligenceManager: initialized={self._initialized}, indicators={len(self._indicators)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="ThreatIntelligenceManager",
            version="1.0.0",
            inputs=["anomaly.detected"],
            outputs=["threat.confirmed", "threat.dismissed"],
            side_effects=["writes_threat_indicators"],
        )

    def activation_triggers(self) -> List[str]:
        return ["anomaly.detected"]  # event_driven
