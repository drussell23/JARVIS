#!/usr/bin/env python3
"""
Advanced AI Brain for JARVIS
Provides predictive intelligence, contextual understanding, and creative problem solving
Powered by Anthropic's Claude API
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import json
import os
from collections import defaultdict, deque
import anthropic

# Import JARVIS components
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autonomy.autonomous_decision_engine import AutonomousDecisionEngine, AutonomousAction
from autonomy.vision_navigation_system import VisionNavigationSystem
from autonomy.workspace_automation import WorkspaceAutomation
from vision.enhanced_monitoring import EnhancedWorkspaceMonitor

# Import new enhanced modules
from autonomy.predictive_intelligence import (
    PredictiveIntelligenceEngine, PredictionContext, DynamicPrediction, PredictionType
)
from autonomy.contextual_understanding import (
    ContextualUnderstandingEngine, EmotionalState, WorkContext, 
    CognitiveLoad, ContextualInsight, EmotionalProfile
)
from autonomy.creative_problem_solving import (
    CreativeProblemSolver, Problem, CreativeSolution, ProblemType, SolutionApproach
)

# Import voice and system integration (optional)
try:
    from autonomy.voice_integration import VoiceIntegrationSystem
    VOICE_INTEGRATION_AVAILABLE = True
except ImportError:
    VOICE_INTEGRATION_AVAILABLE = False
    
try:
    from autonomy.macos_integration import get_macos_integration
    from autonomy.hardware_control import HardwareControlSystem
    SYSTEM_INTEGRATION_AVAILABLE = True
except ImportError:
    SYSTEM_INTEGRATION_AVAILABLE = False

logger = logging.getLogger(__name__)


# Remove duplicate enums and dataclasses since they're imported from modules


class AdvancedAIBrain:
    """
    The advanced AI brain for JARVIS that provides:
    - Dynamic predictive intelligence
    - Deep contextual understanding
    - Creative problem solving
    - Fully autonomous learning and adaptation
    """
    
    def __init__(self, anthropic_api_key: Optional[str] = None):
        # Initialize Claude client
        self.claude_api_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.claude_api_key:
            raise ValueError("Anthropic API key required for Advanced AI Brain")
            
        self.claude = anthropic.Anthropic(api_key=self.claude_api_key)
        
        # Initialize enhanced intelligence modules
        self.predictive_engine = PredictiveIntelligenceEngine(self.claude_api_key)
        self.contextual_engine = ContextualUnderstandingEngine(self.claude_api_key)
        self.creative_solver = CreativeProblemSolver(self.claude_api_key)
        
        # Initialize voice and system integration if available
        self.voice_system = None
        self.macos_system = None
        self.hardware_system = None
        
        if VOICE_INTEGRATION_AVAILABLE:
            self.voice_system = VoiceIntegrationSystem(self.claude_api_key)
            
        if SYSTEM_INTEGRATION_AVAILABLE:
            self.macos_system = get_macos_integration(self.claude_api_key)
            self.hardware_system = HardwareControlSystem(self.claude_api_key)
        
        # Core JARVIS components
        self.decision_engine = AutonomousDecisionEngine()
        self.navigation = None  # Will be injected
        self.automation = None  # Will be injected
        self.monitor = EnhancedWorkspaceMonitor()
        
        # Dynamic state tracking
        self.current_state = {
            'emotional': EmotionalState.NEUTRAL,
            'cognitive_load': CognitiveLoad.MODERATE,
            'work_context': WorkContext.DEEP_WORK,
            'predictions': [],
            'insights': [],
            'active_solutions': []
        }
        
        # Real-time learning systems
        self.learning_buffer = deque(maxlen=10000)
        self.pattern_recognition_active = True
        self.continuous_improvement_rate = 0.1
        
        # Performance tracking
        self.performance_metrics = {
            'prediction_accuracy': 0.7,
            'context_understanding': 0.8,
            'solution_effectiveness': 0.75,
            'user_satisfaction': 0.85,
            'autonomy_level': 0.6,
            'learning_velocity': 0.0
        }
        
        # Active monitoring
        self.is_active = False
        self.monitoring_tasks = []
        
    def set_navigation_system(self, navigation: VisionNavigationSystem):
        """Inject navigation system"""
        self.navigation = navigation
        
    def set_automation_system(self, automation: WorkspaceAutomation):
        """Inject automation system"""
        self.automation = automation
        
    async def start_brain_activity(self, enable_voice=True, enable_system_integration=True):
        """Start the AI brain's fully autonomous operation"""
        self.is_active = True
        
        # Start all intelligence loops
        self.monitoring_tasks = [
            asyncio.create_task(self._dynamic_prediction_loop()),
            asyncio.create_task(self._contextual_understanding_loop()),
            asyncio.create_task(self._creative_problem_solving_loop()),
            asyncio.create_task(self._continuous_learning_loop()),
            asyncio.create_task(self._real_time_adaptation_loop())
        ]
        
        # Start voice integration if available and enabled
        if enable_voice and self.voice_system:
            await self.voice_system.start_voice_integration()
            logger.info("🔊 Voice integration activated")
            
        # Start system integration if available and enabled
        if enable_system_integration:
            if self.macos_system:
                await self.macos_system.start_system_monitoring()
                logger.info("💻 macOS integration activated")
        
        logger.info("🧠 Advanced AI Brain fully activated - JARVIS achieving true autonomous intelligence")
        
    async def stop_brain_activity(self):
        """Gracefully stop brain activity"""
        self.is_active = False
        
        # Cancel all monitoring tasks
        for task in self.monitoring_tasks:
            task.cancel()
        
        # Wait for tasks to complete
        await asyncio.gather(*self.monitoring_tasks, return_exceptions=True)
        
        # Stop voice and system integration
        if self.voice_system:
            await self.voice_system.stop_voice_integration()
            
        if self.macos_system:
            await self.macos_system.stop_system_monitoring()
        
        logger.info("🧠 AI Brain deactivated gracefully")
        
    async def _dynamic_prediction_loop(self):
        """Enhanced dynamic prediction with real-time learning"""
        while self.is_active:
            try:
                # Build comprehensive prediction context
                context = await self._build_prediction_context()
                
                # Generate dynamic predictions
                predictions = await self.predictive_engine.generate_predictions(context)
                
                # Store and act on predictions
                self.current_state['predictions'] = predictions
                await self._process_predictions(predictions)
                
                # Learn from prediction patterns
                await self._learn_from_prediction_patterns(predictions)
                
                await asyncio.sleep(15)  # More frequent for better responsiveness
                
            except Exception as e:
                logger.error(f"Error in dynamic prediction: {e}")
                await asyncio.sleep(30)
                
    async def _contextual_understanding_loop(self):
        """Deep contextual understanding with emotional intelligence"""
        while self.is_active:
            try:
                # Gather comprehensive data
                workspace_state = await self._get_workspace_state()
                activity_data = await self._get_activity_data()
                
                # Analyze user state comprehensively
                user_state = await self.contextual_engine.analyze_user_state(
                    workspace_state, activity_data
                )
                
                # Update current state
                self.current_state['emotional'] = user_state['emotional_state']
                self.current_state['cognitive_load'] = user_state['cognitive_load']
                self.current_state['work_context'] = user_state['work_context']
                self.current_state['insights'] = user_state['insights']
                
                # Respond to state changes
                await self._respond_to_state_changes(user_state)
                
                # Update personality adaptation
                await self._update_personality_adaptation(user_state)
                
                await asyncio.sleep(30)  # Regular monitoring
                
            except Exception as e:
                logger.error(f"Error in contextual understanding: {e}")
                await asyncio.sleep(60)
                
    async def _creative_problem_solving_loop(self):
        """Dynamic creative problem solving with continuous innovation"""
        while self.is_active:
            try:
                # Identify problems from context and predictions
                problems = await self._identify_problems_to_solve()
                
                # Solve problems creatively
                for problem in problems:
                    solutions = await self.creative_solver.solve_problem(problem)
                    
                    if solutions:
                        self.current_state['active_solutions'].extend(solutions)
                        
                        # Auto-implement high-confidence solutions
                        await self._auto_implement_solutions(solutions)
                        
                        # Learn from solution patterns
                        await self._learn_solution_effectiveness(solutions)
                
                # Proactively identify optimization opportunities
                await self._identify_optimization_opportunities()
                
                await asyncio.sleep(60)  # Regular creative thinking
                
            except Exception as e:
                logger.error(f"Error in creative problem solving: {e}")
                await asyncio.sleep(120)
                
    async def _continuous_learning_loop(self):
        """Continuous learning from all interactions and patterns"""
        while self.is_active:
            try:
                # Collect learning data from all modules
                learning_data = {
                    'prediction_stats': self.predictive_engine.get_prediction_stats(),
                    'understanding_stats': self.contextual_engine.get_understanding_stats(),
                    'innovation_stats': self.creative_solver.get_innovation_stats()
                }
                
                # Analyze patterns and improve
                improvements = await self._analyze_and_improve(learning_data)
                
                # Apply improvements dynamically
                await self._apply_improvements(improvements)
                
                # Update performance metrics
                self._update_performance_metrics(learning_data)
                
                # Store patterns for future learning
                self.learning_buffer.append({
                    'timestamp': datetime.now(),
                    'data': learning_data,
                    'improvements': improvements
                })
                
                await asyncio.sleep(300)  # Learn every 5 minutes
                
            except Exception as e:
                logger.error(f"Error in continuous learning: {e}")
                await asyncio.sleep(300)
    
    async def _real_time_adaptation_loop(self):
        """Real-time adaptation to user behavior and preferences"""
        while self.is_active:
            try:
                # Monitor real-time signals
                signals = await self._collect_real_time_signals()
                
                # Adapt behavior immediately
                if await self._should_adapt(signals):
                    adaptations = await self._generate_adaptations(signals)
                    await self._apply_adaptations(adaptations)
                
                # Fine-tune personality and responses
                await self._fine_tune_personality()
                
                await asyncio.sleep(10)  # Very responsive adaptation
                
            except Exception as e:
                logger.error(f"Error in real-time adaptation: {e}")
                await asyncio.sleep(30)
    
    # Helper methods for dynamic prediction
    async def _build_prediction_context(self) -> PredictionContext:
        """Build comprehensive context for predictions"""
        workspace_state = await self._get_workspace_state()
        activity_data = await self._get_activity_data()
        
        # Build rich context
        context = PredictionContext(
            timestamp=datetime.now(),
            workspace_state=workspace_state,
            user_activity=activity_data,
            environmental_factors={
                'time_of_day': datetime.now().hour,
                'day_of_week': datetime.now().weekday(),
                'calendar_density': await self._get_calendar_density(),
                'deadline_pressure': await self._calculate_deadline_pressure()
            },
            historical_patterns=await self._get_historical_patterns(),
            real_time_signals=await self._collect_real_time_signals()
        )
        
        return context
    
    async def _get_workspace_state(self) -> Dict[str, Any]:
        """Get current workspace state"""
        if self.monitor:
            return await self.monitor.get_complete_workspace_state()
        return {'windows': [], 'notifications': {}}
    
    async def _get_activity_data(self) -> Dict[str, Any]:
        """Get user activity data"""
        # This would integrate with activity tracking
        return {
            'window_switches': 0,
            'typing_speed': 60,
            'typing_variance': 10,
            'click_rate': 30,
            'idle_time': 0,
            'error_corrections': 0,
            'total_actions': 100
        }
    
    async def _get_calendar_density(self) -> float:
        """Calculate calendar density for context"""
        # Would integrate with calendar API
        return 0.5
    
    async def _calculate_deadline_pressure(self) -> float:
        """Calculate deadline pressure"""
        # Would analyze project deadlines
        return 0.3
    
    async def _get_historical_patterns(self) -> Dict[str, Any]:
        """Get historical behavior patterns"""
        patterns = {}
        
        # Extract patterns from learning buffer
        if self.learning_buffer:
            recent_data = list(self.learning_buffer)[-100:]
            patterns['common_workflows'] = self._extract_workflow_patterns(recent_data)
            patterns['productivity_peaks'] = self._extract_productivity_patterns(recent_data)
        
        return patterns
    
    async def _collect_real_time_signals(self) -> Dict[str, Any]:
        """Collect real-time behavioral signals"""
        return {
            'current_focus_level': await self._estimate_focus_level(),
            'stress_indicators': await self._detect_stress_indicators(),
            'engagement_level': await self._calculate_engagement()
        }
    
    async def _process_predictions(self, predictions: List[DynamicPrediction]):
        """Process and act on predictions"""
        for prediction in predictions:
            if prediction.should_act():
                await self._execute_prediction_action(prediction)
                
                # Track prediction
                self.performance_metrics['prediction_accuracy'] *= 0.99
                self.performance_metrics['prediction_accuracy'] += 0.01
    
    async def _execute_prediction_action(self, prediction: DynamicPrediction):
        """Execute actions based on predictions"""
        if prediction.type == PredictionType.BREAK_SUGGESTION:
            # Suggest break to user
            logger.info(f"Suggesting break: {prediction.action_items}")
            
            # Voice announcement if available
            if self.voice_system:
                await self.voice_system.announce_text(
                    f"Sir, {prediction.reasoning}. {prediction.action_items[0].get('activities', ['Take a break'])[0]}",
                    priority="medium"
                )
                
        elif prediction.type == PredictionType.RESOURCE_NEED:
            # Prepare resources
            if self.automation:
                for action in prediction.action_items:
                    if action.get('action') == 'prepare_resources':
                        # Prepare workspace with needed resources
                        pass
                        
            # Voice announcement
            if self.voice_system and prediction.confidence > 0.8:
                await self.voice_system.announce_text(
                    f"I'm preparing {', '.join(prediction.action_items[0].get('resources', [])[:2])} for your workflow.",
                    priority="low"
                )
                
        elif prediction.type == PredictionType.MEETING_PREPARATION:
            # Prepare for meeting
            if self.voice_system:
                await self.voice_system.announce_text(
                    "Sir, you have a meeting coming up. Shall I prepare your workspace?",
                    priority="high"
                )
                
            # System optimization for meeting
            if self.macos_system:
                await self.macos_system.optimize_for_context("meeting")
                
        # Add more prediction action handlers
    
    async def _learn_from_prediction_patterns(self, predictions: List[DynamicPrediction]):
        """Learn from prediction patterns"""
        # Analyze prediction types and accuracy
        prediction_types = [p.type.value for p in predictions]
        
        # Update learning buffer
        self.learning_buffer.append({
            'timestamp': datetime.now(),
            'predictions': prediction_types,
            'context': self.current_state
        })
    
    # Helper methods for contextual understanding
    async def _respond_to_state_changes(self, user_state: Dict[str, Any]):
        """Respond to changes in user state"""
        # Check for significant changes
        insights = user_state.get('insights', [])
        
        for insight in insights:
            if insight.confidence > 0.7:
                await self._act_on_insight(insight)
    
    async def _act_on_insight(self, insight: ContextualInsight):
        """Act on contextual insights"""
        for action in insight.recommended_actions:
            if action.get('urgency') == 'high':
                logger.info(f"Taking action on insight: {action['action']}")
                
                # Voice announcement for important insights
                if self.voice_system and insight.confidence > 0.7:
                    await self.voice_system.announce_text(
                        insight.description,
                        priority="high" if action.get('urgency') == 'high' else "medium"
                    )
                
                # Execute system actions
                if action['action'] == 'suggest_break' and self.voice_system:
                    await self.voice_system.announce_text(
                        "Sir, I recommend taking a short break to maintain optimal performance.",
                        priority="medium"
                    )
                elif action['action'] == 'reduce_notifications' and self.macos_system:
                    # Enable Do Not Disturb
                    await self.execute_system_command("Enable Do Not Disturb")
                elif action['action'] == 'simplify_workspace' and self.automation:
                    await self.automation.execute_workflow("simplify_workspace")
    
    async def _update_personality_adaptation(self, user_state: Dict[str, Any]):
        """Update personality based on user state"""
        personality_traits = user_state.get('personality_adaptation', {})
        
        # Apply personality traits to communication style
        if personality_traits.get('warmth', 0) > 0.7:
            self.current_state['communication_style'] = 'warm_supportive'
        elif personality_traits.get('formality', 0) > 0.7:
            self.current_state['communication_style'] = 'professional'
        else:
            self.current_state['communication_style'] = 'balanced'
    
    # Helper methods for creative problem solving
    async def _identify_problems_to_solve(self) -> List[Problem]:
        """Identify problems that need creative solutions"""
        problems = []
        
        # Analyze current state for problems
        if self.current_state['cognitive_load'] == CognitiveLoad.OVERLOAD:
            problems.append(Problem(
                problem_id=f"cognitive_overload_{datetime.now().timestamp()}",
                description="User experiencing cognitive overload",
                problem_type=ProblemType.WORKFLOW_OPTIMIZATION,
                constraints=["Maintain productivity", "Reduce stress"],
                objectives=["Simplify workflow", "Reduce cognitive load"],
                context=self.current_state,
                priority=0.9
            ))
        
        # Check for workflow inefficiencies
        predictions = self.current_state.get('predictions', [])
        for pred in predictions:
            if pred.type == PredictionType.AUTOMATION_OPPORTUNITY:
                problems.append(Problem(
                    problem_id=f"automation_opp_{datetime.now().timestamp()}",
                    description="Repetitive task detected that could be automated",
                    problem_type=ProblemType.AUTOMATION_DESIGN,
                    constraints=["User-friendly", "Reliable"],
                    objectives=["Save time", "Reduce errors"],
                    context={'prediction': pred},
                    priority=0.7
                ))
        
        return problems
    
    async def _auto_implement_solutions(self, solutions: List[CreativeSolution]):
        """Automatically implement high-confidence solutions"""
        for solution in solutions:
            if solution.get_overall_score() > 0.8 and solution.feasibility_score > 0.8:
                logger.info(f"Auto-implementing solution: {solution.description}")
                
                # Execute implementation steps
                for step in solution.implementation_steps[:3]:  # Start with first steps
                    await self._execute_solution_step(step, solution)
    
    async def _execute_solution_step(self, step: Dict[str, Any], solution: CreativeSolution):
        """Execute a solution implementation step"""
        # This would integrate with automation systems
        logger.info(f"Executing step: {step.get('description', 'Unknown step')}")
    
    async def _learn_solution_effectiveness(self, solutions: List[CreativeSolution]):
        """Learn from solution effectiveness"""
        for solution in solutions:
            # Track solution metrics
            self.performance_metrics['solution_effectiveness'] *= 0.95
            self.performance_metrics['solution_effectiveness'] += 0.05 * solution.get_overall_score()
    
    async def _identify_optimization_opportunities(self):
        """Proactively identify optimization opportunities"""
        # Analyze patterns for optimization
        if len(self.learning_buffer) > 100:
            patterns = self._analyze_optimization_patterns()
            
            for pattern in patterns:
                if pattern.get('optimization_potential', 0) > 0.7:
                    # Create optimization problem
                    problem = Problem(
                        problem_id=f"optimization_{datetime.now().timestamp()}",
                        description=f"Optimize {pattern['area']}",
                        problem_type=ProblemType.PRODUCTIVITY_ENHANCEMENT,
                        constraints=pattern.get('constraints', []),
                        objectives=pattern.get('objectives', []),
                        context=pattern,
                        priority=pattern['optimization_potential']
                    )
                    
                    # Solve proactively
                    solutions = await self.creative_solver.solve_problem(problem)
                    if solutions:
                        self.current_state['active_solutions'].extend(solutions)
    
    # Helper methods for continuous learning
    async def _analyze_and_improve(self, learning_data: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze patterns and generate improvements"""
        improvements = {
            'prediction_improvements': [],
            'understanding_improvements': [],
            'solution_improvements': []
        }
        
        # Analyze prediction accuracy
        pred_stats = learning_data.get('prediction_stats', {})
        if pred_stats.get('total_predictions', 0) > 50:
            # Identify weak prediction types
            for pred_type, perf in pred_stats.get('model_performance', {}).items():
                if perf['performance'] < 0.6:
                    improvements['prediction_improvements'].append({
                        'type': pred_type,
                        'action': 'increase_training_weight',
                        'factor': 1.2
                    })
        
        return improvements
    
    async def _apply_improvements(self, improvements: Dict[str, Any]):
        """Apply improvements to the system"""
        # Apply prediction improvements
        for imp in improvements.get('prediction_improvements', []):
            logger.info(f"Applying improvement: {imp}")
            # This would update model parameters
        
        # Update learning velocity
        self.performance_metrics['learning_velocity'] = len(improvements.get('prediction_improvements', [])) * 0.1
    
    def _update_performance_metrics(self, learning_data: Dict[str, Any]):
        """Update performance metrics"""
        # Update autonomy level based on successful actions
        successful_actions = sum(1 for d in self.learning_buffer if d.get('success', False))
        total_actions = len(self.learning_buffer)
        
        if total_actions > 0:
            self.performance_metrics['autonomy_level'] = successful_actions / total_actions
    
    # Real-time adaptation methods
    async def _should_adapt(self, signals: Dict[str, Any]) -> bool:
        """Determine if adaptation is needed"""
        # Check for significant changes
        focus_level = signals.get('current_focus_level', 0.5)
        stress_indicators = signals.get('stress_indicators', 0)
        
        return focus_level < 0.3 or stress_indicators > 0.7
    
    async def _generate_adaptations(self, signals: Dict[str, Any]) -> Dict[str, Any]:
        """Generate adaptations based on signals"""
        adaptations = {}
        
        if signals.get('current_focus_level', 0.5) < 0.3:
            adaptations['reduce_interruptions'] = True
            adaptations['simplify_interface'] = True
        
        if signals.get('stress_indicators', 0) > 0.7:
            adaptations['calming_mode'] = True
            adaptations['reduce_complexity'] = True
        
        return adaptations
    
    async def _apply_adaptations(self, adaptations: Dict[str, Any]):
        """Apply real-time adaptations"""
        for adaptation, value in adaptations.items():
            if value:
                logger.info(f"Applying adaptation: {adaptation}")
                # This would modify system behavior
    
    async def _fine_tune_personality(self):
        """Fine-tune personality based on interactions"""
        # Adjust personality traits based on user response
        if self.performance_metrics['user_satisfaction'] < 0.7:
            # Increase empathy and warmth
            pass
        elif self.performance_metrics['user_satisfaction'] > 0.9:
            # Current personality is working well
            pass
    
    # Utility methods
    async def _estimate_focus_level(self) -> float:
        """Estimate current focus level"""
        # Based on activity patterns
        return 0.7  # Placeholder
    
    async def _detect_stress_indicators(self) -> float:
        """Detect stress from behavior"""
        return 0.3  # Placeholder
    
    async def _calculate_engagement(self) -> float:
        """Calculate user engagement level"""
        return 0.8  # Placeholder
    
    def _extract_workflow_patterns(self, data: List[Dict[str, Any]]) -> List[str]:
        """Extract workflow patterns from data"""
        # Analyze data for patterns
        return ['development_flow', 'communication_flow']
    
    def _extract_productivity_patterns(self, data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Extract productivity patterns"""
        return [{'hour': 10, 'productivity': 0.9}, {'hour': 14, 'productivity': 0.7}]
    
    def _analyze_optimization_patterns(self) -> List[Dict[str, Any]]:
        """Analyze patterns for optimization opportunities"""
        return [{
            'area': 'window_management',
            'optimization_potential': 0.8,
            'constraints': ['Maintain visibility'],
            'objectives': ['Reduce switching time']
        }]
                
    # Main interface methods
    async def get_personality_response(self, user_input: str, 
                                     context: Optional[Dict[str, Any]] = None,
                                     voice_output: bool = True) -> str:
        """Generate personality-adapted response using enhanced understanding"""
        # Get current user state
        user_state = {
            'emotional_state': self.current_state['emotional'],
            'work_context': self.current_state['work_context'],
            'personality_adaptation': await self._get_current_personality_traits()
        }
        
        # Generate empathetic response
        response = await self.contextual_engine.generate_empathetic_response(
            user_input, user_state
        )
        
        # Voice output if enabled
        if voice_output and self.voice_system:
            await self.voice_system.announce_text(
                response,
                priority="normal",
                context=user_state
            )
        
        return response
    
    async def process_natural_language_command(self, command: str) -> Dict[str, Any]:
        """Process complex commands with full AI capabilities"""
        try:
            # Build comprehensive context
            context = await self._build_command_context(command)
            
            # Use Claude for deep understanding
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1000,
                messages=[{
                    "role": "user",
                    "content": f"""As JARVIS with full autonomous AI capabilities, process this command:

Command: "{command}"

Current State:
- Emotional: {self.current_state['emotional'].value}
- Work Context: {self.current_state['work_context'].value}  
- Active Predictions: {len(self.current_state['predictions'])}
- Active Solutions: {len(self.current_state['active_solutions'])}

Capabilities:
1. Predictive Intelligence - Anticipate needs
2. Contextual Understanding - Deep emotional/cognitive awareness
3. Creative Problem Solving - Innovative solutions
4. Autonomous Action - Self-directed execution

Provide:
1. Intent analysis
2. Execution plan with specific actions
3. Predicted outcomes
4. Proactive suggestions"""
                }]
            )
            
            # Parse and execute
            plan = self._parse_command_response(response.content[0].text)
            
            # Execute autonomously if confidence is high
            if plan.get('confidence', 0) > 0.8:
                await self._execute_command_plan(plan)
            
            return {
                'understood': True,
                'plan': plan,
                'executed': plan.get('confidence', 0) > 0.8
            }
            
        except Exception as e:
            logger.error(f"Error processing command: {e}")
            return {'understood': False, 'error': str(e)}
    
    async def process_voice_command(self, voice_input: str, confidence: float = 0.8) -> Dict[str, Any]:
        """Process voice command with full integration"""
        if not self.voice_system:
            return await self.process_natural_language_command(voice_input)
        
        # Process through voice system for natural interaction
        result = await self.voice_system.process_voice_command(voice_input, confidence)
        
        # If high confidence, also execute through main command processor
        if confidence > 0.8:
            command_result = await self.process_natural_language_command(voice_input)
            result['executed'] = command_result.get('executed', False)
        
        return result
    
    async def announce_notification(self, notification: Dict[str, Any]):
        """Announce notification via voice if available"""
        if self.voice_system:
            await self.voice_system.announce_notification(notification)
        else:
            logger.info(f"Notification: {notification.get('message', 'New notification')}")
        
    async def learn_from_feedback(self, action: str, result: str, satisfaction: float):
        """Learn from user feedback across all modules"""
        # Create comprehensive feedback
        feedback = {
            'timestamp': datetime.now(),
            'action': action,
            'result': result,
            'satisfaction': satisfaction,
            'state': self.current_state.copy()
        }
        
        # Update performance metrics
        self.performance_metrics['user_satisfaction'] = (
            self.performance_metrics['user_satisfaction'] * 0.9 + satisfaction * 0.1
        )
        
        # Learn in each module
        if 'prediction' in action.lower():
            # Update prediction learning
            for pred in self.current_state['predictions']:
                if pred.prediction_id in action:
                    await self.predictive_engine.learn_from_outcome(
                        pred.prediction_id,
                        {'accurate': satisfaction > 0.7, 'feedback': feedback}
                    )
        
        if 'solution' in action.lower():
            # Update solution learning
            for sol in self.current_state['active_solutions']:
                if sol.solution_id in action:
                    await self.creative_solver.learn_from_implementation(
                        sol.solution_id,
                        {'success_score': satisfaction, 'feedback': feedback}
                    )
        
        # Store in learning buffer
        self.learning_buffer.append(feedback)
        
    def get_brain_status(self) -> Dict[str, Any]:
        """Get comprehensive brain status"""
        return {
            'active': self.is_active,
            'current_state': {
                'emotional': self.current_state['emotional'].value,
                'cognitive_load': self.current_state['cognitive_load'].value,
                'work_context': self.current_state['work_context'].value
            },
            'performance_metrics': self.performance_metrics,
            'intelligence_stats': {
                'predictions': self.predictive_engine.get_prediction_stats(),
                'understanding': self.contextual_engine.get_understanding_stats(),
                'innovation': self.creative_solver.get_innovation_stats()
            },
            'active_elements': {
                'predictions': len(self.current_state['predictions']),
                'insights': len(self.current_state['insights']),
                'solutions': len(self.current_state['active_solutions'])
            },
            'learning_velocity': self.performance_metrics['learning_velocity']
        }
        
    def add_problem_for_solving(self, problem_description: str, 
                               context: Optional[Dict[str, Any]] = None):
        """Add a problem for creative solving"""
        problem = Problem(
            problem_id=f"user_problem_{datetime.now().timestamp()}",
            description=problem_description,
            problem_type=ProblemType.WORKFLOW_OPTIMIZATION,
            constraints=context.get('constraints', []) if context else [],
            objectives=context.get('objectives', ['Improve efficiency']) if context else ['Improve efficiency'],
            context=context or self.current_state,
            priority=0.8
        )
        
        # Queue for solving
        asyncio.create_task(self._solve_problem_async(problem))
    
    async def _solve_problem_async(self, problem: Problem):
        """Solve problem asynchronously"""
        solutions = await self.creative_solver.solve_problem(problem)
        if solutions:
            self.current_state['active_solutions'].extend(solutions)
            logger.info(f"Generated {len(solutions)} solutions for: {problem.description}")
        
    async def _analyze_emotional_indicators(self, indicators: Dict[str, int]) -> EmotionalState:
        """Use AI to analyze emotional indicators"""
        try:
            response = self.claude.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=100,
                messages=[{
                    "role": "user",
                    "content": f"""Analyze these behavior indicators and determine the user's likely emotional state:

Indicators:
- Window switches: {indicators['window_switches']}
- Rapid clicks/actions: {indicators['rapid_clicks']}
- Idle time: {indicators['idle_time']} seconds
- Error windows: {indicators['error_windows']}

Current work context: {self.current_work_context.value}

Return only one of: focused, stressed, relaxed, frustrated, energetic, tired, neutral"""
                }]
            )
            
            state_text = response.content[0].text.strip().lower()
            
            # Map to enum
            for state in EmotionalState:
                if state.value in state_text:
                    return state
                    
        except Exception as e:
            logger.error(f"Error analyzing emotional state: {e}")
            
        return EmotionalState.NEUTRAL
        
    # Removed old methods - now handled by enhanced modules
        
    # Helper methods
    async def _get_current_personality_traits(self) -> Dict[str, float]:
        """Get current personality traits"""
        # Get base traits from contextual engine
        base_traits = self.contextual_engine.personality_model['base_traits'].copy()
        
        # Apply current adaptations
        if self.current_state.get('communication_style') == 'warm_supportive':
            base_traits['warmth'] *= 1.2
            base_traits['empathy'] *= 1.2
        elif self.current_state.get('communication_style') == 'professional':
            base_traits['formality'] *= 1.3
            base_traits['competence'] *= 1.1
        
        # Normalize
        for trait in base_traits:
            base_traits[trait] = min(1.0, base_traits[trait])
        
        return base_traits
    
    async def _build_command_context(self, command: str) -> Dict[str, Any]:
        """Build context for command processing"""
        return {
            'command': command,
            'timestamp': datetime.now(),
            'user_state': self.current_state,
            'recent_predictions': self.current_state['predictions'][-5:],
            'active_solutions': self.current_state['active_solutions'][-3:],
            'performance': self.performance_metrics
        }
    
    def _parse_command_response(self, response_text: str) -> Dict[str, Any]:
        """Parse Claude's command response"""
        # Extract key components
        plan = {
            'intent': 'unknown',
            'actions': [],
            'confidence': 0.5
        }
        
        # Simple parsing - would be more sophisticated
        if 'intent' in response_text.lower():
            # Extract intent
            lines = response_text.split('\n')
            for line in lines:
                if 'intent' in line.lower():
                    plan['intent'] = line.split(':')[-1].strip()
                elif 'confidence' in line.lower():
                    try:
                        import re
                        conf_match = re.search(r'(\d\.\d+|\d+%)', line)
                        if conf_match:
                            conf_str = conf_match.group()
                            if '%' in conf_str:
                                plan['confidence'] = float(conf_str.strip('%')) / 100
                            else:
                                plan['confidence'] = float(conf_str)
                    except:
                        pass
        
        return plan
    
    async def _execute_command_plan(self, plan: Dict[str, Any]):
        """Execute command plan autonomously"""
        logger.info(f"Executing plan: {plan.get('intent', 'unknown')}")
        
        # Execute based on intent
        if 'optimize' in plan.get('intent', '').lower():
            # Create optimization problem
            self.add_problem_for_solving(
                f"Optimize: {plan.get('intent', 'workflow')}",
                {'source': 'command', 'plan': plan}
            )
        
        # Add more execution handlers
            
    def _import_re(self):
        """Import re module"""
        import re
        return re
            
            
            
            
        
        
        
                    
        
            
        
            
        
        
            
        
        


# Singleton instance manager
_brain_instance: Optional[AdvancedAIBrain] = None


def get_ai_brain(api_key: Optional[str] = None) -> AdvancedAIBrain:
    """Get or create the AI brain instance"""
    global _brain_instance
    if _brain_instance is None:
        _brain_instance = AdvancedAIBrain(api_key)
    return _brain_instance


async def test_ai_brain():
    """Test the advanced AI brain"""
    print("🧠 Testing Advanced AI Brain")
    print("=" * 50)
    
    # Create brain
    brain = get_ai_brain()
    
    # Start brain activity
    await brain.start_brain_activity()
    
    # Test natural language understanding
    print("\n🎯 Testing natural language command...")
    result = await brain.process_natural_language_command(
        "I'm feeling stressed with all these deadlines, help me focus"
    )
    print(f"Command understood: {result['understood']}")
    
    # Test personality response
    print("\n💬 Testing personality response...")
    response = await brain.get_personality_response(
        "Good morning JARVIS",
        {'time': 'morning'}
    )
    print(f"JARVIS: {response}")
    
    # Get brain status
    status = brain.get_brain_status()
    print(f"\n📊 Brain Status:")
    print(f"   Active: {status['active']}")
    print(f"   Emotional State: {status['emotional_state']}")
    print(f"   Work Context: {status['work_context']}")
    print(f"   User Satisfaction: {status['user_satisfaction']:.0%}")
    
    # Stop brain
    await brain.stop_brain_activity()
    
    print("\n✅ AI Brain test complete!")


if __name__ == "__main__":
    asyncio.run(test_ai_brain())