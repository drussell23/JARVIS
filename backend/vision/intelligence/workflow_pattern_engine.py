#!/usr/bin/env python3
"""
Workflow Pattern Engine - Advanced Pattern Learning for JARVIS Vision System
Dynamic workflow pattern discovery, formation, and application with zero hardcoding
Memory allocation: 120MB total

This engine learns user work patterns including:
- Daily routines and workflows
- Task execution patterns  
- Problem-solving approaches
- Context-dependent behaviors
- Temporal patterns and sequences

All patterns are learned dynamically from observation - no hardcoded rules.
"""

import asyncio
import json
import logging
import os
import pickle
import hashlib
import statistics
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any, Deque, Callable, Union
from collections import defaultdict, deque, Counter
import numpy as np
import weakref
import threading
from concurrent.futures import ThreadPoolExecutor

# Import managed executor for clean shutdown
try:
    from core.thread_manager import ManagedThreadPoolExecutor
    _HAS_MANAGED_EXECUTOR = True
except ImportError:
    _HAS_MANAGED_EXECUTOR = False


# Import existing systems for integration
from .visual_state_management_system import VisualStateManagementSystem, ApplicationStateTracker
from .activity_recognition_engine import ActivityRecognitionEngine, RecognizedTask, PrimaryActivity
from .goal_inference_system import GoalInferenceEngine, Goal, GoalLevel

logger = logging.getLogger(__name__)

# Memory allocation constants (120MB total)
MEMORY_LIMITS = {
    'pattern_mining': 40 * 1024 * 1024,      # 40MB - Sequence mining and extraction
    'pattern_formation': 45 * 1024 * 1024,    # 45MB - Clustering and pattern formation
    'pattern_application': 35 * 1024 * 1024,  # 35MB - Prediction and optimization
}


class PatternType(Enum):
    """Types of workflow patterns that can be learned"""
    ROUTINE_PATTERN = "routine_pattern"           # Daily/weekly routines
    TASK_PATTERN = "task_pattern"                 # Task execution sequences
    PROBLEM_SOLVING_PATTERN = "problem_solving"   # Problem-solving approaches
    CONTEXT_SWITCH_PATTERN = "context_switch"     # Context switching behaviors
    TEMPORAL_PATTERN = "temporal_pattern"         # Time-based patterns
    ADAPTIVE_PATTERN = "adaptive_pattern"         # Patterns that adapt to context
    COLLABORATIVE_PATTERN = "collaborative"       # Multi-user/multi-app patterns
    LEARNING_PATTERN = "learning_pattern"         # How user learns new things


class PatternConfidence(Enum):
    """Confidence levels for patterns"""
    EXPLORING = 0.3     # Just discovered, need more data
    EMERGING = 0.5      # Pattern forming but not stable
    ESTABLISHED = 0.7   # Stable pattern with good evidence
    CONFIDENT = 0.9     # Strong pattern, reliable for prediction
    CERTAIN = 0.95      # Extremely reliable pattern


class PatternScope(Enum):
    """Scope of pattern applicability"""
    PERSONAL = "personal"         # User-specific patterns
    APPLICATION = "application"   # App-specific patterns
    TASK_TYPE = "task_type"      # Task category patterns  
    TEMPORAL = "temporal"        # Time-context patterns
    CONTEXTUAL = "contextual"    # Situation-specific patterns
    UNIVERSAL = "universal"      # Generally applicable patterns


@dataclass
class WorkflowEvent:
    """Individual event in a workflow sequence"""
    timestamp: datetime
    event_type: str  # 'state_change', 'action', 'goal_update', etc.
    source_system: str  # 'vsms', 'activity_recognition', 'goal_inference'
    event_data: Dict[str, Any]
    context: Dict[str, Any] = field(default_factory=dict)
    sequence_id: Optional[str] = None
    
    def __hash__(self) -> int:
        """Make hashable for deduplication"""
        return hash((self.timestamp.isoformat(), self.event_type, self.source_system))
    
    def to_feature_vector(self) -> Dict[str, Any]:
        """Convert event to feature vector for pattern mining"""
        features = {
            'event_type': self.event_type,
            'source': self.source_system,
            'hour': self.timestamp.hour,
            'day_of_week': self.timestamp.weekday(),
            'app_count': len(self.context.get('active_applications', [])),
            'has_error': any('error' in str(v).lower() for v in self.event_data.values()),
            'interaction_count': len(self.context.get('interactions', []))
        }
        
        # Add semantic features from event data
        if 'confidence' in self.event_data:
            features['confidence'] = self.event_data['confidence']
        
        if 'state_id' in self.event_data:
            features['state_transition'] = True
            
        if 'task_name' in self.event_data:
            features['task_active'] = True
            
        return features


@dataclass  
class WorkflowSequence:
    """Sequence of related workflow events"""
    sequence_id: str
    events: List[WorkflowEvent] = field(default_factory=list)
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    context_signature: Optional[str] = None
    outcome: Optional[str] = None  # 'success', 'failure', 'abandoned'
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.events:
            self.start_time = min(e.timestamp for e in self.events)
            self.end_time = max(e.timestamp for e in self.events)
    
    @property
    def duration(self) -> timedelta:
        """Get sequence duration"""
        if self.start_time and self.end_time:
            return self.end_time - self.start_time
        return timedelta()
    
    def get_event_types_sequence(self) -> List[str]:
        """Get sequence of event types"""
        return [event.event_type for event in sorted(self.events, key=lambda e: e.timestamp)]
    
    def get_transition_pairs(self) -> List[Tuple[str, str]]:
        """Get pairs of consecutive event transitions"""
        event_types = self.get_event_types_sequence()
        return [(event_types[i], event_types[i+1]) for i in range(len(event_types)-1)]
    
    def calculate_similarity(self, other: 'WorkflowSequence') -> float:
        """Calculate similarity with another sequence"""
        if not self.events or not other.events:
            return 0.0
        
        # Compare event type sequences
        self_types = self.get_event_types_sequence()
        other_types = other.get_event_types_sequence()
        
        # Use sequence alignment similarity
        return self._sequence_similarity(self_types, other_types)
    
    def _sequence_similarity(self, seq1: List[str], seq2: List[str]) -> float:
        """Calculate sequence similarity using dynamic programming"""
        if not seq1 or not seq2:
            return 0.0
        
        m, n = len(seq1), len(seq2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        
        # Fill DP table
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i-1] == seq2[j-1]:
                    dp[i][j] = dp[i-1][j-1] + 1
                else:
                    dp[i][j] = max(dp[i-1][j], dp[i][j-1])
        
        # Return similarity as ratio of LCS to average length
        lcs_length = dp[m][n]
        avg_length = (m + n) / 2
        return lcs_length / avg_length if avg_length > 0 else 0.0


@dataclass
class WorkflowPattern:
    """Discovered workflow pattern"""
    pattern_id: str
    pattern_type: PatternType
    scope: PatternScope
    name: str = ""
    description: str = ""
    
    # Pattern definition
    event_sequence_template: List[str] = field(default_factory=list)
    transition_probabilities: Dict[Tuple[str, str], float] = field(default_factory=dict)
    context_conditions: Dict[str, Any] = field(default_factory=dict)
    temporal_constraints: Dict[str, Any] = field(default_factory=dict)
    
    # Learning data
    supporting_sequences: List[str] = field(default_factory=list)  # sequence IDs
    confidence: PatternConfidence = PatternConfidence.EXPLORING
    strength: float = 0.0  # Statistical strength of pattern
    frequency: int = 0  # How often pattern occurs
    
    # Metadata
    discovered_at: datetime = field(default_factory=datetime.now)
    last_observed: Optional[datetime] = None
    success_rate: float = 0.0
    average_duration: Optional[timedelta] = None
    
    # Adaptive properties
    adaptation_count: int = 0
    learned_variations: List[Dict[str, Any]] = field(default_factory=list)
    context_dependencies: Dict[str, float] = field(default_factory=dict)
    
    def update_from_sequence(self, sequence: WorkflowSequence):
        """Update pattern based on new sequence observation"""
        self.supporting_sequences.append(sequence.sequence_id)
        self.frequency += 1
        self.last_observed = datetime.now()
        
        # Update success rate
        if sequence.outcome:
            if sequence.outcome == 'success':
                self.success_rate = (self.success_rate * (self.frequency - 1) + 1.0) / self.frequency
            else:
                self.success_rate = (self.success_rate * (self.frequency - 1) + 0.0) / self.frequency
        
        # Update average duration
        if sequence.duration:
            if self.average_duration:
                total_seconds = (self.average_duration.total_seconds() * (self.frequency - 1) + 
                               sequence.duration.total_seconds()) / self.frequency
                self.average_duration = timedelta(seconds=total_seconds)
            else:
                self.average_duration = sequence.duration
        
        # Update confidence based on frequency and consistency
        self._update_confidence()
    
    def _update_confidence(self):
        """Update pattern confidence based on observations"""
        if self.frequency < 3:
            self.confidence = PatternConfidence.EXPLORING
        elif self.frequency < 8:
            self.confidence = PatternConfidence.EMERGING
        elif self.frequency < 15:
            self.confidence = PatternConfidence.ESTABLISHED
        elif self.success_rate > 0.8 and self.frequency >= 15:
            self.confidence = PatternConfidence.CONFIDENT
        elif self.success_rate > 0.9 and self.frequency >= 25:
            self.confidence = PatternConfidence.CERTAIN
    
    def predict_next_event(self, current_sequence: List[str]) -> Optional[Tuple[str, float]]:
        """Predict next event in sequence based on pattern"""
        if not current_sequence or not self.transition_probabilities:
            return None
        
        current_event = current_sequence[-1]
        
        # Find possible next events
        possible_next = {}
        for (from_event, to_event), probability in self.transition_probabilities.items():
            if from_event == current_event:
                possible_next[to_event] = probability
        
        if not possible_next:
            return None
        
        # Return most likely next event
        best_event = max(possible_next, key=possible_next.get)
        return best_event, possible_next[best_event]
    
    def matches_context(self, context: Dict[str, Any]) -> float:
        """Check how well pattern matches current context"""
        if not self.context_conditions:
            return 1.0  # No context constraints
        
        match_score = 0.0
        total_conditions = 0
        
        for condition, expected_value in self.context_conditions.items():
            total_conditions += 1
            if condition in context:
                if isinstance(expected_value, (list, set)):
                    if context[condition] in expected_value:
                        match_score += 1.0
                elif isinstance(expected_value, dict):
                    # Range or complex matching
                    if 'min' in expected_value and 'max' in expected_value:
                        value = context[condition]
                        if expected_value['min'] <= value <= expected_value['max']:
                            match_score += 1.0
                else:
                    if context[condition] == expected_value:
                        match_score += 1.0
        
        return match_score / total_conditions if total_conditions > 0 else 0.0


class SequenceMiner:
    """Mines sequences from workflow events using advanced algorithms"""
    
    def __init__(self, max_memory_mb: int = 40):
        self.max_memory_mb = max_memory_mb
        self.event_buffer: Deque[WorkflowEvent] = deque(maxlen=10000)
        self.active_sequences: Dict[str, WorkflowSequence] = {}
        self.completed_sequences: List[WorkflowSequence] = []
        
        # Sequence detection parameters
        self.max_gap_minutes = 15  # Max gap between events in sequence
        self.min_sequence_length = 3
        self.max_sequence_length = 50
        
        # Context-based sequence grouping
        self.context_weights = {
            'application': 0.4,
            'goal': 0.3,
            'task': 0.2,
            'temporal': 0.1
        }
        
    async def add_event(self, event: WorkflowEvent) -> Optional[List[WorkflowSequence]]:
        """Add event and potentially complete sequences"""
        self.event_buffer.append(event)
        
        # Try to assign event to existing sequence
        assigned = False
        for seq_id, sequence in list(self.active_sequences.items()):
            if self._should_extend_sequence(sequence, event):
                sequence.events.append(event)
                event.sequence_id = seq_id
                sequence.end_time = event.timestamp
                assigned = True
                break
        
        # Create new sequence if not assigned
        if not assigned:
            new_seq_id = self._generate_sequence_id(event)
            new_sequence = WorkflowSequence(
                sequence_id=new_seq_id,
                events=[event],
                start_time=event.timestamp,
                end_time=event.timestamp,
                context_signature=self._generate_context_signature(event)
            )
            event.sequence_id = new_seq_id
            self.active_sequences[new_seq_id] = new_sequence
        
        # Check for sequence completion
        completed = await self._check_sequence_completion()
        
        # Memory management
        await self._manage_memory()
        
        return completed
    
    def _should_extend_sequence(self, sequence: WorkflowSequence, event: WorkflowEvent) -> bool:
        """Determine if event should extend existing sequence"""
        if not sequence.events:
            return False
        
        last_event = sequence.events[-1]
        
        # Check time gap
        time_gap = event.timestamp - last_event.timestamp
        if time_gap > timedelta(minutes=self.max_gap_minutes):
            return False
        
        # Check context similarity
        context_similarity = self._calculate_context_similarity(
            last_event.context, event.context
        )
        
        return context_similarity > 0.5
    
    def _calculate_context_similarity(self, context1: Dict[str, Any], 
                                    context2: Dict[str, Any]) -> float:
        """Calculate similarity between event contexts"""
        if not context1 or not context2:
            return 0.0
        
        similarities = []
        
        # Application similarity
        apps1 = set(context1.get('active_applications', []))
        apps2 = set(context2.get('active_applications', []))
        if apps1 or apps2:
            app_sim = len(apps1 & apps2) / len(apps1 | apps2) if (apps1 | apps2) else 0
            similarities.append(app_sim * self.context_weights['application'])
        
        # Goal similarity
        goal1 = context1.get('current_goal')
        goal2 = context2.get('current_goal')
        if goal1 and goal2:
            goal_sim = 1.0 if goal1 == goal2 else 0.0
            similarities.append(goal_sim * self.context_weights['goal'])
        
        # Task similarity
        task1 = context1.get('current_task')
        task2 = context2.get('current_task')
        if task1 and task2:
            task_sim = 1.0 if task1 == task2 else 0.0
            similarities.append(task_sim * self.context_weights['task'])
        
        # Temporal similarity
        time_diff = abs((context1.get('timestamp', datetime.now()) - 
                        context2.get('timestamp', datetime.now())).total_seconds())
        temp_sim = max(0, 1.0 - time_diff / 3600)  # 1-hour window
        similarities.append(temp_sim * self.context_weights['temporal'])
        
        return sum(similarities)
    
    async def _check_sequence_completion(self) -> List[WorkflowSequence]:
        """Check for completed sequences and move them"""
        completed = []
        current_time = datetime.now()
        to_complete = []
        
        for seq_id, sequence in self.active_sequences.items():
            # Complete if no activity for gap threshold
            if sequence.end_time:
                inactive_time = current_time - sequence.end_time
                if inactive_time > timedelta(minutes=self.max_gap_minutes * 2):
                    to_complete.append(seq_id)
            
            # Complete if sequence is too long
            if len(sequence.events) >= self.max_sequence_length:
                to_complete.append(seq_id)
        
        for seq_id in to_complete:
            sequence = self.active_sequences.pop(seq_id)
            
            # Only keep sequences with minimum length
            if len(sequence.events) >= self.min_sequence_length:
                # Determine outcome based on final events
                sequence.outcome = self._determine_sequence_outcome(sequence)
                
                self.completed_sequences.append(sequence)
                completed.append(sequence)
        
        return completed
    
    def _determine_sequence_outcome(self, sequence: WorkflowSequence) -> str:
        """Determine if sequence was successful, failed, or abandoned"""
        if not sequence.events:
            return 'unknown'
        
        # Look at final events for completion indicators
        final_events = sequence.events[-3:]  # Last 3 events
        
        success_indicators = ['completed', 'success', 'saved', 'sent', 'published']
        failure_indicators = ['error', 'failed', 'crashed', 'timeout']
        abandon_indicators = ['switched', 'closed', 'cancelled']
        
        for event in reversed(final_events):
            event_str = json.dumps(event.event_data).lower()
            
            if any(indicator in event_str for indicator in success_indicators):
                return 'success'
            elif any(indicator in event_str for indicator in failure_indicators):
                return 'failure'
            elif any(indicator in event_str for indicator in abandon_indicators):
                return 'abandoned'
        
        # Check duration - very short sequences might be abandoned
        if sequence.duration < timedelta(minutes=1):
            return 'abandoned'
        
        return 'unknown'
    
    def _generate_sequence_id(self, event: WorkflowEvent) -> str:
        """Generate unique sequence ID"""
        timestamp_str = event.timestamp.strftime('%Y%m%d_%H%M%S')
        event_hash = hashlib.md5(
            f"{event.event_type}_{event.source_system}".encode()
        ).hexdigest()[:8]
        return f"seq_{timestamp_str}_{event_hash}"
    
    def _generate_context_signature(self, event: WorkflowEvent) -> str:
        """Generate context signature for sequence grouping"""
        context_parts = []
        
        if 'active_applications' in event.context:
            apps = sorted(event.context['active_applications'])
            context_parts.append(f"apps:{','.join(apps[:3])}")
        
        if 'current_task' in event.context:
            context_parts.append(f"task:{event.context['current_task']}")
        
        if 'current_goal' in event.context:
            context_parts.append(f"goal:{event.context['current_goal']}")
        
        return "|".join(context_parts)
    
    async def _manage_memory(self):
        """Manage memory usage by pruning old data"""
        # Keep only recent completed sequences
        if len(self.completed_sequences) > 500:
            # Sort by recency and keep most recent 400
            self.completed_sequences.sort(key=lambda s: s.end_time or s.start_time, reverse=True)
            self.completed_sequences = self.completed_sequences[:400]
        
        # Limit event buffer size is already handled by deque maxlen
    
    def get_frequent_subsequences(self, min_support: int = 3) -> List[Tuple[List[str], int]]:
        """Find frequent event subsequences using mining algorithm"""
        if not self.completed_sequences:
            return []
        
        # Extract all event type sequences
        sequences = [seq.get_event_types_sequence() for seq in self.completed_sequences]
        
        # Generate candidate subsequences
        subsequence_counts = Counter()
        
        for sequence in sequences:
            # Generate all subsequences of length 2-5
            for length in range(2, min(6, len(sequence) + 1)):
                for i in range(len(sequence) - length + 1):
                    subseq = tuple(sequence[i:i + length])
                    subsequence_counts[subseq] += 1
        
        # Filter by minimum support
        frequent = [(list(subseq), count) for subseq, count in subsequence_counts.items()
                   if count >= min_support]
        
        # Sort by frequency
        frequent.sort(key=lambda x: x[1], reverse=True)
        
        return frequent[:50]  # Top 50 frequent subsequences


class PatternFormation:
    """Forms patterns from mined sequences using clustering and analysis"""
    
    def __init__(self, max_memory_mb: int = 45):
        self.max_memory_mb = max_memory_mb
        self.sequence_clusters: Dict[str, List[WorkflowSequence]] = {}
        self.pattern_templates: Dict[str, Dict[str, Any]] = {}
        self.clustering_threshold = 0.7
        
    async def form_patterns(self, sequences: List[WorkflowSequence]) -> List[WorkflowPattern]:
        """Form workflow patterns from sequences"""
        if not sequences:
            return []
        
        # Step 1: Cluster similar sequences
        clusters = await self._cluster_sequences(sequences)
        
        # Step 2: Extract patterns from each cluster
        patterns = []
        for cluster_id, cluster_sequences in clusters.items():
            if len(cluster_sequences) >= 3:  # Need minimum sequences for pattern
                pattern = await self._extract_pattern_from_cluster(cluster_id, cluster_sequences)
                if pattern:
                    patterns.append(pattern)
        
        return patterns
    
    async def _cluster_sequences(self, sequences: List[WorkflowSequence]) -> Dict[str, List[WorkflowSequence]]:
        """Cluster sequences by similarity"""
        clusters = {}
        cluster_id = 0
        
        for sequence in sequences:
            best_cluster = None
            best_similarity = 0.0
            
            # Find best matching cluster
            for cid, cluster_seqs in clusters.items():
                # Calculate average similarity to cluster
                similarities = [sequence.calculate_similarity(cs) for cs in cluster_seqs]
                avg_similarity = statistics.mean(similarities) if similarities else 0.0
                
                if avg_similarity > best_similarity and avg_similarity >= self.clustering_threshold:
                    best_similarity = avg_similarity
                    best_cluster = cid
            
            # Add to best cluster or create new one
            if best_cluster is not None:
                clusters[best_cluster].append(sequence)
            else:
                clusters[f"cluster_{cluster_id}"] = [sequence]
                cluster_id += 1
        
        return clusters
    
    async def _extract_pattern_from_cluster(self, cluster_id: str, 
                                          sequences: List[WorkflowSequence]) -> Optional[WorkflowPattern]:
        """Extract workflow pattern from sequence cluster"""
        if len(sequences) < 3:
            return None
        
        # Analyze common event sequence template
        event_sequences = [seq.get_event_types_sequence() for seq in sequences]
        common_template = self._find_common_sequence_template(event_sequences)
        
        if not common_template:
            return None
        
        # Calculate transition probabilities
        transition_probs = self._calculate_transition_probabilities(event_sequences)
        
        # Analyze context conditions
        context_conditions = self._analyze_context_conditions(sequences)
        
        # Determine pattern type
        pattern_type = self._classify_pattern_type(sequences, common_template)
        
        # Determine pattern scope
        pattern_scope = self._determine_pattern_scope(sequences)
        
        # Calculate success rate and duration
        success_rate = self._calculate_success_rate(sequences)
        avg_duration = self._calculate_average_duration(sequences)
        
        # Create pattern
        pattern = WorkflowPattern(
            pattern_id=f"pattern_{cluster_id}_{datetime.now().timestamp()}",
            pattern_type=pattern_type,
            scope=pattern_scope,
            name=self._generate_pattern_name(pattern_type, common_template),
            description=self._generate_pattern_description(pattern_type, common_template, context_conditions),
            event_sequence_template=common_template,
            transition_probabilities=transition_probs,
            context_conditions=context_conditions,
            supporting_sequences=[seq.sequence_id for seq in sequences],
            frequency=len(sequences),
            success_rate=success_rate,
            average_duration=avg_duration
        )
        
        # Update confidence based on cluster quality
        pattern._update_confidence()
        
        return pattern
    
    def _find_common_sequence_template(self, sequences: List[List[str]]) -> List[str]:
        """Find common event sequence template from multiple sequences"""
        if not sequences:
            return []
        
        # Use dynamic programming to find longest common subsequence pattern
        # For simplicity, we'll find the most common events at each position
        
        max_length = max(len(seq) for seq in sequences)
        common_template = []
        
        for pos in range(max_length):
            # Get events at this position from all sequences that are long enough
            events_at_pos = [seq[pos] for seq in sequences if len(seq) > pos]
            
            if not events_at_pos:
                break
            
            # Find most common event at this position
            event_counts = Counter(events_at_pos)
            most_common = event_counts.most_common(1)[0]
            
            # Include if it appears in at least 60% of sequences
            if most_common[1] / len(events_at_pos) >= 0.6:
                common_template.append(most_common[0])
            else:
                # Use wildcard for variable positions
                common_template.append("*")
        
        return common_template
    
    def _calculate_transition_probabilities(self, sequences: List[List[str]]) -> Dict[Tuple[str, str], float]:
        """Calculate transition probabilities between events"""
        transition_counts = Counter()
        total_transitions = 0
        
        for sequence in sequences:
            for i in range(len(sequence) - 1):
                transition = (sequence[i], sequence[i + 1])
                transition_counts[transition] += 1
                total_transitions += 1
        
        # Convert counts to probabilities
        transition_probs = {}
        for transition, count in transition_counts.items():
            transition_probs[transition] = count / total_transitions
        
        return transition_probs
    
    def _analyze_context_conditions(self, sequences: List[WorkflowSequence]) -> Dict[str, Any]:
        """Analyze common context conditions across sequences"""
        context_conditions = {}
        
        # Analyze applications
        app_lists = []
        for seq in sequences:
            for event in seq.events:
                if 'active_applications' in event.context:
                    app_lists.append(set(event.context['active_applications']))
        
        if app_lists:
            # Find common applications
            common_apps = set.intersection(*app_lists) if app_lists else set()
            if common_apps:
                context_conditions['required_applications'] = list(common_apps)
        
        # Analyze time patterns
        hours = [seq.start_time.hour for seq in sequences if seq.start_time]
        if hours:
            hour_counter = Counter(hours)
            common_hours = [h for h, count in hour_counter.items() if count >= len(sequences) * 0.5]
            if common_hours:
                context_conditions['preferred_hours'] = common_hours
        
        # Analyze day of week patterns
        days = [seq.start_time.weekday() for seq in sequences if seq.start_time]
        if days:
            day_counter = Counter(days)
            common_days = [d for d, count in day_counter.items() if count >= len(sequences) * 0.4]
            if common_days:
                context_conditions['preferred_days'] = common_days
        
        return context_conditions
    
    def _classify_pattern_type(self, sequences: List[WorkflowSequence], 
                             template: List[str]) -> PatternType:
        """Classify the type of workflow pattern"""
        # Analyze temporal characteristics
        durations = [seq.duration.total_seconds() for seq in sequences if seq.duration]
        avg_duration = statistics.mean(durations) if durations else 0
        
        # Analyze event types
        event_types = set()
        for seq in sequences:
            event_types.update(event.event_type for event in seq.events)
        
        # Classification logic based on characteristics
        if avg_duration > 3600:  # > 1 hour
            return PatternType.ROUTINE_PATTERN
        elif any('error' in et or 'debug' in et for et in event_types):
            return PatternType.PROBLEM_SOLVING_PATTERN
        elif any('switch' in et or 'context' in et for et in event_types):
            return PatternType.CONTEXT_SWITCH_PATTERN
        elif len(template) > 10:
            return PatternType.TASK_PATTERN
        else:
            return PatternType.ADAPTIVE_PATTERN
    
    def _determine_pattern_scope(self, sequences: List[WorkflowSequence]) -> PatternScope:
        """Determine the scope of pattern applicability"""
        # Analyze context diversity
        all_apps = set()
        for seq in sequences:
            for event in seq.events:
                all_apps.update(event.context.get('active_applications', []))
        
        if len(all_apps) == 1:
            return PatternScope.APPLICATION
        elif len(all_apps) <= 3:
            return PatternScope.TASK_TYPE
        else:
            return PatternScope.PERSONAL
    
    def _calculate_success_rate(self, sequences: List[WorkflowSequence]) -> float:
        """Calculate success rate of sequences"""
        outcomes = [seq.outcome for seq in sequences if seq.outcome]
        if not outcomes:
            return 0.5  # Unknown
        
        success_count = sum(1 for outcome in outcomes if outcome == 'success')
        return success_count / len(outcomes)
    
    def _calculate_average_duration(self, sequences: List[WorkflowSequence]) -> Optional[timedelta]:
        """Calculate average duration of sequences"""
        durations = [seq.duration for seq in sequences if seq.duration]
        if not durations:
            return None
        
        total_seconds = sum(d.total_seconds() for d in durations)
        avg_seconds = total_seconds / len(durations)
        return timedelta(seconds=avg_seconds)
    
    def _generate_pattern_name(self, pattern_type: PatternType, template: List[str]) -> str:
        """Generate human-readable pattern name"""
        type_names = {
            PatternType.ROUTINE_PATTERN: "Daily Routine",
            PatternType.TASK_PATTERN: "Task Workflow",
            PatternType.PROBLEM_SOLVING_PATTERN: "Problem Solving",
            PatternType.CONTEXT_SWITCH_PATTERN: "Context Switch",
            PatternType.TEMPORAL_PATTERN: "Temporal Pattern",
            PatternType.ADAPTIVE_PATTERN: "Adaptive Workflow",
            PatternType.COLLABORATIVE_PATTERN: "Collaborative Work",
            PatternType.LEARNING_PATTERN: "Learning Process"
        }
        
        base_name = type_names.get(pattern_type, "Unknown Pattern")
        
        # Add template hint
        if template:
            key_events = [event for event in template[:3] if event != "*"]
            if key_events:
                hint = " - " + " → ".join(key_events)
                return base_name + hint
        
        return base_name
    
    def _generate_pattern_description(self, pattern_type: PatternType, 
                                    template: List[str], 
                                    conditions: Dict[str, Any]) -> str:
        """Generate human-readable pattern description"""
        desc_parts = []
        
        # Base description
        type_descriptions = {
            PatternType.ROUTINE_PATTERN: "A recurring workflow pattern executed regularly",
            PatternType.TASK_PATTERN: "A systematic approach to completing specific tasks",
            PatternType.PROBLEM_SOLVING_PATTERN: "A methodology for diagnosing and fixing issues",
            PatternType.CONTEXT_SWITCH_PATTERN: "A pattern for switching between different work contexts",
            PatternType.TEMPORAL_PATTERN: "A time-dependent workflow with temporal constraints",
            PatternType.ADAPTIVE_PATTERN: "A flexible workflow that adapts to different situations",
            PatternType.COLLABORATIVE_PATTERN: "A pattern involving multi-user or multi-application coordination",
            PatternType.LEARNING_PATTERN: "A pattern for acquiring new knowledge or skills"
        }
        
        desc_parts.append(type_descriptions.get(pattern_type, "A workflow pattern"))
        
        # Add context conditions
        if 'required_applications' in conditions:
            apps = conditions['required_applications']
            desc_parts.append(f"typically involving {', '.join(apps)}")
        
        if 'preferred_hours' in conditions:
            hours = conditions['preferred_hours']
            if len(hours) <= 3:
                hour_desc = f"usually performed around {', '.join(map(str, hours))}:00"
                desc_parts.append(hour_desc)
        
        return ". ".join(desc_parts) + "."


class PatternApplication:
    """Applies learned patterns for prediction and optimization"""
    
    def __init__(self, max_memory_mb: int = 35):
        self.max_memory_mb = max_memory_mb
        self.active_patterns: Dict[str, WorkflowPattern] = {}
        self.pattern_performance: Dict[str, Dict[str, float]] = defaultdict(dict)
        self.prediction_cache: Dict[str, Tuple[Any, datetime]] = {}
        self.cache_ttl = timedelta(minutes=5)
        
    async def predict_next_actions(self, current_context: Dict[str, Any],
                                 current_sequence: List[str]) -> List[Tuple[str, float, str]]:
        """Predict next likely actions based on learned patterns"""
        # Create cache key
        cache_key = self._create_cache_key(current_context, current_sequence)
        
        # Check cache
        if cache_key in self.prediction_cache:
            result, timestamp = self.prediction_cache[cache_key]
            if datetime.now() - timestamp < self.cache_ttl:
                return result
        
        predictions = []
        
        # Find matching patterns
        matching_patterns = await self._find_matching_patterns(current_context)
        
        for pattern in matching_patterns:
            context_match = pattern.matches_context(current_context)
            if context_match > 0.5:
                # Get prediction from pattern
                prediction = pattern.predict_next_event(current_sequence)
                if prediction:
                    next_event, probability = prediction
                    confidence = probability * context_match * pattern.confidence.value
                    predictions.append((next_event, confidence, pattern.pattern_id))
        
        # Sort by confidence and remove duplicates
        predictions.sort(key=lambda x: x[1], reverse=True)
        unique_predictions = []
        seen_events = set()
        
        for event, confidence, pattern_id in predictions:
            if event not in seen_events:
                unique_predictions.append((event, confidence, pattern_id))
                seen_events.add(event)
        
        # Cache result
        self.prediction_cache[cache_key] = (unique_predictions[:5], datetime.now())
        
        return unique_predictions[:5]  # Top 5 predictions
    
    async def suggest_workflow_optimizations(self, current_workflow: WorkflowSequence) -> List[Dict[str, Any]]:
        """Suggest optimizations for current workflow"""
        suggestions = []
        
        # Find similar successful patterns
        similar_patterns = await self._find_similar_patterns(current_workflow)
        
        for pattern in similar_patterns:
            if pattern.success_rate > 0.8 and pattern.confidence.value > 0.7:
                # Compare current workflow with pattern
                optimization = await self._compare_with_pattern(current_workflow, pattern)
                if optimization:
                    suggestions.append(optimization)
        
        # Sort by potential impact
        suggestions.sort(key=lambda x: x.get('impact_score', 0), reverse=True)
        
        return suggestions[:3]  # Top 3 suggestions
    
    async def adapt_pattern_to_context(self, pattern: WorkflowPattern, 
                                     current_context: Dict[str, Any]) -> Optional[WorkflowPattern]:
        """Adapt a pattern to current context"""
        if pattern.scope == PatternScope.UNIVERSAL:
            return pattern  # No adaptation needed
        
        # Create adapted version
        adapted_pattern = WorkflowPattern(
            pattern_id=f"{pattern.pattern_id}_adapted_{datetime.now().timestamp()}",
            pattern_type=pattern.pattern_type,
            scope=PatternScope.CONTEXTUAL,
            name=f"Adapted {pattern.name}",
            description=f"Context-adapted version: {pattern.description}",
            event_sequence_template=pattern.event_sequence_template.copy(),
            transition_probabilities=pattern.transition_probabilities.copy(),
            context_conditions=self._adapt_context_conditions(
                pattern.context_conditions, current_context
            ),
            confidence=PatternConfidence.EMERGING  # Start with lower confidence
        )
        
        return adapted_pattern
    
    async def evaluate_pattern_performance(self, pattern_id: str, 
                                         actual_outcome: str) -> Dict[str, float]:
        """Evaluate how well a pattern performed"""
        if pattern_id not in self.pattern_performance:
            self.pattern_performance[pattern_id] = {
                'prediction_accuracy': 0.0,
                'outcome_accuracy': 0.0,
                'usage_count': 0,
                'success_rate': 0.0
            }
        
        perf = self.pattern_performance[pattern_id]
        perf['usage_count'] += 1
        
        # Update success rate
        is_successful = actual_outcome == 'success'
        perf['success_rate'] = ((perf['success_rate'] * (perf['usage_count'] - 1)) + 
                               (1.0 if is_successful else 0.0)) / perf['usage_count']
        
        return perf
    
    async def _find_matching_patterns(self, context: Dict[str, Any]) -> List[WorkflowPattern]:
        """Find patterns that match current context"""
        matching = []
        
        for pattern in self.active_patterns.values():
            match_score = pattern.matches_context(context)
            if match_score > 0.3:  # Minimum threshold
                matching.append(pattern)
        
        # Sort by confidence and match score
        matching.sort(key=lambda p: p.confidence.value, reverse=True)
        
        return matching
    
    async def _find_similar_patterns(self, workflow: WorkflowSequence) -> List[WorkflowPattern]:
        """Find patterns similar to current workflow"""
        similar = []
        current_events = workflow.get_event_types_sequence()
        
        for pattern in self.active_patterns.values():
            # Calculate similarity with pattern template
            similarity = self._calculate_template_similarity(current_events, pattern.event_sequence_template)
            if similarity > 0.6:
                similar.append(pattern)
        
        return similar
    
    def _calculate_template_similarity(self, sequence1: List[str], sequence2: List[str]) -> float:
        """Calculate similarity between event sequences"""
        if not sequence1 or not sequence2:
            return 0.0
        
        # Use longest common subsequence ratio
        lcs_length = self._lcs_length(sequence1, sequence2)
        max_length = max(len(sequence1), len(sequence2))
        
        return lcs_length / max_length if max_length > 0 else 0.0
    
    def _lcs_length(self, seq1: List[str], seq2: List[str]) -> int:
        """Calculate longest common subsequence length"""
        m, n = len(seq1), len(seq2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]
        
        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i-1] == seq2[j-1]:
                    dp[i][j] = dp[i-1][j-1] + 1
                else:
                    dp[i][j] = max(dp[i-1][j], dp[i][j-1])
        
        return dp[m][n]
    
    async def _compare_with_pattern(self, workflow: WorkflowSequence, 
                                   pattern: WorkflowPattern) -> Optional[Dict[str, Any]]:
        """Compare workflow with pattern and suggest improvements"""
        current_events = workflow.get_event_types_sequence()
        template_events = pattern.event_sequence_template
        
        # Find differences
        differences = []
        missing_events = []
        extra_events = []
        
        # Simple diff - can be enhanced with more sophisticated algorithm
        current_set = set(current_events)
        template_set = set(event for event in template_events if event != "*")
        
        missing_events = list(template_set - current_set)
        extra_events = list(current_set - template_set)
        
        if not missing_events and not extra_events:
            return None  # No significant differences
        
        # Calculate potential impact
        impact_score = (len(missing_events) * 0.7 + len(extra_events) * 0.3) / len(template_events)
        
        # Generate suggestion
        suggestion = {
            'type': 'workflow_optimization',
            'pattern_id': pattern.pattern_id,
            'pattern_name': pattern.name,
            'impact_score': impact_score,
            'description': f"Optimize workflow based on successful pattern '{pattern.name}'",
            'missing_steps': missing_events,
            'unnecessary_steps': extra_events,
            'expected_improvement': {
                'success_rate': pattern.success_rate,
                'time_savings': self._estimate_time_savings(workflow, pattern)
            }
        }
        
        return suggestion
    
    def _estimate_time_savings(self, current: WorkflowSequence, pattern: WorkflowPattern) -> Optional[str]:
        """Estimate potential time savings"""
        if not pattern.average_duration or not current.duration:
            return None
        
        current_seconds = current.duration.total_seconds()
        pattern_seconds = pattern.average_duration.total_seconds()
        
        if pattern_seconds < current_seconds:
            savings = current_seconds - pattern_seconds
            if savings > 60:
                return f"{savings/60:.1f} minutes"
            else:
                return f"{savings:.0f} seconds"
        
        return None
    
    def _adapt_context_conditions(self, original_conditions: Dict[str, Any], 
                                current_context: Dict[str, Any]) -> Dict[str, Any]:
        """Adapt context conditions to current situation"""
        adapted = original_conditions.copy()
        
        # Update applications if current context has different apps
        if 'active_applications' in current_context:
            current_apps = set(current_context['active_applications'])
            if 'required_applications' in adapted:
                required_apps = set(adapted['required_applications'])
                # Blend required apps with current apps
                blended_apps = list(required_apps | current_apps)
                adapted['required_applications'] = blended_apps
        
        # Update temporal conditions
        if 'timestamp' in current_context:
            current_time = current_context['timestamp']
            if isinstance(current_time, datetime):
                adapted['preferred_hours'] = [current_time.hour]
                adapted['preferred_days'] = [current_time.weekday()]
        
        return adapted
    
    def _create_cache_key(self, context: Dict[str, Any], sequence: List[str]) -> str:
        """Create cache key for predictions"""
        context_str = json.dumps(context, sort_keys=True, default=str)
        sequence_str = "|".join(sequence)
        combined = f"{context_str}#{sequence_str}"
        return hashlib.md5(combined.encode()).hexdigest()


class WorkflowPatternEngine:
    """Main engine coordinating all pattern learning components"""
    
    def __init__(self, vsms: Optional[VisualStateManagementSystem] = None,
                 activity_engine: Optional[ActivityRecognitionEngine] = None,
                 goal_engine: Optional[GoalInferenceEngine] = None):
        """Initialize with existing system integrations"""
        
        # Core components
        self.sequence_miner = SequenceMiner(max_memory_mb=40)
        self.pattern_formation = PatternFormation(max_memory_mb=45)
        self.pattern_application = PatternApplication(max_memory_mb=35)
        
        # System integrations
        self.vsms = vsms
        self.activity_engine = activity_engine  
        self.goal_engine = goal_engine
        
        # Pattern storage
        self.learned_patterns: Dict[str, WorkflowPattern] = {}
        self.pattern_history: Deque[WorkflowPattern] = deque(maxlen=1000)
        
        # Learning state
        self.learning_enabled = True
        self.min_sequences_for_pattern = 3
        self.pattern_cleanup_interval = 3600  # 1 hour
        
        # Performance tracking
        self.metrics = {
            'patterns_learned': 0,
            'sequences_processed': 0,
            'predictions_made': 0,
            'optimizations_suggested': 0,
            'memory_usage': {}
        }
        
        # Threading for async processing
        if _HAS_MANAGED_EXECUTOR:

            self.executor = ManagedThreadPoolExecutor(max_workers=3, name='pool')

        else:

            self.executor = ThreadPoolExecutor(max_workers=3)
        self._cleanup_task = None
        
        # Persistence
        self.state_path = Path("workflow_patterns")
        self.state_path.mkdir(exist_ok=True)
        
        logger.info("Workflow Pattern Engine initialized with 120MB memory allocation")
    
    async def start(self):
        """Start the pattern engine"""
        await self._load_saved_patterns()
        
        # Start background tasks
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        
        # Connect to existing systems
        if self.vsms:
            logger.info("Connected to Visual State Management System")
        if self.activity_engine:
            logger.info("Connected to Activity Recognition Engine")
        if self.goal_engine:
            logger.info("Connected to Goal Inference Engine")
        
        logger.info("Workflow Pattern Engine started successfully")
    
    async def stop(self):
        """Stop the pattern engine and save state"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
        
        await self._save_patterns()
        self.executor.shutdown(wait=True)
        
        logger.info("Workflow Pattern Engine stopped")
    
    async def process_vision_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Process event from vision system and potentially learn patterns"""
        if not self.learning_enabled:
            return {'status': 'learning_disabled'}
        
        # Create workflow event from vision data
        event = await self._create_workflow_event(event_data, 'vision')
        
        # Process event through sequence miner
        completed_sequences = await self.sequence_miner.add_event(event)
        
        # Form patterns from completed sequences
        if completed_sequences:
            new_patterns = await self.pattern_formation.form_patterns(completed_sequences)
            
            for pattern in new_patterns:
                await self._register_pattern(pattern)
        
        # Generate predictions and suggestions
        result = await self._generate_insights(event)
        
        # Update metrics
        self.metrics['sequences_processed'] += len(completed_sequences)
        self.metrics['memory_usage'] = await self._calculate_memory_usage()
        
        return result
    
    async def process_activity_event(self, task: RecognizedTask) -> Dict[str, Any]:
        """Process event from activity recognition system"""
        if not self.learning_enabled:
            return {'status': 'learning_disabled'}
        
        # Create workflow event from activity data
        event_data = {
            'task_id': task.task_id,
            'task_name': task.name,
            'activity': task.primary_activity.value,
            'confidence': task.confidence,
            'progress': task.completion_percentage,
            'status': task.status.name,
            'active_apps': list(task.active_applications)
        }
        
        event = await self._create_workflow_event(event_data, 'activity_recognition')
        completed_sequences = await self.sequence_miner.add_event(event)
        
        if completed_sequences:
            new_patterns = await self.pattern_formation.form_patterns(completed_sequences)
            for pattern in new_patterns:
                await self._register_pattern(pattern)
        
        return await self._generate_insights(event)
    
    async def process_goal_event(self, goal: Goal) -> Dict[str, Any]:
        """Process event from goal inference system"""
        if not self.learning_enabled:
            return {'status': 'learning_disabled'}
        
        # Create workflow event from goal data
        event_data = {
            'goal_id': goal.goal_id,
            'goal_type': goal.goal_type,
            'level': goal.level.name,
            'confidence': goal.confidence,
            'progress': goal.progress,
            'is_completed': goal.is_completed
        }
        
        event = await self._create_workflow_event(event_data, 'goal_inference')
        completed_sequences = await self.sequence_miner.add_event(event)
        
        if completed_sequences:
            new_patterns = await self.pattern_formation.form_patterns(completed_sequences)
            for pattern in new_patterns:
                await self._register_pattern(pattern)
        
        return await self._generate_insights(event)
    
    async def predict_workflow(self, current_context: Dict[str, Any]) -> Dict[str, Any]:
        """Predict next workflow steps based on learned patterns"""
        # Extract current sequence
        current_sequence = await self._extract_current_sequence(current_context)
        
        # Get predictions
        predictions = await self.pattern_application.predict_next_actions(
            current_context, current_sequence
        )
        
        # Get optimization suggestions
        if 'current_workflow' in current_context:
            optimizations = await self.pattern_application.suggest_workflow_optimizations(
                current_context['current_workflow']
            )
        else:
            optimizations = []
        
        # Update metrics
        self.metrics['predictions_made'] += len(predictions)
        self.metrics['optimizations_suggested'] += len(optimizations)
        
        return {
            'predictions': [
                {
                    'next_action': pred[0],
                    'confidence': pred[1],
                    'source_pattern': pred[2]
                } for pred in predictions
            ],
            'optimizations': optimizations,
            'pattern_count': len(self.learned_patterns),
            'confidence_level': self._calculate_overall_confidence()
        }
    
    async def get_pattern_insights(self) -> Dict[str, Any]:
        """Get insights about learned patterns"""
        patterns_by_type = defaultdict(int)
        patterns_by_confidence = defaultdict(int)
        avg_success_rates = defaultdict(list)
        
        for pattern in self.learned_patterns.values():
            patterns_by_type[pattern.pattern_type.value] += 1
            patterns_by_confidence[pattern.confidence.name] += 1
            avg_success_rates[pattern.pattern_type.value].append(pattern.success_rate)
        
        # Calculate averages
        for pattern_type, rates in avg_success_rates.items():
            avg_success_rates[pattern_type] = statistics.mean(rates) if rates else 0.0
        
        most_frequent_patterns = sorted(
            self.learned_patterns.values(),
            key=lambda p: p.frequency,
            reverse=True
        )[:5]
        
        return {
            'total_patterns': len(self.learned_patterns),
            'patterns_by_type': dict(patterns_by_type),
            'patterns_by_confidence': dict(patterns_by_confidence),
            'average_success_rates': dict(avg_success_rates),
            'most_frequent_patterns': [
                {
                    'name': p.name,
                    'type': p.pattern_type.value,
                    'frequency': p.frequency,
                    'success_rate': p.success_rate
                } for p in most_frequent_patterns
            ],
            'memory_usage': self.metrics['memory_usage'],
            'performance_metrics': self.metrics
        }
    
    async def _create_workflow_event(self, event_data: Dict[str, Any], 
                                   source_system: str) -> WorkflowEvent:
        """Create workflow event from system data"""
        # Determine event type based on source and data
        event_type = self._determine_event_type(event_data, source_system)
        
        # Gather current context
        context = await self._gather_current_context()
        context.update(event_data.get('context', {}))
        
        return WorkflowEvent(
            timestamp=datetime.now(),
            event_type=event_type,
            source_system=source_system,
            event_data=event_data,
            context=context
        )
    
    def _determine_event_type(self, data: Dict[str, Any], source: str) -> str:
        """Determine event type from data and source"""
        if source == 'vision':
            if 'state_change' in data:
                return 'state_change'
            elif 'screenshot' in data:
                return 'screen_capture'
            else:
                return 'vision_event'
        
        elif source == 'activity_recognition':
            if data.get('status') == 'COMPLETED':
                return 'task_completed'
            elif data.get('status') == 'IN_PROGRESS':
                return 'task_progress'
            else:
                return 'activity_event'
        
        elif source == 'goal_inference':
            if data.get('is_completed'):
                return 'goal_completed'
            else:
                return 'goal_update'
        
        return 'unknown_event'
    
    async def _gather_current_context(self) -> Dict[str, Any]:
        """Gather current context from all systems"""
        context = {
            'timestamp': datetime.now(),
            'active_applications': [],
            'current_task': None,
            'current_goal': None
        }
        
        # Get context from VSMS
        if self.vsms:
            # This would be implemented based on VSMS API
            pass
        
        # Get context from activity engine
        if self.activity_engine:
            active_tasks = self.activity_engine.get_current_activities()
            if active_tasks:
                task = active_tasks[0]  # Most recent task
                context['current_task'] = task.name
                context['active_applications'] = list(task.active_applications)
        
        # Get context from goal engine
        if self.goal_engine:
            goal_summary = self.goal_engine.get_active_goals_summary()
            if goal_summary['high_confidence']:
                context['current_goal'] = goal_summary['high_confidence'][0]['type']
        
        return context
    
    async def _register_pattern(self, pattern: WorkflowPattern):
        """Register a new learned pattern"""
        self.learned_patterns[pattern.pattern_id] = pattern
        self.pattern_history.append(pattern)
        self.pattern_application.active_patterns[pattern.pattern_id] = pattern
        
        self.metrics['patterns_learned'] += 1
        
        logger.info(f"Registered new pattern: {pattern.name} "
                   f"(confidence: {pattern.confidence.name}, frequency: {pattern.frequency})")
    
    async def _generate_insights(self, event: WorkflowEvent) -> Dict[str, Any]:
        """Generate insights based on current event and patterns"""
        context = event.context
        
        # Get current sequence for predictions
        current_sequence = []
        if event.sequence_id and event.sequence_id in self.sequence_miner.active_sequences:
            sequence = self.sequence_miner.active_sequences[event.sequence_id]
            current_sequence = sequence.get_event_types_sequence()
        
        # Generate predictions
        predictions = await self.pattern_application.predict_next_actions(
            context, current_sequence
        )
        
        return {
            'event_processed': True,
            'sequence_id': event.sequence_id,
            'predictions': predictions[:3],  # Top 3 predictions
            'pattern_matches': len([p for p in self.learned_patterns.values() 
                                  if p.matches_context(context) > 0.5]),
            'learning_active': self.learning_enabled
        }
    
    async def _extract_current_sequence(self, context: Dict[str, Any]) -> List[str]:
        """Extract current event sequence from context"""
        # This would extract the current sequence based on recent events
        # For now, return empty sequence
        return []
    
    def _calculate_overall_confidence(self) -> float:
        """Calculate overall pattern engine confidence"""
        if not self.learned_patterns:
            return 0.0
        
        confidences = [p.confidence.value for p in self.learned_patterns.values()]
        return statistics.mean(confidences)
    
    async def _calculate_memory_usage(self) -> Dict[str, int]:
        """Calculate memory usage of components"""
        import sys
        
        return {
            'sequence_miner': sys.getsizeof(self.sequence_miner),
            'pattern_formation': sys.getsizeof(self.pattern_formation),
            'pattern_application': sys.getsizeof(self.pattern_application),
            'learned_patterns': sum(sys.getsizeof(p) for p in self.learned_patterns.values()),
            'total_mb': sum([
                sys.getsizeof(self.sequence_miner),
                sys.getsizeof(self.pattern_formation),
                sys.getsizeof(self.pattern_application),
                sum(sys.getsizeof(p) for p in self.learned_patterns.values())
            ]) // (1024 * 1024)
        }
    
    async def _periodic_cleanup(self):
        """Periodic cleanup of old data"""
        max_runtime = float(os.getenv("TIMEOUT_VISION_SESSION", "3600.0"))  # 1 hour default
        session_start = time.monotonic()
        while time.monotonic() - session_start < max_runtime:
            try:
                await asyncio.sleep(self.pattern_cleanup_interval)

                # Clean up old prediction cache
                current_time = datetime.now()
                expired_keys = [
                    key for key, (_, timestamp) in self.pattern_application.prediction_cache.items()
                    if current_time - timestamp > self.pattern_application.cache_ttl
                ]
                for key in expired_keys:
                    del self.pattern_application.prediction_cache[key]

                # Clean up low-confidence patterns
                low_confidence_patterns = [
                    pid for pid, pattern in self.learned_patterns.items()
                    if pattern.confidence == PatternConfidence.EXPLORING and
                       (current_time - pattern.discovered_at) > timedelta(days=7)
                ]

                for pid in low_confidence_patterns:
                    if pid in self.learned_patterns:
                        del self.learned_patterns[pid]
                    if pid in self.pattern_application.active_patterns:
                        del self.pattern_application.active_patterns[pid]

                # Save state periodically
                await self._save_patterns()

                logger.debug(f"Cleanup complete. Active patterns: {len(self.learned_patterns)}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
        else:
            logger.info("Workflow pattern cleanup session timeout, stopping")
    
    async def _save_patterns(self):
        """Save learned patterns to disk"""
        try:
            # Save patterns
            patterns_file = self.state_path / "learned_patterns.json"
            patterns_data = {
                'patterns': {
                    pid: {
                        'pattern_type': pattern.pattern_type.value,
                        'scope': pattern.scope.value,
                        'name': pattern.name,
                        'description': pattern.description,
                        'event_sequence_template': pattern.event_sequence_template,
                        'confidence': pattern.confidence.name,
                        'frequency': pattern.frequency,
                        'success_rate': pattern.success_rate,
                        'discovered_at': pattern.discovered_at.isoformat(),
                        'last_observed': pattern.last_observed.isoformat() if pattern.last_observed else None
                    }
                    for pid, pattern in self.learned_patterns.items()
                },
                'metrics': self.metrics,
                'saved_at': datetime.now().isoformat()
            }
            
            with open(patterns_file, 'w') as f:
                json.dump(patterns_data, f, indent=2)
            
            logger.debug(f"Saved {len(self.learned_patterns)} patterns to {patterns_file}")
            
        except Exception as e:
            logger.error(f"Failed to save patterns: {e}")
    
    async def _load_saved_patterns(self):
        """Load saved patterns from disk"""
        try:
            patterns_file = self.state_path / "learned_patterns.json"
            
            if not patterns_file.exists():
                logger.info("No saved patterns found, starting fresh")
                return
            
            with open(patterns_file, 'r') as f:
                data = json.load(f)
            
            # Reconstruct patterns (simplified version)
            for pid, pattern_data in data.get('patterns', {}).items():
                # Note: This is a simplified reconstruction
                # Full implementation would restore all pattern properties
                pattern = WorkflowPattern(
                    pattern_id=pid,
                    pattern_type=PatternType(pattern_data['pattern_type']),
                    scope=PatternScope(pattern_data['scope']),
                    name=pattern_data['name'],
                    description=pattern_data['description'],
                    event_sequence_template=pattern_data.get('event_sequence_template', []),
                    frequency=pattern_data.get('frequency', 0),
                    success_rate=pattern_data.get('success_rate', 0.0)
                )
                
                # Set confidence
                confidence_name = pattern_data.get('confidence', 'EXPLORING')
                pattern.confidence = PatternConfidence[confidence_name]
                
                self.learned_patterns[pid] = pattern
                self.pattern_application.active_patterns[pid] = pattern
            
            # Load metrics
            if 'metrics' in data:
                self.metrics.update(data['metrics'])
            
            logger.info(f"Loaded {len(self.learned_patterns)} saved patterns")
            
        except Exception as e:
            logger.error(f"Failed to load saved patterns: {e}")


# Global instance management
_workflow_engine_instance = None

def get_workflow_pattern_engine(vsms=None, activity_engine=None, goal_engine=None) -> WorkflowPatternEngine:
    """Get or create the global Workflow Pattern Engine instance"""
    global _workflow_engine_instance
    if _workflow_engine_instance is None:
        _workflow_engine_instance = WorkflowPatternEngine(
            vsms=vsms,
            activity_engine=activity_engine, 
            goal_engine=goal_engine
        )
    return _workflow_engine_instance


# Test function
async def test_workflow_pattern_engine():
    """Test the Workflow Pattern Engine"""
    print("🔄 Testing Workflow Pattern Engine")
    print("=" * 50)
    
    engine = get_workflow_pattern_engine()
    await engine.start()
    
    try:
        # Test 1: Process mock vision events
        print("\n1️⃣ Testing vision event processing...")
        vision_events = [
            {
                'state_change': True,
                'app': 'vscode',
                'confidence': 0.9,
                'context': {'active_applications': ['vscode', 'terminal']}
            },
            {
                'state_change': True, 
                'app': 'terminal',
                'confidence': 0.8,
                'context': {'active_applications': ['terminal']}
            },
            {
                'screenshot': True,
                'analysis': 'code_editing',
                'context': {'active_applications': ['vscode']}
            }
        ]
        
        for event_data in vision_events:
            result = await engine.process_vision_event(event_data)
            print(f"   Processed event: {result.get('event_processed', False)}")
        
        # Test 2: Generate predictions
        print("\n2️⃣ Testing workflow predictions...")
        current_context = {
            'active_applications': ['vscode', 'chrome'],
            'timestamp': datetime.now(),
            'current_task': 'coding'
        }
        
        predictions = await engine.predict_workflow(current_context)
        print(f"   Generated {len(predictions['predictions'])} predictions")
        print(f"   Pattern confidence: {predictions['confidence_level']:.2f}")
        
        # Test 3: Get insights
        print("\n3️⃣ Getting pattern insights...")
        insights = await engine.get_pattern_insights()
        print(f"   Total patterns learned: {insights['total_patterns']}")
        print(f"   Memory usage: {insights['memory_usage'].get('total_mb', 0)} MB")
        
        print("\n✅ Workflow Pattern Engine test complete!")
        
    finally:
        await engine.stop()


if __name__ == "__main__":
    asyncio.run(test_workflow_pattern_engine())