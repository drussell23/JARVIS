"""
JARVIS Neural Mesh - Pattern Recognition Agent

Advanced pattern detection across system data, behaviors, and events.
Uses statistical analysis and temporal pattern mining to identify
recurring patterns, anomalies, and predictive signals.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessagePriority, MessageType

logger = logging.getLogger(__name__)


class PatternType(Enum):
    """Types of patterns the agent can detect."""
    TEMPORAL = "temporal"  # Time-based patterns (daily, weekly, etc.)
    SEQUENTIAL = "sequential"  # Event sequence patterns
    BEHAVIORAL = "behavioral"  # User behavior patterns
    ANOMALY = "anomaly"  # Deviations from normal patterns
    CORRELATION = "correlation"  # Related event patterns
    FREQUENCY = "frequency"  # Event frequency patterns
    TREND = "trend"  # Increasing/decreasing trends


@dataclass
class DetectedPattern:
    """Represents a detected pattern."""
    pattern_id: str
    pattern_type: PatternType
    description: str
    confidence: float
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    data: Dict[str, Any] = field(default_factory=dict)
    related_events: List[str] = field(default_factory=list)


@dataclass
class TimeSeriesPoint:
    """A point in a time series."""
    timestamp: datetime
    value: float
    metadata: Dict[str, Any] = field(default_factory=dict)


class PatternRecognitionAgent(BaseNeuralMeshAgent):
    """
    Pattern Recognition Agent - Intelligent pattern detection and analysis.

    Capabilities:
    - detect_patterns: Analyze data for patterns
    - find_anomalies: Detect anomalies in behavior/data
    - predict_next: Predict likely next events
    - get_trends: Identify trends in time series
    - correlate_events: Find correlated events
    - track_frequency: Track event frequencies
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="pattern_recognition_agent",
            agent_type="intelligence",
            capabilities={
                "detect_patterns",
                "find_anomalies",
                "predict_next",
                "get_trends",
                "correlate_events",
                "track_frequency",
                "analyze_sequence",
                "get_pattern_stats",
            },
            version="1.0.0",
        )

        self._patterns: Dict[str, DetectedPattern] = {}
        self._event_history: List[Dict[str, Any]] = []
        self._time_series: Dict[str, List[TimeSeriesPoint]] = defaultdict(list)
        self._frequency_counters: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._correlation_matrix: Dict[str, Dict[str, float]] = defaultdict(dict)
        self._pattern_counter = 0
        self._analysis_task: Optional[asyncio.Task] = None

    async def on_initialize(self) -> None:
        logger.info("Initializing PatternRecognitionAgent")

        # Subscribe to events for pattern tracking
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_event,
        )

        # Start background pattern analysis
        self._analysis_task = asyncio.create_task(
            self._periodic_analysis(),
            name="pattern_analysis"
        )

        logger.info("PatternRecognitionAgent initialized")

    async def on_start(self) -> None:
        logger.info("PatternRecognitionAgent started - detecting patterns")

    async def on_stop(self) -> None:
        if self._analysis_task:
            self._analysis_task.cancel()
            try:
                await self._analysis_task
            except asyncio.CancelledError:
                pass
        logger.info(
            f"PatternRecognitionAgent stopping - detected {len(self._patterns)} patterns"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = payload.get("action", "")

        if action == "detect_patterns":
            return await self._detect_patterns(payload)
        elif action == "find_anomalies":
            return await self._find_anomalies(payload)
        elif action == "predict_next":
            return await self._predict_next(payload)
        elif action == "get_trends":
            return self._get_trends(payload)
        elif action == "correlate_events":
            return await self._correlate_events(payload)
        elif action == "track_frequency":
            return self._track_frequency(payload)
        elif action == "analyze_sequence":
            return self._analyze_sequence(payload)
        elif action == "get_pattern_stats":
            return self._get_pattern_stats()
        else:
            raise ValueError(f"Unknown pattern action: {action}")

    async def _detect_patterns(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Detect patterns in provided data."""
        data = payload.get("data", [])
        pattern_types = payload.get("pattern_types", [pt.value for pt in PatternType])
        min_confidence = payload.get("min_confidence", 0.7)

        detected = []

        # Temporal pattern detection
        if "temporal" in pattern_types:
            temporal = self._detect_temporal_patterns(data)
            detected.extend([p for p in temporal if p.confidence >= min_confidence])

        # Sequential pattern detection
        if "sequential" in pattern_types:
            sequential = self._detect_sequential_patterns(data)
            detected.extend([p for p in sequential if p.confidence >= min_confidence])

        # Frequency pattern detection
        if "frequency" in pattern_types:
            frequency = self._detect_frequency_patterns(data)
            detected.extend([p for p in frequency if p.confidence >= min_confidence])

        # Store detected patterns
        for pattern in detected:
            self._patterns[pattern.pattern_id] = pattern

        # Add to knowledge graph
        if self.knowledge_graph and detected:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.OBSERVATION,
                data={
                    "detected_patterns": len(detected),
                    "pattern_types": [p.pattern_type.value for p in detected],
                    "timestamp": datetime.now().isoformat(),
                },
                confidence=0.9,
            )

        return {
            "status": "success",
            "patterns_detected": len(detected),
            "patterns": [
                {
                    "pattern_id": p.pattern_id,
                    "type": p.pattern_type.value,
                    "description": p.description,
                    "confidence": p.confidence,
                    "occurrences": p.occurrences,
                }
                for p in detected
            ],
        }

    def _detect_temporal_patterns(self, data: List[Dict]) -> List[DetectedPattern]:
        """Detect time-based patterns."""
        patterns = []

        if not data:
            return patterns

        # Extract timestamps
        timestamps = []
        for item in data:
            ts = item.get("timestamp")
            if ts:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                timestamps.append(ts)

        if len(timestamps) < 3:
            return patterns

        timestamps.sort()

        # Detect hourly patterns
        hour_counts: Dict[int, int] = defaultdict(int)
        for ts in timestamps:
            hour_counts[ts.hour] += 1

        peak_hour = max(hour_counts, key=hour_counts.get)
        if hour_counts[peak_hour] >= len(timestamps) * 0.3:
            patterns.append(DetectedPattern(
                pattern_id=self._generate_pattern_id(),
                pattern_type=PatternType.TEMPORAL,
                description=f"Peak activity at hour {peak_hour}:00",
                confidence=hour_counts[peak_hour] / len(timestamps),
                occurrences=hour_counts[peak_hour],
                first_seen=timestamps[0],
                last_seen=timestamps[-1],
                data={"peak_hour": peak_hour, "hour_distribution": dict(hour_counts)},
            ))

        # Detect day of week patterns
        dow_counts: Dict[int, int] = defaultdict(int)
        for ts in timestamps:
            dow_counts[ts.weekday()] += 1

        peak_day = max(dow_counts, key=dow_counts.get)
        if dow_counts[peak_day] >= len(timestamps) * 0.25:
            day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            patterns.append(DetectedPattern(
                pattern_id=self._generate_pattern_id(),
                pattern_type=PatternType.TEMPORAL,
                description=f"Peak activity on {day_names[peak_day]}",
                confidence=dow_counts[peak_day] / len(timestamps),
                occurrences=dow_counts[peak_day],
                first_seen=timestamps[0],
                last_seen=timestamps[-1],
                data={"peak_day": peak_day, "day_distribution": dict(dow_counts)},
            ))

        return patterns

    def _detect_sequential_patterns(self, data: List[Dict]) -> List[DetectedPattern]:
        """Detect sequential event patterns."""
        patterns = []

        if len(data) < 3:
            return patterns

        # Extract event sequences
        events = [item.get("event_type", item.get("type", "unknown")) for item in data]

        # Find frequent pairs
        pair_counts: Dict[Tuple[str, str], int] = defaultdict(int)
        for i in range(len(events) - 1):
            pair = (events[i], events[i + 1])
            pair_counts[pair] += 1

        # Significant pairs (appear more than 20% of time)
        threshold = len(events) * 0.2
        for pair, count in pair_counts.items():
            if count >= threshold:
                patterns.append(DetectedPattern(
                    pattern_id=self._generate_pattern_id(),
                    pattern_type=PatternType.SEQUENTIAL,
                    description=f"'{pair[0]}' frequently followed by '{pair[1]}'",
                    confidence=count / len(events),
                    occurrences=count,
                    first_seen=datetime.now(),
                    last_seen=datetime.now(),
                    data={"sequence": list(pair), "count": count},
                ))

        return patterns

    def _detect_frequency_patterns(self, data: List[Dict]) -> List[DetectedPattern]:
        """Detect frequency-based patterns."""
        patterns = []

        if len(data) < 5:
            return patterns

        # Count event types
        type_counts: Dict[str, int] = defaultdict(int)
        for item in data:
            event_type = item.get("event_type", item.get("type", "unknown"))
            type_counts[event_type] += 1

        total = len(data)
        for event_type, count in type_counts.items():
            frequency = count / total
            if frequency >= 0.15:  # At least 15% of events
                patterns.append(DetectedPattern(
                    pattern_id=self._generate_pattern_id(),
                    pattern_type=PatternType.FREQUENCY,
                    description=f"'{event_type}' occurs frequently ({frequency:.1%})",
                    confidence=frequency,
                    occurrences=count,
                    first_seen=datetime.now(),
                    last_seen=datetime.now(),
                    data={"event_type": event_type, "count": count, "frequency": frequency},
                ))

        return patterns

    async def _find_anomalies(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Detect anomalies in data."""
        data = payload.get("data", [])
        metric = payload.get("metric", "value")
        sensitivity = payload.get("sensitivity", 2.0)  # Standard deviations

        if not data:
            return {"status": "success", "anomalies": []}

        # Extract values
        values = [item.get(metric, 0) for item in data if metric in item]

        if len(values) < 5:
            return {"status": "insufficient_data", "message": "Need at least 5 data points"}

        # Calculate statistics
        mean_val = statistics.mean(values)
        stdev_val = statistics.stdev(values) if len(values) > 1 else 0

        # Find anomalies
        anomalies = []
        for i, item in enumerate(data):
            if metric not in item:
                continue
            value = item[metric]
            if stdev_val > 0:
                z_score = abs(value - mean_val) / stdev_val
                if z_score > sensitivity:
                    anomalies.append({
                        "index": i,
                        "value": value,
                        "z_score": z_score,
                        "deviation": "high" if value > mean_val else "low",
                        "data": item,
                    })

        return {
            "status": "success",
            "anomalies_found": len(anomalies),
            "anomalies": anomalies,
            "statistics": {
                "mean": mean_val,
                "stdev": stdev_val,
                "sensitivity_threshold": sensitivity,
            },
        }

    async def _predict_next(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Predict likely next events based on patterns."""
        recent_events = payload.get("recent_events", [])
        prediction_count = payload.get("count", 3)

        if not recent_events:
            return {"status": "success", "predictions": [], "message": "No recent events provided"}

        # Get last event
        last_event = recent_events[-1] if isinstance(recent_events[-1], str) else \
            recent_events[-1].get("event_type", recent_events[-1].get("type", "unknown"))

        # Find sequential patterns that start with this event
        predictions = []
        for pattern in self._patterns.values():
            if pattern.pattern_type == PatternType.SEQUENTIAL:
                sequence = pattern.data.get("sequence", [])
                if sequence and sequence[0] == last_event:
                    predictions.append({
                        "predicted_event": sequence[1],
                        "confidence": pattern.confidence,
                        "based_on_pattern": pattern.pattern_id,
                        "occurrences": pattern.occurrences,
                    })

        # Sort by confidence
        predictions.sort(key=lambda x: x["confidence"], reverse=True)

        return {
            "status": "success",
            "predictions": predictions[:prediction_count],
            "based_on_event": last_event,
        }

    def _get_trends(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Identify trends in time series data."""
        series_name = payload.get("series_name", "default")
        data = payload.get("data", [])
        window_size = payload.get("window_size", 5)

        # Use provided data or stored series
        points = []
        if data:
            for item in data:
                ts = item.get("timestamp")
                val = item.get("value", 0)
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                points.append(TimeSeriesPoint(timestamp=ts, value=val))
        else:
            points = self._time_series.get(series_name, [])

        if len(points) < window_size:
            return {
                "status": "insufficient_data",
                "message": f"Need at least {window_size} data points",
            }

        # Sort by timestamp
        points.sort(key=lambda p: p.timestamp)
        values = [p.value for p in points]

        # Calculate moving average
        moving_avg = []
        for i in range(len(values) - window_size + 1):
            window = values[i:i + window_size]
            moving_avg.append(sum(window) / len(window))

        # Determine trend
        if len(moving_avg) >= 2:
            first_half_avg = sum(moving_avg[:len(moving_avg) // 2]) / (len(moving_avg) // 2)
            second_half_avg = sum(moving_avg[len(moving_avg) // 2:]) / (len(moving_avg) - len(moving_avg) // 2)

            if second_half_avg > first_half_avg * 1.1:
                trend = "increasing"
                strength = (second_half_avg - first_half_avg) / first_half_avg if first_half_avg != 0 else 0
            elif second_half_avg < first_half_avg * 0.9:
                trend = "decreasing"
                strength = (first_half_avg - second_half_avg) / first_half_avg if first_half_avg != 0 else 0
            else:
                trend = "stable"
                strength = 0
        else:
            trend = "unknown"
            strength = 0

        return {
            "status": "success",
            "trend": trend,
            "strength": strength,
            "data_points": len(points),
            "moving_average": moving_avg[-5:] if len(moving_avg) >= 5 else moving_avg,
            "statistics": {
                "min": min(values),
                "max": max(values),
                "mean": sum(values) / len(values),
            },
        }

    async def _correlate_events(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Find correlated events."""
        events = payload.get("events", [])
        time_window_seconds = payload.get("time_window_seconds", 60)

        if len(events) < 10:
            return {"status": "insufficient_data", "message": "Need at least 10 events"}

        # Group events by type with timestamps
        event_times: Dict[str, List[datetime]] = defaultdict(list)
        for event in events:
            event_type = event.get("event_type", event.get("type", "unknown"))
            ts = event.get("timestamp")
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if ts:
                event_times[event_type].append(ts)

        # Find correlations (events that often occur close together)
        correlations = []
        event_types = list(event_times.keys())

        for i, type_a in enumerate(event_types):
            for type_b in event_types[i + 1:]:
                times_a = sorted(event_times[type_a])
                times_b = sorted(event_times[type_b])

                # Count co-occurrences within time window
                co_occurrences = 0
                for ta in times_a:
                    for tb in times_b:
                        diff = abs((ta - tb).total_seconds())
                        if diff <= time_window_seconds:
                            co_occurrences += 1
                            break

                if co_occurrences > 0:
                    correlation_strength = co_occurrences / max(len(times_a), len(times_b))
                    if correlation_strength >= 0.3:  # At least 30% correlation
                        correlations.append({
                            "event_a": type_a,
                            "event_b": type_b,
                            "correlation_strength": correlation_strength,
                            "co_occurrences": co_occurrences,
                        })

                        # Store in correlation matrix
                        self._correlation_matrix[type_a][type_b] = correlation_strength
                        self._correlation_matrix[type_b][type_a] = correlation_strength

        # Sort by correlation strength
        correlations.sort(key=lambda x: x["correlation_strength"], reverse=True)

        return {
            "status": "success",
            "correlations_found": len(correlations),
            "correlations": correlations[:10],  # Top 10
            "time_window_seconds": time_window_seconds,
        }

    def _track_frequency(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Track and report event frequencies."""
        event_type = payload.get("event_type")
        category = payload.get("category", "default")
        increment = payload.get("increment", True)
        get_report = payload.get("get_report", False)

        if increment and event_type:
            self._frequency_counters[category][event_type] += 1

        if get_report:
            category_data = dict(self._frequency_counters.get(category, {}))
            total = sum(category_data.values())
            return {
                "status": "success",
                "category": category,
                "total_events": total,
                "frequencies": {
                    k: {"count": v, "percentage": v / total if total > 0 else 0}
                    for k, v in sorted(category_data.items(), key=lambda x: x[1], reverse=True)
                },
            }

        return {
            "status": "tracked",
            "event_type": event_type,
            "category": category,
            "new_count": self._frequency_counters[category].get(event_type, 0),
        }

    def _analyze_sequence(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze an event sequence for patterns."""
        sequence = payload.get("sequence", [])

        if len(sequence) < 3:
            return {"status": "insufficient_data", "message": "Need at least 3 events"}

        # Analyze sequence
        # 1. Find repeating subsequences
        repeats = self._find_repeating_subsequences(sequence)

        # 2. Calculate transition probabilities
        transitions = self._calculate_transitions(sequence)

        # 3. Find most common elements
        element_counts: Dict[str, int] = defaultdict(int)
        for elem in sequence:
            element_counts[elem] += 1

        sorted_elements = sorted(element_counts.items(), key=lambda x: x[1], reverse=True)

        return {
            "status": "success",
            "sequence_length": len(sequence),
            "unique_elements": len(element_counts),
            "most_common": sorted_elements[:5],
            "repeating_subsequences": repeats[:5],
            "transitions": dict(list(transitions.items())[:10]),
        }

    def _find_repeating_subsequences(self, sequence: List[str]) -> List[Dict]:
        """Find repeating subsequences."""
        if len(sequence) < 2:
            return []

        repeats = []
        for length in range(2, min(len(sequence) // 2 + 1, 6)):
            subseq_counts: Dict[tuple, int] = defaultdict(int)
            for i in range(len(sequence) - length + 1):
                subseq = tuple(sequence[i:i + length])
                subseq_counts[subseq] += 1

            for subseq, count in subseq_counts.items():
                if count >= 2:
                    repeats.append({
                        "subsequence": list(subseq),
                        "length": length,
                        "occurrences": count,
                    })

        return sorted(repeats, key=lambda x: x["occurrences"], reverse=True)

    def _calculate_transitions(self, sequence: List[str]) -> Dict[str, Dict[str, float]]:
        """Calculate transition probabilities."""
        transitions: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        totals: Dict[str, int] = defaultdict(int)

        for i in range(len(sequence) - 1):
            curr, next_elem = sequence[i], sequence[i + 1]
            transitions[curr][next_elem] += 1
            totals[curr] += 1

        # Convert to probabilities
        probs: Dict[str, Dict[str, float]] = {}
        for curr, nexts in transitions.items():
            probs[curr] = {
                next_elem: count / totals[curr]
                for next_elem, count in nexts.items()
            }

        return probs

    def _get_pattern_stats(self) -> Dict[str, Any]:
        """Get pattern recognition statistics."""
        pattern_types: Dict[str, int] = defaultdict(int)
        for pattern in self._patterns.values():
            pattern_types[pattern.pattern_type.value] += 1

        return {
            "status": "success",
            "total_patterns": len(self._patterns),
            "patterns_by_type": dict(pattern_types),
            "events_tracked": len(self._event_history),
            "time_series_tracked": len(self._time_series),
            "frequency_categories": len(self._frequency_counters),
            "correlation_pairs": sum(len(v) for v in self._correlation_matrix.values()) // 2,
        }

    def _generate_pattern_id(self) -> str:
        """Generate unique pattern ID."""
        self._pattern_counter += 1
        return f"pattern_{datetime.now().timestamp()}_{self._pattern_counter}"

    async def _handle_event(self, message: AgentMessage) -> None:
        """Handle incoming events for pattern tracking."""
        if message.payload.get("type") == "event":
            event = {
                "timestamp": datetime.now().isoformat(),
                "event_type": message.payload.get("event_type", "unknown"),
                "data": message.payload.get("data", {}),
                "source": message.from_agent,
            }
            self._event_history.append(event)

            # Track frequency
            self._track_frequency({
                "event_type": event["event_type"],
                "category": "all_events",
            })

            # v238.0: Broadcast notable events for cross-agent awareness
            try:
                await self.broadcast(
                    message_type=MessageType.KNOWLEDGE_SHARED,
                    payload={
                        "type": "pattern_event_tracked",
                        "event_type": event["event_type"],
                        "source": event["source"],
                        "event_count": len(self._event_history),
                    },
                    priority=MessagePriority.LOW,
                )
            except Exception:
                pass  # Best-effort broadcast

            # Limit history size
            if len(self._event_history) > 10000:
                self._event_history = self._event_history[-10000:]

    async def _periodic_analysis(self) -> None:
        """Periodically analyze collected data for patterns."""
        max_runtime = float(os.getenv("TIMEOUT_PATTERN_ANALYSIS_SESSION", "86400.0"))  # 24 hours
        analysis_interval = float(os.getenv("PATTERN_ANALYSIS_INTERVAL", "300.0"))  # 5 minutes
        iteration_timeout = float(os.getenv("TIMEOUT_PATTERN_ANALYSIS_ITERATION", "60.0"))
        start = time.monotonic()
        cancelled = False

        while time.monotonic() - start < max_runtime:
            try:
                await asyncio.sleep(analysis_interval)

                if len(self._event_history) >= 20:
                    # Run pattern detection on recent events with timeout
                    recent = self._event_history[-100:]
                    await asyncio.wait_for(
                        self._detect_patterns({
                            "data": recent,
                            "pattern_types": ["temporal", "sequential", "frequency"],
                        }),
                        timeout=iteration_timeout,
                    )

                    logger.debug(f"Periodic analysis complete - {len(self._patterns)} patterns")

            except asyncio.TimeoutError:
                logger.warning("Pattern analysis iteration timed out")
            except asyncio.CancelledError:
                cancelled = True
                break
            except Exception as e:
                logger.exception(f"Error in periodic pattern analysis: {e}")

        if cancelled:
            logger.info("Pattern analysis loop cancelled (shutdown)")
        else:
            logger.info("Pattern analysis loop reached max runtime, exiting")
