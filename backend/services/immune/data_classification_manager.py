"""DataClassificationManager — enterprise data classification and handling.

Extracted from unified_supervisor.py (lines 42395-42686).
The canonical copy remains in the monolith; this module exists so the
governance framework can import and register the service independently.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

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
class DataClassification:
    """
    Data classification definition.

    Attributes:
        level: Classification level (public, internal, confidential, restricted)
        label: Human-readable label
        handling_rules: Rules for how to handle this data
        retention_days: How long to retain data
        encryption_required: Whether encryption is required
        access_restrictions: Who can access this data
    """
    level: str
    label: str
    handling_rules: List[str]
    retention_days: int = 365
    encryption_required: bool = True
    access_restrictions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClassifiedData:
    """Represents classified data with its metadata."""
    data_id: str
    classification: DataClassification
    data_type: str
    location: str
    owner: str
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 0


# ---------------------------------------------------------------------------
# DataClassificationManager
# ---------------------------------------------------------------------------

class DataClassificationManager(SystemService):
    """
    Enterprise data classification and handling system.

    Classifies data based on content and context, enforces handling
    rules based on classification, and tracks data lineage.

    Features:
    - Automatic content-based classification
    - Manual classification overrides
    - Classification inheritance for derived data
    - Handling rule enforcement
    - Data lineage tracking
    """

    def __init__(self, config: SystemKernelConfig):
        self.config = config
        self._lock = asyncio.Lock()
        self._classifications: Dict[str, DataClassification] = {}
        self._classified_data: Dict[str, ClassifiedData] = {}
        self._lineage: Dict[str, List[str]] = {}  # parent -> children
        self._classifiers: List[Callable[[Any], Optional[str]]] = []
        self._logger = logging.getLogger("DataClassificationManager")
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize with default classification levels."""
        try:
            async with self._lock:
                # Define standard classification levels
                self._classifications = {
                    "public": DataClassification(
                        level="public",
                        label="Public",
                        handling_rules=["No restrictions"],
                        retention_days=365,
                        encryption_required=False,
                        access_restrictions=[],
                    ),
                    "internal": DataClassification(
                        level="internal",
                        label="Internal Use Only",
                        handling_rules=[
                            "Do not share externally",
                            "Mark documents as Internal",
                        ],
                        retention_days=730,
                        encryption_required=False,
                        access_restrictions=["employees"],
                    ),
                    "confidential": DataClassification(
                        level="confidential",
                        label="Confidential",
                        handling_rules=[
                            "Encrypt at rest",
                            "Encrypt in transit",
                            "Limit access to need-to-know",
                            "Audit all access",
                        ],
                        retention_days=1825,
                        encryption_required=True,
                        access_restrictions=["authorized_personnel"],
                    ),
                    "restricted": DataClassification(
                        level="restricted",
                        label="Restricted",
                        handling_rules=[
                            "Encrypt with strong encryption",
                            "Multi-factor access required",
                            "No copies allowed",
                            "Immediate breach notification",
                            "Regular access reviews",
                        ],
                        retention_days=2555,
                        encryption_required=True,
                        access_restrictions=["executive_team", "security_team"],
                    ),
                }

                # Register default classifiers
                self._register_default_classifiers()

                self._initialized = True
                self._logger.info("Data classification manager initialized")
                return True
        except Exception as e:
            self._logger.error(f"Failed to initialize data classification: {e}")
            return False

    def _register_default_classifiers(self) -> None:
        """Register automatic data classifiers."""
        # PII detector
        def pii_classifier(data: Any) -> Optional[str]:
            if isinstance(data, str):
                pii_patterns = [
                    r"\b\d{3}-\d{2}-\d{4}\b",  # SSN
                    r"\b\d{16}\b",  # Credit card
                    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",  # Email
                ]
                for pattern in pii_patterns:
                    if re.search(pattern, data):
                        return "confidential"
            return None

        # Credential detector
        def credential_classifier(data: Any) -> Optional[str]:
            if isinstance(data, str):
                cred_patterns = [
                    r"password\s*[:=]\s*",
                    r"api[_-]?key\s*[:=]\s*",
                    r"secret\s*[:=]\s*",
                    r"token\s*[:=]\s*",
                    r"-----BEGIN.*PRIVATE KEY-----",
                ]
                for pattern in cred_patterns:
                    if re.search(pattern, data, re.IGNORECASE):
                        return "restricted"
            return None

        self._classifiers.append(pii_classifier)
        self._classifiers.append(credential_classifier)

    def register_classifier(
        self,
        classifier: Callable[[Any], Optional[str]],
    ) -> None:
        """Register a custom data classifier."""
        self._classifiers.append(classifier)

    async def classify(
        self,
        data_id: str,
        data: Any,
        data_type: str,
        location: str,
        owner: str,
        override_level: Optional[str] = None,
    ) -> ClassifiedData:
        """
        Classify data and register it.

        Args:
            data_id: Unique identifier for the data
            data: The actual data to classify
            data_type: Type of data (file, record, etc.)
            location: Where the data is stored
            owner: Owner of the data
            override_level: Manual classification override

        Returns:
            ClassifiedData object
        """
        # Determine classification level
        level = override_level

        if not level:
            # Run through classifiers
            for classifier in self._classifiers:
                detected = classifier(data)
                if detected:
                    # Take the most restrictive classification
                    if not level or self._is_more_restrictive(detected, level):
                        level = detected

        # Default to internal if no classification detected
        level = level or "internal"

        # Get classification definition
        classification = self._classifications.get(level)
        if not classification:
            classification = self._classifications["internal"]

        # Create classified data record
        classified = ClassifiedData(
            data_id=data_id,
            classification=classification,
            data_type=data_type,
            location=location,
            owner=owner,
        )

        async with self._lock:
            self._classified_data[data_id] = classified

        self._logger.debug(f"Classified {data_id} as {level}")
        return classified

    def _is_more_restrictive(self, level1: str, level2: str) -> bool:
        """Check if level1 is more restrictive than level2."""
        order = ["public", "internal", "confidential", "restricted"]
        try:
            return order.index(level1) > order.index(level2)
        except ValueError:
            return False

    async def get_classification(self, data_id: str) -> Optional[ClassifiedData]:
        """Get classification for data."""
        return self._classified_data.get(data_id)

    async def check_access(
        self,
        data_id: str,
        accessor_roles: List[str],
    ) -> Tuple[bool, str]:
        """
        Check if access is allowed based on classification.

        Args:
            data_id: ID of the data to access
            accessor_roles: Roles of the person requesting access

        Returns:
            Tuple of (allowed, reason)
        """
        classified = self._classified_data.get(data_id)
        if not classified:
            return False, "Data not found"

        restrictions = classified.classification.access_restrictions
        if not restrictions:
            return True, "No restrictions"

        for role in accessor_roles:
            if role in restrictions:
                # Update access tracking
                classified.last_accessed = datetime.now()
                classified.access_count += 1
                return True, f"Access granted via role: {role}"

        return False, f"Access denied. Required roles: {restrictions}"

    def get_handling_rules(self, data_id: str) -> List[str]:
        """Get handling rules for classified data."""
        classified = self._classified_data.get(data_id)
        if classified:
            return classified.classification.handling_rules
        return []

    def record_lineage(self, parent_id: str, child_id: str) -> None:
        """Record data lineage (parent-child relationship)."""
        if parent_id not in self._lineage:
            self._lineage[parent_id] = []
        self._lineage[parent_id].append(child_id)

        # Inherit parent classification if child doesn't have one
        parent = self._classified_data.get(parent_id)
        child = self._classified_data.get(child_id)
        if parent and child:
            if self._is_more_restrictive(
                parent.classification.level,
                child.classification.level,
            ):
                child.classification = parent.classification

    # -- SystemService ABC --------------------------------------------------
    async def health_check(self) -> Tuple[bool, str]:
        classified = len(self._classified_data)
        return (True, f"DataClassificationManager: {classified} items classified")

    async def cleanup(self) -> None:
        self._classified_data.clear()
        self._lineage.clear()

    async def start(self) -> bool:
        if not self._initialized:
            await self.initialize()
        return True

    async def health(self) -> ServiceHealthReport:
        return ServiceHealthReport(
            alive=True,
            ready=self._initialized,
            message=f"DataClassificationManager: initialized={self._initialized}, classified={len(self._classified_data)}",
        )

    async def drain(self, deadline_s: float) -> bool:
        return True

    async def stop(self) -> None:
        await self.cleanup()

    def capability_contract(self) -> CapabilityContract:
        return CapabilityContract(
            name="DataClassificationManager",
            version="1.0.0",
            inputs=["data.ingested"],
            outputs=["data.classified"],
            side_effects=["writes_classification_labels"],
        )

    def activation_triggers(self) -> List[str]:
        return ["data.ingested"]  # event_driven
