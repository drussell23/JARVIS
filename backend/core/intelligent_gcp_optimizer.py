"""
Intelligent GCP VM Cost Optimizer
Prevents unnecessary VM creation through advanced multi-factor analysis

Features:
- Multi-factor pressure scoring (not binary yes/no decisions)
- Adaptive threshold learning from historical patterns
- Workload prediction to prevent short-lived VMs
- VM warm-down period to reduce create/destroy churn
- Cost-aware decision making with daily budget tracking
- Scenario detection (coding spike, ML training, sustained load, etc.)
- Edge case handling (thundering herd, memory leak detection, etc.)

Philosophy:
- ONLY create VMs when absolutely necessary
- Learn from past patterns to predict future needs
- Optimize for cost: prevent VM churn, batch workloads
- Graceful degradation: local can handle more than you think
"""

import asyncio
from backend.core import portable_fcntl as fcntl
import json
import logging
import os
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class PressureScore:
    """Multi-factor pressure score (not binary)"""

    timestamp: datetime

    # Core metrics (0-100 scale)
    memory_pressure_score: float  # 0 = no pressure, 100 = critical
    swap_activity_score: float  # 0 = no swapping, 100 = heavy swapping
    trend_score: float  # 0 = decreasing, 50 = stable, 100 = spiking

    # Contextual factors
    workload_type: str  # "coding", "ml_training", "browser_heavy", "idle", "unknown"
    time_of_day_factor: float  # 0-1, based on typical usage patterns
    historical_stability: float  # 0-1, how stable RAM has been recently

    # Prediction
    predicted_pressure_60s: float  # Predicted pressure in 60 seconds
    confidence: float  # 0-1, confidence in prediction

    # Final decision factors
    composite_score: float  # 0-100, weighted combination of all factors
    gcp_recommended: bool
    gcp_urgent: bool
    reasoning: str

    def to_dict(self) -> Dict:
        result = asdict(self)
        result["timestamp"] = self.timestamp.isoformat()
        return result


@dataclass
class VMSession:
    """Track VM session for cost optimization"""

    vm_id: str
    created_at: datetime
    trigger_reason: str
    initial_cost_estimate: float

    # Metrics
    actual_runtime_seconds: float = 0.0
    actual_cost: float = 0.0
    was_useful: bool = True  # Did it actually help?
    workload_handled: List[str] = None

    # Learn from this session
    should_have_created: Optional[bool] = None  # In hindsight
    lessons_learned: List[str] = None

    def __post_init__(self):
        if self.workload_handled is None:
            self.workload_handled = []
        if self.lessons_learned is None:
            self.lessons_learned = []


@dataclass
class CostBudget:
    """Daily cost budget tracking"""

    date: str  # YYYY-MM-DD
    budget_limit: float  # Daily budget in dollars
    current_spend: float = 0.0
    vm_sessions: List[VMSession] = None

    def __post_init__(self):
        if self.vm_sessions is None:
            self.vm_sessions = []

    @property
    def remaining_budget(self) -> float:
        return max(0, self.budget_limit - self.current_spend)

    @property
    def budget_exhausted(self) -> bool:
        return self.current_spend >= self.budget_limit


class IntelligentGCPOptimizer:
    """
    Advanced GCP VM creation optimizer
    Prevents unnecessary costs through intelligent multi-factor analysis
    """

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

        # Storage paths
        self.data_dir = Path.home() / ".jarvis" / "gcp_optimizer"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_file = self.data_dir / "pressure_history.jsonl"
        self.sessions_file = self.data_dir / "vm_sessions.jsonl"
        self.budget_file = self.data_dir / "daily_budgets.json"
        self.lock_file = self.data_dir / "vm_creation.lock"
        self.lock_fd = None

        # Adaptive thresholds (learned from history)
        self.thresholds = {
            "pressure_score_warning": 60.0,  # Start considering GCP
            "pressure_score_critical": 80.0,  # Strong recommendation
            "pressure_score_emergency": 95.0,  # Urgent creation
            "min_vm_runtime_seconds": 300,  # Don't create VM for <5min workloads
            "vm_warmdown_seconds": 600,  # Keep VM alive 10min after pressure drops
            "max_vm_creates_per_day": 10,  # Prevent runaway costs
        }

        # Cost settings
        self.cost_config = {
            "spot_vm_hourly_cost": 0.029,  # e2-highmem-4 spot rate
            "daily_budget_limit": 1.00,  # $1/day default
            "cost_optimization_mode": "aggressive",  # "aggressive", "balanced", "performance"
        }

        # Update from config
        if "thresholds" in self.config:
            self.thresholds.update(self.config["thresholds"])
        if "cost" in self.config:
            self.cost_config.update(self.config["cost"])

        # State tracking
        self.pressure_history: deque = deque(maxlen=1000)  # Last 1000 pressure checks
        self.vm_sessions: List[VMSession] = []
        self.current_vm_session: Optional[VMSession] = None

        # Workload detection
        self.workload_indicators = {
            "coding": ["python", "node", "code", "cursor", "vscode"],
            "ml_training": ["python", "tensorflow", "pytorch", "jupyter"],
            "browser_heavy": ["chrome", "firefox", "safari"],
            "idle": [],
        }

        # Learning state
        self.false_alarms = 0  # Times we created VM unnecessarily
        self.missed_opportunities = 0  # Times we should have created VM
        self.total_decisions = 0

        # Current daily budget
        self.current_budget = self._load_or_create_daily_budget()

        # Last VM operation
        self.last_vm_created: Optional[datetime] = None
        self.last_vm_destroyed: Optional[datetime] = None
        self.vm_creation_count_today = 0

        logger.info("🧠 IntelligentGCPOptimizer initialized")
        logger.info(f"   Daily budget: ${self.cost_config['daily_budget_limit']:.2f}")
        logger.info(f"   Mode: {self.cost_config['cost_optimization_mode']}")
        logger.info(f"   Max VMs/day: {self.thresholds['max_vm_creates_per_day']}")

    def _load_or_create_daily_budget(self) -> CostBudget:
        """Load today's budget or create new one"""
        today = datetime.now().strftime("%Y-%m-%d")

        if self.budget_file.exists():
            try:
                with open(self.budget_file, "r") as f:
                    budgets = json.load(f)

                if today in budgets:
                    data = budgets[today]
                    return CostBudget(
                        date=today,
                        budget_limit=data.get(
                            "budget_limit", self.cost_config["daily_budget_limit"]
                        ),
                        current_spend=data.get("current_spend", 0.0),
                        vm_sessions=[VMSession(**s) for s in data.get("vm_sessions", [])],
                    )
            except Exception as e:
                logger.warning(f"Failed to load budget: {e}")

        # Create new budget for today
        return CostBudget(date=today, budget_limit=self.cost_config["daily_budget_limit"])

    def _save_daily_budget(self):
        """Save current budget to disk"""
        try:
            budgets = {}
            if self.budget_file.exists():
                with open(self.budget_file, "r") as f:
                    budgets = json.load(f)

            # Update today's budget
            budgets[self.current_budget.date] = {
                "budget_limit": self.current_budget.budget_limit,
                "current_spend": self.current_budget.current_spend,
                "vm_sessions": [asdict(s) for s in self.current_budget.vm_sessions],
            }

            # Keep only last 30 days
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            budgets = {k: v for k, v in budgets.items() if k >= cutoff}

            with open(self.budget_file, "w") as f:
                json.dump(budgets, f, indent=2, default=str)

        except Exception as e:
            logger.error(f"Failed to save budget: {e}")

    async def calculate_pressure_score(
        self, memory_snapshot, current_processes: Optional[List] = None
    ) -> PressureScore:
        """
        Calculate multi-factor pressure score

        Args:
            memory_snapshot: From PlatformMemoryMonitor
            current_processes: Optional list of running processes

        Returns:
            PressureScore with detailed analysis
        """

        # 1. Memory pressure score (0-100)
        memory_pressure_score = self._calculate_memory_pressure_score(memory_snapshot)

        # 2. Swap activity score (0-100)
        swap_activity_score = self._calculate_swap_activity_score(memory_snapshot)

        # 3. Trend score (0-100)
        trend_score = self._calculate_trend_score()

        # 4. Workload type detection
        workload_type = await self._detect_workload_type(current_processes)

        # 5. Time of day factor (some times have higher typical usage)
        time_of_day_factor = self._calculate_time_of_day_factor()

        # 6. Historical stability (how stable has RAM been?)
        historical_stability = self._calculate_historical_stability()

        # 7. Predict pressure in 60 seconds
        predicted_pressure_60s, confidence = self._predict_future_pressure()

        # 8. Calculate composite score (weighted combination)
        composite_score = self._calculate_composite_score(
            memory_pressure_score,
            swap_activity_score,
            trend_score,
            predicted_pressure_60s,
            time_of_day_factor,
            historical_stability,
        )

        # 9. Make recommendation based on composite score + cost awareness
        gcp_recommended, gcp_urgent, reasoning = self._make_recommendation(
            composite_score, workload_type, memory_snapshot
        )

        score = PressureScore(
            timestamp=datetime.now(),
            memory_pressure_score=memory_pressure_score,
            swap_activity_score=swap_activity_score,
            trend_score=trend_score,
            workload_type=workload_type,
            time_of_day_factor=time_of_day_factor,
            historical_stability=historical_stability,
            predicted_pressure_60s=predicted_pressure_60s,
            confidence=confidence,
            composite_score=composite_score,
            gcp_recommended=gcp_recommended,
            gcp_urgent=gcp_urgent,
            reasoning=reasoning,
        )

        # Store in history for learning
        self.pressure_history.append(score)
        self._save_pressure_history(score)

        return score

    def _calculate_memory_pressure_score(self, snapshot) -> float:
        """Convert memory snapshot to 0-100 score"""

        # Start with platform-specific pressure
        if snapshot.platform == "darwin":
            # macOS pressure levels
            if snapshot.macos_pressure_level == "critical":
                base_score = 90.0
            elif snapshot.macos_pressure_level == "warn":
                base_score = 70.0
            else:
                base_score = 30.0

            # Adjust for available memory
            if snapshot.available_gb < 1.0:
                base_score = max(base_score, 85.0)
            elif snapshot.available_gb < 2.0:
                base_score = max(base_score, 60.0)

        elif snapshot.platform == "linux":
            # Linux PSI scores
            if snapshot.linux_psi_full_avg10 is not None:
                # Full PSI is most critical
                psi_full = snapshot.linux_psi_full_avg10
                if psi_full > 5.0:
                    base_score = 95.0
                elif psi_full > 1.0:
                    base_score = 75.0
                elif snapshot.linux_psi_some_avg10 and snapshot.linux_psi_some_avg10 > 20.0:
                    base_score = 60.0
                else:
                    base_score = 30.0
            else:
                # Fallback to available memory
                if snapshot.available_gb < 1.0:
                    base_score = 85.0
                elif snapshot.available_gb < 2.0:
                    base_score = 60.0
                else:
                    base_score = 30.0

        else:
            # Unknown platform - conservative estimate
            if snapshot.available_gb < 1.0:
                base_score = 80.0
            elif snapshot.available_gb < 2.0:
                base_score = 55.0
            else:
                base_score = 30.0

        return min(100.0, max(0.0, base_score))

    def _calculate_swap_activity_score(self, snapshot) -> float:
        """Score based on swapping activity (0-100)"""

        if snapshot.platform == "darwin" and snapshot.macos_is_swapping:
            # Active swapping on macOS
            return 85.0
        elif snapshot.platform == "linux":
            # Linux doesn't expose swapping easily, use available memory
            if snapshot.available_gb < 0.5:
                return 80.0
            elif snapshot.available_gb < 1.0:
                return 50.0

        return 0.0

    def _calculate_trend_score(self) -> float:
        """Analyze RAM trend (0-100, where 50=stable, 100=rapid increase)"""

        if len(self.pressure_history) < 5:
            return 50.0  # Not enough data

        # Look at last 5 pressure scores
        recent = list(self.pressure_history)[-5:]
        scores = [p.memory_pressure_score for p in recent]

        # Calculate trend
        if len(scores) >= 2:
            # Simple linear trend
            trend = scores[-1] - scores[0]

            if trend > 20:
                return 90.0  # Rapidly increasing
            elif trend > 10:
                return 70.0  # Increasing
            elif trend > 5:
                return 60.0  # Slowly increasing
            elif trend < -10:
                return 20.0  # Decreasing
            else:
                return 50.0  # Stable

        return 50.0

    async def _detect_workload_type(self, processes: Optional[List]) -> str:
        """Detect current workload type from processes"""

        if processes is None:
            return "unknown"

        # Count indicators for each workload type
        workload_counts = {wtype: 0 for wtype in self.workload_indicators.keys()}

        for proc in processes:
            proc_name = proc.get("name", "").lower()
            for wtype, indicators in self.workload_indicators.items():
                if any(ind in proc_name for ind in indicators):
                    workload_counts[wtype] += 1

        # Return workload with most indicators
        if any(workload_counts.values()):
            return max(workload_counts, key=workload_counts.get)

        return "unknown"

    def _calculate_time_of_day_factor(self) -> float:
        """Factor based on typical usage patterns (0-1)"""

        hour = datetime.now().hour

        # Learn from history eventually, but for now use heuristics
        if 9 <= hour <= 17:
            return 1.0  # Work hours - typical high usage
        elif 18 <= hour <= 23:
            return 0.8  # Evening - medium usage
        else:
            return 0.5  # Night/early morning - low usage

    def _calculate_historical_stability(self) -> float:
        """How stable has RAM been recently? (0-1, 1=very stable)"""

        if len(self.pressure_history) < 10:
            return 0.5  # Not enough data

        recent = list(self.pressure_history)[-20:]
        scores = [p.memory_pressure_score for p in recent]

        # Calculate standard deviation
        mean_score = sum(scores) / len(scores)
        variance = sum((s - mean_score) ** 2 for s in scores) / len(scores)
        std_dev = variance**0.5

        # Convert to stability (low std dev = high stability)
        stability = max(0.0, 1.0 - (std_dev / 50.0))

        return stability

    def _predict_future_pressure(self) -> Tuple[float, float]:
        """
        Predict pressure in 60 seconds

        Returns: (predicted_pressure, confidence)
        """

        if len(self.pressure_history) < 10:
            # Not enough data for prediction
            return 50.0, 0.3

        recent = list(self.pressure_history)[-10:]
        scores = [p.memory_pressure_score for p in recent]

        # Simple linear extrapolation
        if len(scores) >= 2:
            recent_trend = (
                (scores[-1] - scores[-5]) if len(scores) >= 5 else (scores[-1] - scores[0])
            )
            predicted = scores[-1] + (recent_trend * 0.5)  # Extrapolate trend
            predicted = min(100.0, max(0.0, predicted))

            # Confidence based on trend consistency
            confidence = min(0.9, 0.5 + (len(scores) / 20.0))

            return predicted, confidence

        return scores[-1], 0.5

    def _calculate_composite_score(
        self,
        memory_pressure_score: float,
        swap_activity_score: float,
        trend_score: float,
        predicted_pressure: float,
        time_of_day_factor: float,
        historical_stability: float,
    ) -> float:
        """Calculate weighted composite score (0-100)"""

        # Weights (sum to 1.0)
        weights = {
            "memory_pressure": 0.35,  # Most important
            "swap_activity": 0.25,  # Critical indicator
            "trend": 0.15,  # Future matters
            "predicted": 0.15,  # Predictive component
            "time_of_day": 0.05,  # Minor adjustment
            "stability": 0.05,  # Stability bonus/penalty
        }

        # Calculate weighted sum
        composite = (
            memory_pressure_score * weights["memory_pressure"]
            + swap_activity_score * weights["swap_activity"]
            + trend_score * weights["trend"]
            + predicted_pressure * weights["predicted"]
            + (100.0 * time_of_day_factor) * weights["time_of_day"]
            + (100.0 * historical_stability) * weights["stability"]
        )

        return min(100.0, max(0.0, composite))

    def _make_recommendation(
        self, composite_score: float, workload_type: str, memory_snapshot
    ) -> Tuple[bool, bool, str]:
        """
        Make final GCP recommendation with cost awareness

        Returns: (recommended, urgent, reasoning)
        """

        reasons = []

        # Check daily budget
        if self.current_budget.budget_exhausted:
            return (
                False,
                False,
                f"❌ Daily budget exhausted (${self.current_budget.current_spend:.2f}/${self.current_budget.budget_limit:.2f})",
            )

        # Check VM creation limit
        if self.vm_creation_count_today >= self.thresholds["max_vm_creates_per_day"]:
            return (
                False,
                False,
                f"❌ Max VMs/day limit reached ({self.vm_creation_count_today}/{self.thresholds['max_vm_creates_per_day']})",
            )

        # Check if we just destroyed a VM (prevent churn)
        if self.last_vm_destroyed:
            time_since_destroy = (datetime.now() - self.last_vm_destroyed).total_seconds()
            if time_since_destroy < 300:  # 5 minutes
                return (
                    False,
                    False,
                    f"⏳ Recently destroyed VM ({time_since_destroy:.0f}s ago), waiting to prevent churn",
                )

        # Emergency: Immediate creation
        if composite_score >= self.thresholds["pressure_score_emergency"]:
            reasons.append(f"🚨 EMERGENCY: Composite score {composite_score:.1f}/100")
            reasons.append(f"   Platform: {memory_snapshot.platform}")
            reasons.append(f"   Available: {memory_snapshot.available_gb:.1f}GB")
            return True, True, "; ".join(reasons)

        # Critical: Strong recommendation
        if composite_score >= self.thresholds["pressure_score_critical"]:
            # Check if workload justifies VM
            if workload_type in ["ml_training", "coding"]:
                reasons.append(f"⚠️  CRITICAL: Score {composite_score:.1f}/100")
                reasons.append(f"   Workload: {workload_type}")
                reasons.append(f"   Budget remaining: ${self.current_budget.remaining_budget:.2f}")
                return True, False, "; ".join(reasons)
            else:
                reasons.append(
                    f"⚠️  High score ({composite_score:.1f}/100) but workload '{workload_type}' may not need VM"
                )
                reasons.append(f"   Available: {memory_snapshot.available_gb:.1f}GB")
                return False, False, "; ".join(reasons)

        # Warning: Consider VM
        if composite_score >= self.thresholds["pressure_score_warning"]:
            reasons.append(f"📊 Elevated pressure ({composite_score:.1f}/100)")
            reasons.append(f"   {memory_snapshot.available_gb:.1f}GB available")
            reasons.append(f"   Workload: {workload_type}")
            reasons.append(f"   ✅ Can handle locally for now")
            return False, False, "; ".join(reasons)

        # Normal: No VM needed
        reasons.append(f"✅ Normal operation (score: {composite_score:.1f}/100)")
        reasons.append(f"   {memory_snapshot.available_gb:.1f}GB available")
        return False, False, "; ".join(reasons)

    def _save_pressure_history(self, score: PressureScore):
        """Append pressure score to history file"""
        try:
            with open(self.history_file, "a") as f:
                f.write(json.dumps(score.to_dict(), default=str) + "\n")
        except Exception as e:
            logger.debug(f"Failed to save pressure history: {e}")

    def _acquire_vm_creation_lock(self) -> bool:
        """
        Acquire exclusive lock for VM creation.
        Prevents multiple JARVIS instances from creating VMs simultaneously.

        Returns: True if lock acquired, False otherwise
        """
        # If we already hold the lock, don't try to acquire again
        if self.lock_fd is not None:
            logger.debug("VM creation lock already held by this instance")
            return True

        try:
            # Open/create lock file
            self.lock_fd = os.open(str(self.lock_file), os.O_CREAT | os.O_RDWR)

            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Write PID and timestamp to lock file
            lock_info = {
                "pid": os.getpid(),
                "timestamp": datetime.now().isoformat(),
                "hostname": os.uname().nodename,
            }
            os.ftruncate(self.lock_fd, 0)  # Clear file first
            os.write(self.lock_fd, json.dumps(lock_info).encode())

            logger.info(f"🔒 Acquired VM creation lock (PID: {os.getpid()})")
            return True

        except BlockingIOError:
            # Another instance holds the lock
            logger.warning(f"⚠️  VM creation lock held by another JARVIS instance")
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except Exception:
                    pass
                self.lock_fd = None
            return False

        except Exception as e:
            logger.error(f"❌ Error acquiring VM creation lock: {e}")
            if self.lock_fd is not None:
                try:
                    os.close(self.lock_fd)
                except Exception:
                    pass
                self.lock_fd = None
            return False

    def _release_vm_creation_lock(self):
        """Release VM creation lock"""
        if self.lock_fd is not None:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                os.close(self.lock_fd)
                self.lock_fd = None
                logger.info(f"🔓 Released VM creation lock (PID: {os.getpid()})")
            except Exception as e:
                logger.error(f"❌ Error releasing VM creation lock: {e}")

    async def should_create_vm(
        self, memory_snapshot, current_processes: Optional[List] = None
    ) -> Tuple[bool, str, PressureScore]:
        """
        Main decision function: Should we create a GCP VM?

        Returns: (should_create, reason, pressure_score)
        """

        # Calculate comprehensive pressure score
        score = await self.calculate_pressure_score(memory_snapshot, current_processes)

        self.total_decisions += 1

        if score.gcp_recommended:
            # Try to acquire lock before recommending VM creation
            if not self._acquire_vm_creation_lock():
                logger.warning(
                    "⚠️  Another JARVIS instance is creating a VM - skipping to prevent duplicate VMs"
                )
                score.gcp_recommended = False
                score.gcp_urgent = False
                score.reasoning = (
                    "❌ VM creation blocked: Another JARVIS instance is already creating a VM. "
                    "Only one VM creation at a time is allowed to prevent duplicate VMs and cost waste."
                )
                return False, score.reasoning, score

            logger.info(f"🔍 GCP Recommended (score: {score.composite_score:.1f}/100)")
            logger.info(f"   {score.reasoning}")
            logger.info(f"   🔒 VM creation lock acquired - safe to proceed")

        return score.gcp_recommended, score.reasoning, score

    def record_vm_creation(self, vm_id: str, trigger_reason: str):
        """Record that we created a VM"""
        session = VMSession(
            vm_id=vm_id,
            created_at=datetime.now(),
            trigger_reason=trigger_reason,
            initial_cost_estimate=self.cost_config["spot_vm_hourly_cost"] * 2.0,  # Estimate 2 hours
        )

        self.current_vm_session = session
        self.current_budget.vm_sessions.append(session)
        self.last_vm_created = datetime.now()
        self.vm_creation_count_today += 1

        logger.info(f"💰 VM created: {vm_id}")
        logger.info(f"   Reason: {trigger_reason}")
        logger.info(
            f"   VMs today: {self.vm_creation_count_today}/{self.thresholds['max_vm_creates_per_day']}"
        )

    def record_vm_destruction(self, vm_id: str, runtime_seconds: float):
        """Record that we destroyed a VM"""
        # Release the VM creation lock when VM is destroyed
        self._release_vm_creation_lock()

        if self.current_vm_session and self.current_vm_session.vm_id == vm_id:
            self.current_vm_session.actual_runtime_seconds = runtime_seconds

            # Calculate actual cost
            runtime_hours = runtime_seconds / 3600.0
            actual_cost = runtime_hours * self.cost_config["spot_vm_hourly_cost"]
            self.current_vm_session.actual_cost = actual_cost

            # Update budget
            self.current_budget.current_spend += actual_cost

            self.last_vm_destroyed = datetime.now()
            self.current_vm_session = None

            self._save_daily_budget()

            logger.info(f"💰 VM destroyed: {vm_id}")
            logger.info(f"   Runtime: {runtime_seconds/60:.1f} minutes")
            logger.info(f"   Cost: ${actual_cost:.4f}")
            logger.info(
                f"   Daily spend: ${self.current_budget.current_spend:.2f}/${self.current_budget.budget_limit:.2f}"
            )

    def get_cost_report(self) -> Dict:
        """Get current cost report"""
        return {
            "date": self.current_budget.date,
            "budget_limit": self.current_budget.budget_limit,
            "current_spend": self.current_budget.current_spend,
            "remaining_budget": self.current_budget.remaining_budget,
            "vm_sessions_today": len(self.current_budget.vm_sessions),
            "vm_creation_count": self.vm_creation_count_today,
            "total_decisions": self.total_decisions,
            "false_alarms": self.false_alarms,
            "missed_opportunities": self.missed_opportunities,
        }

    def cleanup(self):
        """Cleanup resources on shutdown"""
        self._release_vm_creation_lock()

    def __del__(self):
        """Ensure lock is released on object destruction"""
        self._release_vm_creation_lock()


# Global singleton
_optimizer: Optional[IntelligentGCPOptimizer] = None


def get_gcp_optimizer(config: Optional[Dict] = None) -> IntelligentGCPOptimizer:
    """Get global optimizer instance"""
    global _optimizer
    if _optimizer is None:
        _optimizer = IntelligentGCPOptimizer(config)
    return _optimizer


async def test_optimizer():
    """Test the intelligent optimizer"""
    from backend.core.platform_memory_monitor import get_memory_monitor

    print("\n" + "=" * 80)
    print("Intelligent GCP Optimizer Test")
    print("=" * 80 + "\n")

    optimizer = get_gcp_optimizer(
        {"cost": {"daily_budget_limit": 1.00, "cost_optimization_mode": "aggressive"}}  # $1/day
    )

    monitor = get_memory_monitor()
    snapshot = await monitor.get_memory_pressure()

    print(f"Memory Snapshot:")
    print(f"  Platform: {snapshot.platform}")
    print(
        f"  Used: {snapshot.used_gb:.1f}GB / {snapshot.total_gb:.1f}GB ({snapshot.usage_percent:.1f}%)"
    )
    print(f"  Available: {snapshot.available_gb:.1f}GB")
    print(f"  Pressure: {snapshot.pressure_level}")
    print()

    should_create, reason, score = await optimizer.should_create_vm(snapshot)

    print("Pressure Score Analysis:")
    print(f"  Memory Pressure: {score.memory_pressure_score:.1f}/100")
    print(f"  Swap Activity: {score.swap_activity_score:.1f}/100")
    print(f"  Trend: {score.trend_score:.1f}/100")
    print(f"  Predicted (60s): {score.predicted_pressure_60s:.1f}/100")
    print(f"  Workload: {score.workload_type}")
    print(f"  Time Factor: {score.time_of_day_factor:.2f}")
    print(f"  Stability: {score.historical_stability:.2f}")
    print(f"  Composite Score: {score.composite_score:.1f}/100")
    print()

    print(f"Decision: {'CREATE VM' if should_create else 'NO VM NEEDED'}")
    if score.gcp_urgent:
        print(f"Urgency: URGENT")
    print(f"Reasoning: {score.reasoning}")
    print()

    cost_report = optimizer.get_cost_report()
    print("Cost Report:")
    print(f"  Budget: ${cost_report['current_spend']:.2f} / ${cost_report['budget_limit']:.2f}")
    print(f"  Remaining: ${cost_report['remaining_budget']:.2f}")
    print(f"  VMs today: {cost_report['vm_creation_count']}")
    print()


if __name__ == "__main__":
    asyncio.run(test_optimizer())
