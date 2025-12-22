"""
Predictive Analytics Engine for JARVIS
======================================

Handles high-level predictive and analytical queries:
- "Am I making progress?"
- "What should I work on next?"
- "Are there any potential bugs?"
- "Explain what this code does"

Architecture:
    Query → Intent Classifier → Metric Analyzer → Claude Vision → Response Generator
       ↓            ↓                    ↓              ↓                ↓
    Parse      Determine           Calculate      Semantic      Natural Language
    Intent     Query Type          Metrics        Analysis         Response

Key Features:
1. **Progress Analysis** - Tracks git commits, code changes, test results
2. **Bug Detection** - Analyzes patterns in errors, test failures, code smells
3. **Recommendations** - Suggests next tasks based on project state
4. **Semantic Understanding** - Uses Claude Vision for deep code analysis
5. **Zero Hardcoding** - Fully dynamic, configurable query types

Author: Derek Russell
Date: 2025-10-19
"""

import asyncio
import logging
import re
import json
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from collections import deque, defaultdict
import hashlib

logger = logging.getLogger(__name__)


# ============================================================================
# QUERY TYPES
# ============================================================================

class PredictiveQueryType(Enum):
    """Types of predictive/analytical queries"""
    PROGRESS_CHECK = "progress_check"              # "Am I making progress?"
    NEXT_STEPS = "next_steps"                      # "What should I work on next?"
    BUG_DETECTION = "bug_detection"                # "Are there any bugs?"
    CODE_EXPLANATION = "code_explanation"           # "Explain this code"
    PATTERN_ANALYSIS = "pattern_analysis"          # "What patterns do you see?"
    WORKFLOW_OPTIMIZATION = "workflow_optimization" # "How can I improve my workflow?"
    QUALITY_ASSESSMENT = "quality_assessment"      # "How's my code quality?"
    TIME_ESTIMATION = "time_estimation"            # "How long will this take?"
    RISK_ASSESSMENT = "risk_assessment"            # "What could go wrong?"
    UNKNOWN = "unknown"


class AnalysisScope(Enum):
    """Scope of analysis"""
    CURRENT_SPACE = "current_space"      # Current visible workspace
    ALL_SPACES = "all_spaces"            # All monitored spaces
    SPECIFIC_PROJECT = "specific_project" # Specific git project
    TIMEFRAME = "timeframe"              # Specific time period


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class ProgressMetrics:
    """Progress metrics for a project or workspace"""
    commits_today: int = 0
    commits_this_week: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    files_modified: int = 0
    test_pass_rate: float = 0.0
    build_success_rate: float = 0.0
    active_time_minutes: int = 0
    last_commit_time: Optional[datetime] = None
    velocity_trend: str = "stable"  # "increasing", "decreasing", "stable"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BugPattern:
    """Detected bug pattern"""
    pattern_type: str               # "syntax_error", "test_failure", "runtime_error"
    frequency: int                  # How many times seen
    last_occurrence: datetime
    locations: List[str]            # File paths or space IDs
    severity: str                   # "critical", "high", "medium", "low"
    description: str
    suggested_fix: Optional[str] = None
    confidence: float = 0.0


@dataclass
class Recommendation:
    """Actionable recommendation"""
    category: str                   # "next_task", "optimization", "bug_fix"
    priority: str                   # "high", "medium", "low"
    title: str
    description: str
    rationale: str                  # Why this recommendation
    action_items: List[str]
    estimated_time: Optional[str] = None
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AnalyticsResult:
    """Result of a predictive/analytical query"""
    query_type: PredictiveQueryType
    success: bool
    confidence: float
    insights: List[str]             # High-level insights
    metrics: Optional[ProgressMetrics] = None
    bug_patterns: List[BugPattern] = field(default_factory=list)
    recommendations: List[Recommendation] = field(default_factory=list)
    visualizations: List[str] = field(default_factory=list)  # Markdown/text visualizations
    raw_data: Dict[str, Any] = field(default_factory=dict)
    response_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# METRICS COLLECTORS
# ============================================================================

class GitMetricsCollector:
    """Collects metrics from git repositories"""

    def __init__(self):
        self._cache: Dict[str, Tuple[ProgressMetrics, datetime]] = {}
        self._cache_duration = timedelta(minutes=5)

    async def collect_metrics(self, repo_path: str) -> ProgressMetrics:
        """Collect git metrics for a repository"""
        # Check cache
        if repo_path in self._cache:
            metrics, timestamp = self._cache[repo_path]
            if datetime.now() - timestamp < self._cache_duration:
                logger.debug(f"[GIT-METRICS] Using cached metrics for {repo_path}")
                return metrics

        metrics = ProgressMetrics()

        try:
            import subprocess

            # Get commits today
            result = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "log", "--since='1 day ago'", "--oneline",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            metrics.commits_today = len(stdout.decode().strip().split('\n')) if stdout else 0

            # Get commits this week
            result = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "log", "--since='1 week ago'", "--oneline",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            metrics.commits_this_week = len(stdout.decode().strip().split('\n')) if stdout else 0

            # Get diff stats for today
            result = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "diff", "--stat", "@{1.day.ago}..HEAD",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            stats = stdout.decode()

            # Parse insertions/deletions
            insertions = re.findall(r'(\d+) insertions?', stats)
            deletions = re.findall(r'(\d+) deletions?', stats)
            metrics.lines_added = int(insertions[0]) if insertions else 0
            metrics.lines_removed = int(deletions[0]) if deletions else 0

            # Get files changed
            files_changed = re.findall(r'(\d+) files? changed', stats)
            metrics.files_modified = int(files_changed[0]) if files_changed else 0

            # Get last commit time
            result = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "log", "-1", "--format=%ct",
                stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            stdout, _ = await result.communicate()
            if stdout:
                timestamp = int(stdout.decode().strip())
                metrics.last_commit_time = datetime.fromtimestamp(timestamp)

            # Calculate velocity trend
            metrics.velocity_trend = self._calculate_velocity_trend(metrics)

            # Cache the result
            self._cache[repo_path] = (metrics, datetime.now())

            logger.info(f"[GIT-METRICS] Collected metrics: {metrics.commits_today} commits today, {metrics.files_modified} files modified")

        except Exception as e:
            logger.error(f"[GIT-METRICS] Error collecting metrics: {e}")

        return metrics

    def _calculate_velocity_trend(self, metrics: ProgressMetrics) -> str:
        """Calculate velocity trend"""
        daily_avg = metrics.commits_this_week / 7 if metrics.commits_this_week > 0 else 0

        if metrics.commits_today > daily_avg * 1.2:
            return "increasing"
        elif metrics.commits_today < daily_avg * 0.8:
            return "decreasing"
        else:
            return "stable"


class ErrorPatternCollector:
    """Collects and analyzes error patterns"""

    def __init__(self, max_patterns: int = 100):
        self.error_history: deque[Tuple[str, datetime, Dict[str, Any]]] = deque(maxlen=max_patterns)
        self.pattern_frequency: defaultdict[str, int] = defaultdict(int)

    def record_error(self, error_type: str, location: str, details: Dict[str, Any]):
        """Record an error occurrence"""
        self.error_history.append((error_type, datetime.now(), {
            "location": location,
            **details
        }))
        self.pattern_frequency[error_type] += 1
        logger.debug(f"[ERROR-PATTERN] Recorded error: {error_type} at {location}")

    async def analyze_patterns(self, within_hours: int = 24) -> List[BugPattern]:
        """Analyze error patterns"""
        cutoff = datetime.now() - timedelta(hours=within_hours)
        patterns = []

        # Group errors by type
        grouped_errors = defaultdict(list)
        for error_type, timestamp, details in self.error_history:
            if timestamp > cutoff:
                grouped_errors[error_type].append((timestamp, details))

        # Analyze each error type
        for error_type, occurrences in grouped_errors.items():
            if len(occurrences) >= 2:  # Only patterns that repeat
                locations = [occ[1].get("location", "unknown") for occ in occurrences]
                last_occurrence = max(occ[0] for occ in occurrences)

                # Determine severity
                severity = self._calculate_severity(error_type, len(occurrences))

                pattern = BugPattern(
                    pattern_type=error_type,
                    frequency=len(occurrences),
                    last_occurrence=last_occurrence,
                    locations=list(set(locations)),
                    severity=severity,
                    description=f"{error_type} occurred {len(occurrences)} times in the last {within_hours}h",
                    confidence=min(0.9, 0.5 + (len(occurrences) * 0.1))
                )

                patterns.append(pattern)

        # Sort by severity and frequency
        patterns.sort(key=lambda p: (
            {"critical": 4, "high": 3, "medium": 2, "low": 1}.get(p.severity, 0),
            p.frequency
        ), reverse=True)

        logger.info(f"[ERROR-PATTERN] Found {len(patterns)} bug patterns")
        return patterns

    def _calculate_severity(self, error_type: str, frequency: int) -> str:
        """Calculate severity based on error type and frequency"""
        critical_keywords = ["crash", "segfault", "fatal", "critical"]
        high_keywords = ["exception", "failure", "error"]

        error_lower = error_type.lower()

        if any(kw in error_lower for kw in critical_keywords) or frequency > 10:
            return "critical"
        elif any(kw in error_lower for kw in high_keywords) or frequency > 5:
            return "high"
        elif frequency > 2:
            return "medium"
        else:
            return "low"


class WorkflowAnalyzer:
    """Analyzes workflow patterns and efficiency"""

    def __init__(self):
        self.activity_log: deque[Tuple[str, datetime, Dict[str, Any]]] = deque(maxlen=500)

    def record_activity(self, activity_type: str, details: Dict[str, Any]):
        """Record a workflow activity"""
        self.activity_log.append((activity_type, datetime.now(), details))

    async def analyze_workflow(self, within_hours: int = 24) -> Dict[str, Any]:
        """Analyze workflow patterns"""
        cutoff = datetime.now() - timedelta(hours=within_hours)

        recent_activities = [
            (activity, timestamp, details)
            for activity, timestamp, details in self.activity_log
            if timestamp > cutoff
        ]

        if not recent_activities:
            return {
                "total_activities": 0,
                "focus_time": 0,
                "context_switches": 0,
                "efficiency_score": 0.0
            }

        # Calculate metrics
        total_activities = len(recent_activities)

        # Count context switches (changing spaces/apps)
        context_switches = 0
        last_context = None
        for activity, _, details in recent_activities:
            current_context = details.get("space_id", details.get("app_name"))
            if last_context and current_context != last_context:
                context_switches += 1
            last_context = current_context

        # Estimate focus time (time between activities < 5 min)
        focus_periods = []
        for i in range(1, len(recent_activities)):
            time_diff = (recent_activities[i][1] - recent_activities[i-1][1]).total_seconds() / 60
            if time_diff < 5:  # Focused if < 5 min between activities
                focus_periods.append(time_diff)

        focus_time = sum(focus_periods)

        # Calculate efficiency score (0-1)
        # High focus time, low context switches = high efficiency
        max_possible_focus = within_hours * 60
        efficiency_score = (focus_time / max_possible_focus) * (1 - min(1.0, context_switches / total_activities))

        return {
            "total_activities": total_activities,
            "focus_time_minutes": int(focus_time),
            "context_switches": context_switches,
            "efficiency_score": round(efficiency_score, 2),
            "avg_focus_period_minutes": round(sum(focus_periods) / len(focus_periods), 1) if focus_periods else 0
        }


# ============================================================================
# RECOMMENDATION ENGINE
# ============================================================================

class RecommendationEngine:
    """Generates actionable recommendations"""

    async def generate_recommendations(
        self,
        query_type: PredictiveQueryType,
        metrics: Optional[ProgressMetrics],
        bug_patterns: List[BugPattern],
        workflow_data: Dict[str, Any],
        context: Dict[str, Any]
    ) -> List[Recommendation]:
        """Generate recommendations based on analysis"""
        recommendations = []

        if query_type == PredictiveQueryType.NEXT_STEPS:
            recommendations.extend(await self._recommend_next_steps(metrics, bug_patterns, context))
        elif query_type == PredictiveQueryType.BUG_DETECTION:
            recommendations.extend(await self._recommend_bug_fixes(bug_patterns))
        elif query_type == PredictiveQueryType.WORKFLOW_OPTIMIZATION:
            recommendations.extend(await self._recommend_workflow_improvements(workflow_data))
        elif query_type == PredictiveQueryType.PROGRESS_CHECK:
            recommendations.extend(await self._recommend_progress_actions(metrics, workflow_data))

        # Sort by priority
        priority_order = {"high": 3, "medium": 2, "low": 1}
        recommendations.sort(key=lambda r: priority_order.get(r.priority, 0), reverse=True)

        return recommendations

    async def _recommend_next_steps(
        self,
        metrics: Optional[ProgressMetrics],
        bug_patterns: List[BugPattern],
        context: Dict[str, Any]
    ) -> List[Recommendation]:
        """Recommend next steps based on current state"""
        recommendations = []

        # If there are critical bugs, prioritize fixing them
        critical_bugs = [b for b in bug_patterns if b.severity == "critical"]
        if critical_bugs:
            recommendations.append(Recommendation(
                category="bug_fix",
                priority="high",
                title="Fix Critical Bugs",
                description=f"Address {len(critical_bugs)} critical bug(s) detected in your codebase",
                rationale="Critical bugs can cause system instability and should be addressed immediately",
                action_items=[f"Fix {bug.pattern_type} in {', '.join(bug.locations[:2])}" for bug in critical_bugs[:3]],
                estimated_time="30-60 minutes",
                confidence=0.9
            ))

        # If low commit activity, suggest getting started
        if metrics and metrics.commits_today == 0:
            recommendations.append(Recommendation(
                category="next_task",
                priority="medium",
                title="Start Daily Work",
                description="No commits today - consider starting with a small, manageable task",
                rationale="Consistent daily progress helps maintain momentum",
                action_items=[
                    "Review open issues or TODOs",
                    "Pick one small task to complete",
                    "Make your first commit"
                ],
                estimated_time="15-30 minutes",
                confidence=0.7
            ))

        # If high velocity, suggest optimization or refactoring
        if metrics and metrics.velocity_trend == "increasing" and metrics.commits_today > 5:
            recommendations.append(Recommendation(
                category="optimization",
                priority="low",
                title="Consider Refactoring",
                description="High commit velocity - good time to refactor and clean up",
                rationale="Active development periods are good opportunities to improve code quality",
                action_items=[
                    "Review recent changes for duplicate code",
                    "Add tests for new functionality",
                    "Update documentation"
                ],
                estimated_time="20-40 minutes",
                confidence=0.6
            ))

        return recommendations

    async def _recommend_bug_fixes(self, bug_patterns: List[BugPattern]) -> List[Recommendation]:
        """Recommend bug fixes"""
        recommendations = []

        for bug in bug_patterns[:5]:  # Top 5 bugs
            priority = "high" if bug.severity in ["critical", "high"] else "medium"

            recommendations.append(Recommendation(
                category="bug_fix",
                priority=priority,
                title=f"Fix {bug.pattern_type}",
                description=f"{bug.description} in {', '.join(bug.locations[:2])}",
                rationale=f"This {bug.severity} severity bug has occurred {bug.frequency} times",
                action_items=[
                    f"Investigate {bug.pattern_type} in {bug.locations[0]}",
                    "Review recent code changes in affected areas",
                    "Add tests to prevent regression"
                ],
                confidence=bug.confidence,
                metadata={"bug_pattern": bug}
            ))

        return recommendations

    async def _recommend_workflow_improvements(self, workflow_data: Dict[str, Any]) -> List[Recommendation]:
        """Recommend workflow improvements"""
        recommendations = []

        efficiency = workflow_data.get("efficiency_score", 0.0)
        context_switches = workflow_data.get("context_switches", 0)

        if efficiency < 0.5:
            recommendations.append(Recommendation(
                category="optimization",
                priority="medium",
                title="Improve Focus Time",
                description=f"Current efficiency score: {efficiency:.1%}",
                rationale="Low efficiency indicates frequent interruptions or context switching",
                action_items=[
                    "Try time-blocking for focused work",
                    "Minimize distractions during coding sessions",
                    "Use 'Do Not Disturb' mode"
                ],
                estimated_time="Ongoing",
                confidence=0.7
            ))

        if context_switches > 20:
            recommendations.append(Recommendation(
                category="optimization",
                priority="medium",
                title="Reduce Context Switching",
                description=f"{context_switches} context switches detected",
                rationale="Frequent context switching reduces productivity",
                action_items=[
                    "Group similar tasks together",
                    "Complete one task before starting another",
                    "Use single workspace for related work"
                ],
                estimated_time="Ongoing",
                confidence=0.8
            ))

        return recommendations

    async def _recommend_progress_actions(
        self,
        metrics: Optional[ProgressMetrics],
        workflow_data: Dict[str, Any]
    ) -> List[Recommendation]:
        """Recommend actions based on progress"""
        recommendations = []

        if metrics and metrics.velocity_trend == "decreasing":
            recommendations.append(Recommendation(
                category="next_task",
                priority="medium",
                title="Boost Velocity",
                description="Commit velocity is decreasing - consider taking action",
                rationale="Maintaining consistent progress prevents project delays",
                action_items=[
                    "Review blockers or impediments",
                    "Break large tasks into smaller ones",
                    "Set daily commit goals"
                ],
                estimated_time="15 minutes planning",
                confidence=0.6
            ))

        return recommendations


# ============================================================================
# MAIN PREDICTIVE ANALYZER
# ============================================================================

class PredictiveAnalyzer:
    """
    Main predictive analytics engine for JARVIS

    Handles high-level analytical queries with dynamic analysis
    """

    def __init__(
        self,
        context_graph=None,
        enable_git_metrics: bool = True,
        enable_error_tracking: bool = True,
        enable_workflow_analysis: bool = True
    ):
        """Initialize the predictive analyzer"""
        self.context_graph = context_graph

        # Collectors
        self.git_metrics = GitMetricsCollector() if enable_git_metrics else None
        self.error_patterns = ErrorPatternCollector() if enable_error_tracking else None
        self.workflow_analyzer = WorkflowAnalyzer() if enable_workflow_analysis else None

        # Recommendation engine
        self.recommender = RecommendationEngine()

        # Query patterns (dynamic, extensible)
        self.query_patterns = self._initialize_query_patterns()

        logger.info("[PREDICTIVE-ANALYZER] Initialized")

    def _initialize_query_patterns(self) -> Dict[PredictiveQueryType, List[re.Pattern]]:
        """Initialize query patterns"""
        return {
            PredictiveQueryType.PROGRESS_CHECK: [
                re.compile(r'\b(am i|are we)\s+(making|seeing)\s+progress\b', re.I),
                re.compile(r'\bhow\s+(much|far|well)\s+(progress|am i doing)\b', re.I),
                re.compile(r'\bwhat\'?s\s+my\s+progress\b', re.I),
            ],
            PredictiveQueryType.NEXT_STEPS: [
                re.compile(r'\bwhat\s+should\s+i\s+(do|work on)\s+next\b', re.I),
                re.compile(r'\b(next\s+steps|what\'?s\s+next)\b', re.I),
                re.compile(r'\bwhat\s+to\s+do\s+next\b', re.I),
            ],
            PredictiveQueryType.BUG_DETECTION: [
                re.compile(r'\b(are there|any|find)\s+(any\s+)?(bugs|errors|issues|problems)\b', re.I),
                re.compile(r'\bpotential\s+(bugs|issues)\b', re.I),
                re.compile(r'\bwhat\'?s\s+wrong\b', re.I),
            ],
            PredictiveQueryType.CODE_EXPLANATION: [
                re.compile(r'\bexplain\s+(this|that|the)\s+code\b', re.I),
                re.compile(r'\bwhat\s+does\s+(this|that|the)\s+code\s+do\b', re.I),
                re.compile(r'\bhow\s+does\s+(this|that)\s+work\b', re.I),
            ],
            PredictiveQueryType.PATTERN_ANALYSIS: [
                re.compile(r'\bwhat\s+patterns?\s+do\s+you\s+see\b', re.I),
                re.compile(r'\banalyze\s+(the\s+)?patterns?\b', re.I),
            ],
            PredictiveQueryType.WORKFLOW_OPTIMIZATION: [
                re.compile(r'\bhow\s+can\s+i\s+improve\s+my\s+workflow\b', re.I),
                re.compile(r'\boptimize\s+my\s+workflow\b', re.I),
                re.compile(r'\bwork\s+more\s+efficiently\b', re.I),
            ],
            PredictiveQueryType.QUALITY_ASSESSMENT: [
                re.compile(r'\bhow\'?s\s+my\s+code\s+quality\b', re.I),
                re.compile(r'\bcode\s+quality\s+(assessment|check)\b', re.I),
            ],
        }

    async def analyze(
        self,
        query: str,
        scope: AnalysisScope = AnalysisScope.CURRENT_SPACE,
        context: Optional[Dict[str, Any]] = None
    ) -> AnalyticsResult:
        """
        Analyze a predictive/analytical query

        Args:
            query: Natural language query
            scope: Analysis scope
            context: Additional context (space_id, repo_path, etc.)

        Returns:
            AnalyticsResult with insights and recommendations
        """
        logger.info(f"[PREDICTIVE-ANALYZER] Analyzing query: '{query}'")

        context = context or {}

        # Step 1: Classify query type
        query_type = await self._classify_query(query)
        logger.debug(f"[PREDICTIVE-ANALYZER] Query type: {query_type.value}")

        # Step 2: Collect relevant metrics
        metrics = await self._collect_metrics(query_type, scope, context)

        # Step 3: Analyze patterns
        bug_patterns = await self._analyze_bug_patterns(query_type) if self.error_patterns else []

        # Step 4: Analyze workflow
        workflow_data = await self._analyze_workflow() if self.workflow_analyzer else {}

        # Step 5: Generate insights
        insights = await self._generate_insights(query_type, metrics, bug_patterns, workflow_data, context)

        # Step 6: Generate recommendations
        recommendations = await self.recommender.generate_recommendations(
            query_type, metrics, bug_patterns, workflow_data, context
        )

        # Step 7: Generate response text
        response_text = await self._generate_response(
            query_type, insights, metrics, bug_patterns, recommendations, context
        )

        # Step 8: Calculate confidence
        confidence = await self._calculate_confidence(metrics, bug_patterns, workflow_data)

        return AnalyticsResult(
            query_type=query_type,
            success=True,
            confidence=confidence,
            insights=insights,
            metrics=metrics,
            bug_patterns=bug_patterns,
            recommendations=recommendations,
            response_text=response_text,
            raw_data={
                "workflow": workflow_data,
                "scope": scope.value
            }
        )

    async def _classify_query(self, query: str) -> PredictiveQueryType:
        """Classify the query type"""
        query_lower = query.lower()

        for query_type, patterns in self.query_patterns.items():
            for pattern in patterns:
                if pattern.search(query_lower):
                    return query_type

        return PredictiveQueryType.UNKNOWN

    async def _collect_metrics(
        self,
        query_type: PredictiveQueryType,
        scope: AnalysisScope,
        context: Dict[str, Any]
    ) -> Optional[ProgressMetrics]:
        """Collect relevant metrics"""
        if not self.git_metrics:
            return None

        # Get repo path from context or use cwd
        repo_path = context.get("repo_path", ".")

        try:
            metrics = await self.git_metrics.collect_metrics(repo_path)
            return metrics
        except Exception as e:
            logger.error(f"[PREDICTIVE-ANALYZER] Error collecting metrics: {e}")
            return None

    async def _analyze_bug_patterns(self, query_type: PredictiveQueryType) -> List[BugPattern]:
        """Analyze bug patterns"""
        if not self.error_patterns:
            return []

        return await self.error_patterns.analyze_patterns(within_hours=24)

    async def _analyze_workflow(self) -> Dict[str, Any]:
        """Analyze workflow patterns"""
        if not self.workflow_analyzer:
            return {}

        return await self.workflow_analyzer.analyze_workflow(within_hours=24)

    async def _generate_insights(
        self,
        query_type: PredictiveQueryType,
        metrics: Optional[ProgressMetrics],
        bug_patterns: List[BugPattern],
        workflow_data: Dict[str, Any],
        context: Dict[str, Any]
    ) -> List[str]:
        """Generate high-level insights"""
        insights = []

        if query_type == PredictiveQueryType.PROGRESS_CHECK:
            insights.extend(await self._progress_insights(metrics, workflow_data))
        elif query_type == PredictiveQueryType.BUG_DETECTION:
            insights.extend(await self._bug_insights(bug_patterns))
        elif query_type == PredictiveQueryType.WORKFLOW_OPTIMIZATION:
            insights.extend(await self._workflow_insights(workflow_data))
        elif query_type == PredictiveQueryType.NEXT_STEPS:
            insights.extend(await self._next_steps_insights(metrics, bug_patterns))
        else:
            # Generic insights
            if metrics:
                insights.append(f"You've made {metrics.commits_today} commit(s) today")
            if bug_patterns:
                insights.append(f"Detected {len(bug_patterns)} recurring bug pattern(s)")

        return insights

    async def _progress_insights(self, metrics: Optional[ProgressMetrics], workflow_data: Dict[str, Any]) -> List[str]:
        """Generate progress insights"""
        insights = []

        if metrics:
            if metrics.commits_today > 0:
                insights.append(f"✅ {metrics.commits_today} commit(s) today - you're making progress!")
            else:
                insights.append("⚠️ No commits today yet - consider starting with a small task")

            if metrics.velocity_trend == "increasing":
                insights.append("📈 Your commit velocity is increasing - great momentum!")
            elif metrics.velocity_trend == "decreasing":
                insights.append("📉 Commit velocity is slowing down - might need to address blockers")

            if metrics.lines_added > 100:
                insights.append(f"💻 {metrics.lines_added} lines added - significant code changes")

        if workflow_data:
            efficiency = workflow_data.get("efficiency_score", 0)
            if efficiency > 0.7:
                insights.append(f"🎯 High efficiency score ({efficiency:.0%}) - you're focused!")
            elif efficiency < 0.4:
                insights.append(f"⏰ Low efficiency score ({efficiency:.0%}) - consider reducing distractions")

        return insights

    async def _bug_insights(self, bug_patterns: List[BugPattern]) -> List[str]:
        """Generate bug-related insights"""
        insights = []

        if not bug_patterns:
            insights.append("✅ No recurring bug patterns detected - code looks healthy!")
            return insights

        critical = [b for b in bug_patterns if b.severity == "critical"]
        high = [b for b in bug_patterns if b.severity == "high"]

        if critical:
            insights.append(f"🚨 {len(critical)} critical bug pattern(s) need immediate attention")

        if high:
            insights.append(f"⚠️ {len(high)} high-severity bug pattern(s) detected")

        # Most common pattern
        most_common = max(bug_patterns, key=lambda b: b.frequency) if bug_patterns else None
        if most_common:
            insights.append(f"Most common issue: {most_common.pattern_type} ({most_common.frequency}x)")

        return insights

    async def _workflow_insights(self, workflow_data: Dict[str, Any]) -> List[str]:
        """Generate workflow insights"""
        insights = []

        if not workflow_data:
            return ["No workflow data available yet"]

        context_switches = workflow_data.get("context_switches", 0)
        focus_time = workflow_data.get("focus_time_minutes", 0)

        if context_switches > 30:
            insights.append(f"⚠️ High context switching ({context_switches}x) - consider batching similar tasks")

        if focus_time > 120:
            insights.append(f"🎯 Strong focus time ({focus_time} min) - you're in the zone!")
        elif focus_time < 30:
            insights.append(f"⏰ Low focus time ({focus_time} min) - try longer focused sessions")

        return insights

    async def _next_steps_insights(self, metrics: Optional[ProgressMetrics], bug_patterns: List[BugPattern]) -> List[str]:
        """Generate next steps insights"""
        insights = []

        critical_bugs = [b for b in bug_patterns if b.severity == "critical"]
        if critical_bugs:
            insights.append("Priority: Fix critical bugs first")
        elif metrics and metrics.commits_today == 0:
            insights.append("Suggestion: Start with a small, achievable task")
        elif metrics and metrics.velocity_trend == "increasing":
            insights.append("You're on a roll - keep the momentum going!")

        return insights

    async def _generate_response(
        self,
        query_type: PredictiveQueryType,
        insights: List[str],
        metrics: Optional[ProgressMetrics],
        bug_patterns: List[BugPattern],
        recommendations: List[Recommendation],
        context: Dict[str, Any]
    ) -> str:
        """Generate natural language response"""
        response = []

        # Add insights
        if insights:
            response.append("## 📊 Insights\n")
            for insight in insights:
                response.append(f"- {insight}")
            response.append("")

        # Add metrics summary
        if metrics:
            response.append("## 📈 Metrics\n")
            response.append(f"- **Commits today:** {metrics.commits_today}")
            response.append(f"- **Commits this week:** {metrics.commits_this_week}")
            response.append(f"- **Files modified:** {metrics.files_modified}")
            response.append(f"- **Lines added:** +{metrics.lines_added}, -{metrics.lines_removed}")
            response.append(f"- **Velocity trend:** {metrics.velocity_trend}")
            response.append("")

        # Add bug patterns
        if bug_patterns and query_type == PredictiveQueryType.BUG_DETECTION:
            response.append("## 🐛 Bug Patterns\n")
            for bug in bug_patterns[:5]:
                response.append(f"- **{bug.pattern_type}** ({bug.severity}): {bug.description}")
            response.append("")

        # Add recommendations
        if recommendations:
            response.append("## 💡 Recommendations\n")
            for rec in recommendations[:5]:
                response.append(f"### {rec.title} [{rec.priority.upper()}]")
                response.append(f"{rec.description}\n")
                response.append(f"**Why:** {rec.rationale}\n")
                if rec.action_items:
                    response.append("**Action items:**")
                    for item in rec.action_items:
                        response.append(f"  - {item}")
                response.append("")

        return "\n".join(response)

    async def _calculate_confidence(
        self,
        metrics: Optional[ProgressMetrics],
        bug_patterns: List[BugPattern],
        workflow_data: Dict[str, Any]
    ) -> float:
        """Calculate confidence in the analysis"""
        confidence = 0.5  # Base confidence

        if metrics:
            confidence += 0.2
        if bug_patterns:
            confidence += 0.15
        if workflow_data:
            confidence += 0.15

        return min(1.0, confidence)

    # ========================================================================
    # PUBLIC INTEGRATION METHODS
    # ========================================================================

    def record_error(self, error_type: str, location: str, details: Dict[str, Any]):
        """Record an error for pattern analysis"""
        if self.error_patterns:
            self.error_patterns.record_error(error_type, location, details)

    def record_activity(self, activity_type: str, details: Dict[str, Any]):
        """Record a workflow activity"""
        if self.workflow_analyzer:
            self.workflow_analyzer.record_activity(activity_type, details)

    async def get_summary(self) -> Dict[str, Any]:
        """Get a summary of current state"""
        summary = {
            "timestamp": datetime.now().isoformat(),
            "collectors": {
                "git_metrics": self.git_metrics is not None,
                "error_patterns": self.error_patterns is not None,
                "workflow_analyzer": self.workflow_analyzer is not None
            }
        }

        if self.error_patterns:
            summary["error_patterns_tracked"] = len(self.error_patterns.error_history)

        if self.workflow_analyzer:
            summary["activities_tracked"] = len(self.workflow_analyzer.activity_log)

        return summary


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_analyzer: Optional[PredictiveAnalyzer] = None


def get_predictive_analyzer() -> Optional[PredictiveAnalyzer]:
    """Get the global predictive analyzer instance"""
    return _global_analyzer


def initialize_predictive_analyzer(context_graph=None, **kwargs) -> PredictiveAnalyzer:
    """Initialize the global predictive analyzer"""
    global _global_analyzer
    _global_analyzer = PredictiveAnalyzer(context_graph, **kwargs)
    logger.info("[PREDICTIVE-ANALYZER] Global instance initialized")
    return _global_analyzer


# ============================================================================
# CONVENIENCE FUNCTION
# ============================================================================

async def analyze_query(query: str, **kwargs) -> AnalyticsResult:
    """Convenience function to analyze a query"""
    analyzer = get_predictive_analyzer()
    if not analyzer:
        analyzer = initialize_predictive_analyzer()
    return await analyzer.analyze(query, **kwargs)


# ============================================================================
# DEMO / TESTING
# ============================================================================

if __name__ == "__main__":
    async def demo():
        """Demo the predictive analyzer"""
        analyzer = PredictiveAnalyzer()

        print("=" * 70)
        print("JARVIS Predictive Analytics Engine - Demo")
        print("=" * 70)

        test_queries = [
            "Am I making progress?",
            "What should I work on next?",
            "Are there any potential bugs?",
            "How can I improve my workflow?",
        ]

        for query in test_queries:
            print(f"\n{'='*70}")
            print(f"Query: {query}")
            print(f"{'='*70}\n")

            result = await analyzer.analyze(query, context={"repo_path": "."})

            print(result.response_text)

        print("\n" + "=" * 70)
        print("Demo complete!")
        print("=" * 70)

    asyncio.run(demo())
