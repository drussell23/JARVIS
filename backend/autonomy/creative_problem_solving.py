#!/usr/bin/env python3
"""
Creative Problem Solving Module for JARVIS
Provides innovative solution generation and workflow optimization capabilities
"""

import asyncio
import logging
from typing import Dict, List, Optional, Any, Tuple, Set, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from collections import defaultdict, deque
from enum import Enum
import json
import numpy as np
from itertools import combinations, permutations
import anthropic
import hashlib
import random

logger = logging.getLogger(__name__)


class ProblemType(Enum):
    """Types of problems the system can solve"""
    WORKFLOW_OPTIMIZATION = "workflow_optimization"
    AUTOMATION_DESIGN = "automation_design"
    PRODUCTIVITY_ENHANCEMENT = "productivity_enhancement"
    TECHNICAL_CHALLENGE = "technical_challenge"
    CREATIVE_BLOCK = "creative_block"
    RESOURCE_ALLOCATION = "resource_allocation"
    DECISION_MAKING = "decision_making"
    INNOVATION_OPPORTUNITY = "innovation_opportunity"
    SYSTEM_INTEGRATION = "system_integration"
    USER_EXPERIENCE = "user_experience"


class SolutionApproach(Enum):
    """Creative approaches to problem solving"""
    LATERAL_THINKING = "lateral_thinking"
    SYSTEMATIC_INNOVATION = "systematic_innovation"
    ANALOGICAL_REASONING = "analogical_reasoning"
    REVERSE_ENGINEERING = "reverse_engineering"
    COMBINATORIAL_CREATIVITY = "combinatorial_creativity"
    BIOMIMICRY = "biomimicry"
    FIRST_PRINCIPLES = "first_principles"
    DESIGN_THINKING = "design_thinking"
    SYSTEMS_THINKING = "systems_thinking"
    MORPHOLOGICAL_ANALYSIS = "morphological_analysis"


@dataclass
class Problem:
    """Represents a problem to be solved"""
    problem_id: str
    description: str
    problem_type: ProblemType
    constraints: List[str]
    objectives: List[str]
    context: Dict[str, Any]
    priority: float
    deadline: Optional[datetime] = None
    stakeholders: List[str] = field(default_factory=list)
    
    def to_prompt_context(self) -> str:
        """Convert problem to context for AI prompt"""
        return f"""
Problem: {self.description}
Type: {self.problem_type.value}
Objectives: {', '.join(self.objectives)}
Constraints: {', '.join(self.constraints)}
Context: {json.dumps(self.context, indent=2)}
Priority: {self.priority}
"""


@dataclass
class CreativeSolution:
    """A creative solution to a problem"""
    solution_id: str
    problem_id: str
    approach: SolutionApproach
    description: str
    implementation_steps: List[Dict[str, Any]]
    innovation_score: float
    feasibility_score: float
    impact_score: float
    resources_required: List[str]
    estimated_time: str
    risks: List[Dict[str, Any]]
    alternatives: List[str]
    synergies: List[str]  # How it connects with existing systems
    
    def get_overall_score(self) -> float:
        """Calculate overall solution quality score"""
        return (self.innovation_score * 0.3 + 
                self.feasibility_score * 0.4 + 
                self.impact_score * 0.3)


@dataclass
class IdeaNode:
    """Node in the idea generation graph"""
    node_id: str
    concept: str
    category: str
    connections: List[str]
    strength: float
    metadata: Dict[str, Any] = field(default_factory=dict) 


class CreativeProblemSolver:
    """
    Advanced creative problem solving engine using AI and innovative techniques
    """
    
    def __init__(self, anthropic_api_key: str):
        # Anthropic API Client to work with Claude 
        self.claude = anthropic.Anthropic(api_key=anthropic_api_key)
        
        # Problem solving components
        self.active_problems: Dict[str, Problem] = {}
        self.solution_history: List[CreativeSolution] = []
        self.idea_graph: Dict[str, IdeaNode] = {}
        
        # Learning components
        self.solution_patterns = defaultdict(list)
        self.success_metrics = defaultdict(float)
        self.approach_effectiveness = defaultdict(lambda: 0.5)
        
        # Creative techniques
        self.creative_techniques = self._initialize_creative_techniques()
        self.analogy_database = self._load_analogy_database()
        
        # Innovation tracking
        self.innovation_metrics = {
            'solutions_generated': 0,
            'average_innovation_score': 0.0,
            'successful_implementations': 0,
            'time_saved': 0  # in hours
        }
    
    def _initialize_creative_techniques(self) -> Dict[str, Callable]:
        """Initialize creative problem-solving techniques"""
        return {
            SolutionApproach.LATERAL_THINKING: self._lateral_thinking_approach,
            SolutionApproach.SYSTEMATIC_INNOVATION: self._systematic_innovation_approach,
            SolutionApproach.ANALOGICAL_REASONING: self._analogical_reasoning_approach,
            SolutionApproach.REVERSE_ENGINEERING: self._reverse_engineering_approach,
            SolutionApproach.COMBINATORIAL_CREATIVITY: self._combinatorial_approach,
            SolutionApproach.BIOMIMICRY: self._biomimicry_approach,
            SolutionApproach.FIRST_PRINCIPLES: self._first_principles_approach,
            SolutionApproach.DESIGN_THINKING: self._design_thinking_approach,
            SolutionApproach.SYSTEMS_THINKING: self._systems_thinking_approach,
            SolutionApproach.MORPHOLOGICAL_ANALYSIS: self._morphological_analysis_approach
        }
    
    def _load_analogy_database(self) -> Dict[str, List[Dict[str, Any]]]:
        """Load database of analogies for creative problem solving"""
        return {
            'nature': [
                {'system': 'ant_colony', 'principles': ['distributed_intelligence', 'pheromone_trails', 'task_specialization']},
                {'system': 'neural_network', 'principles': ['parallel_processing', 'learning', 'adaptation']},
                {'system': 'ecosystem', 'principles': ['balance', 'recycling', 'diversity_strength']},
                {'system': 'swarm', 'principles': ['emergent_behavior', 'simple_rules', 'collective_intelligence']}
            ],
            'engineering': [
                {'system': 'modular_design', 'principles': ['reusability', 'scalability', 'maintainability']},
                {'system': 'feedback_loops', 'principles': ['self_regulation', 'continuous_improvement', 'stability']},
                {'system': 'redundancy', 'principles': ['fault_tolerance', 'reliability', 'backup_systems']}
            ],
            'human_systems': [
                {'system': 'agile_methodology', 'principles': ['iteration', 'flexibility', 'collaboration']},
                {'system': 'assembly_line', 'principles': ['efficiency', 'specialization', 'workflow']},
                {'system': 'democracy', 'principles': ['distributed_decision_making', 'checks_balances', 'representation']}
            ]
        }
    
    async def solve_problem(self, problem: Problem) -> List[CreativeSolution]:
        """Generate creative solutions for a given problem"""
        self.active_problems[problem.problem_id] = problem
        solutions = []
        
        # Analyze problem deeply
        problem_analysis = await self._analyze_problem(problem)
        
        # Generate ideas using multiple approaches
        for approach in self._select_approaches(problem, problem_analysis):
            try:
                solution = await self._generate_solution(problem, approach, problem_analysis)
                if solution and solution.feasibility_score > 0.4:
                    solutions.append(solution)
                    self.solution_history.append(solution)
                    self.innovation_metrics['solutions_generated'] += 1
            except Exception as e:
                logger.error(f"Error with approach {approach}: {e}")
        
        # Cross-pollinate solutions
        if len(solutions) > 1:
            hybrid_solutions = await self._create_hybrid_solutions(solutions, problem)
            solutions.extend(hybrid_solutions)
        
        # Rank and refine solutions
        solutions = self._rank_solutions(solutions)
        solutions = await self._refine_top_solutions(solutions[:5], problem)
        
        # Update metrics
        if solutions:
            avg_innovation = sum(s.innovation_score for s in solutions) / len(solutions)
            self.innovation_metrics['average_innovation_score'] = (
                self.innovation_metrics['average_innovation_score'] * 0.9 + avg_innovation * 0.1
            )
        
        return solutions[:3]  # Return top 3 solutions
    
    async def _analyze_problem(self, problem: Problem) -> Dict[str, Any]:
        """Deep analysis of the problem using AI"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1000,
                messages=[{
                    "role": "user",
                    "content": f"""Analyze this problem deeply:

{problem.to_prompt_context()}

Provide analysis including:
1. Root causes (not just symptoms)
2. Hidden assumptions that could be challenged
3. Stakeholder perspectives and needs
4. System interdependencies
5. Potential unintended consequences
6. Opportunity spaces for innovation
7. Similar problems in other domains
8. Key leverage points for maximum impact

Format as JSON with these keys: root_causes, assumptions, stakeholder_needs, 
interdependencies, risks, opportunities, analogous_problems, leverage_points"""
                }]
            )
            
            analysis_text = response.content[0].text
            
            # Parse JSON from response
            import re
            json_match = re.search(r'\{.*\}', analysis_text, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            else:
                return self._generate_default_analysis(problem)
                
        except Exception as e:
            logger.error(f"Error in problem analysis: {e}")
            return self._generate_default_analysis(problem)
    
    def _generate_default_analysis(self, problem: Problem) -> Dict[str, Any]:
        """Generate default analysis when AI analysis fails"""
        return {
            'root_causes': ['Resource constraints', 'Process inefficiency'],
            'assumptions': ['Current approach is optimal', 'Users need all features'],
            'stakeholder_needs': ['Efficiency', 'Reliability', 'Ease of use'],
            'interdependencies': ['System components', 'User workflows'],
            'risks': ['Implementation complexity', 'User adoption'],
            'opportunities': ['Automation', 'Integration', 'Simplification'],
            'analogous_problems': ['Similar optimization challenges'],
            'leverage_points': ['Key bottlenecks', 'High-impact areas']
        }
    
    def _select_approaches(self, problem: Problem, 
                         analysis: Dict[str, Any]) -> List[SolutionApproach]:
        """Select appropriate creative approaches based on problem type"""
        approaches = []
        
        # Map problem types to effective approaches
        if problem.problem_type == ProblemType.WORKFLOW_OPTIMIZATION:
            approaches.extend([
                SolutionApproach.SYSTEMS_THINKING,
                SolutionApproach.SYSTEMATIC_INNOVATION,
                SolutionApproach.BIOMIMICRY
            ])
        elif problem.problem_type == ProblemType.TECHNICAL_CHALLENGE:
            approaches.extend([
                SolutionApproach.FIRST_PRINCIPLES,
                SolutionApproach.REVERSE_ENGINEERING,
                SolutionApproach.ANALOGICAL_REASONING
            ])
        elif problem.problem_type == ProblemType.CREATIVE_BLOCK:
            approaches.extend([
                SolutionApproach.LATERAL_THINKING,
                SolutionApproach.COMBINATORIAL_CREATIVITY,
                SolutionApproach.MORPHOLOGICAL_ANALYSIS
            ])
        else:
            # Default approaches
            approaches.extend([
                SolutionApproach.DESIGN_THINKING,
                SolutionApproach.SYSTEMATIC_INNOVATION,
                SolutionApproach.ANALOGICAL_REASONING
            ])
        
        # Prioritize based on past effectiveness
        approaches.sort(key=lambda a: self.approach_effectiveness[a], reverse=True)
        
        return approaches[:4]  # Use top 4 approaches
    
    async def _generate_solution(self, problem: Problem, 
                               approach: SolutionApproach,
                               analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Generate solution using specific approach"""
        technique = self.creative_techniques.get(approach)
        if not technique:
            return None
        
        return await technique(problem, analysis)
    
    async def _lateral_thinking_approach(self, problem: Problem, 
                                       analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Generate solution using lateral thinking"""
        try:
            # Challenge assumptions
            assumptions = analysis.get('assumptions', [])
            
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Use lateral thinking to solve this problem:

{problem.to_prompt_context()}

Assumptions to challenge: {', '.join(assumptions)}

Apply lateral thinking techniques:
1. Random entry: Start from an unrelated concept
2. Provocation: Make deliberately unreasonable statements
3. Movement: Extract useful ideas from provocations
4. Concept extraction: Find underlying principles
5. Challenge boundaries: Question all constraints

Generate an innovative solution that:
- Breaks conventional thinking
- Finds unexpected connections
- Simplifies radically
- Changes the problem definition if needed

Provide: solution description, implementation steps, innovation reasoning"""
                }]
            )
            
            solution_text = response.content[0].text
            
            return self._parse_solution(
                solution_text, problem, SolutionApproach.LATERAL_THINKING
            )
            
        except Exception as e:
            logger.error(f"Error in lateral thinking: {e}")
            return None
    
    async def _systematic_innovation_approach(self, problem: Problem,
                                            analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Use TRIZ-like systematic innovation"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply systematic innovation (TRIZ principles) to solve:

{problem.to_prompt_context()}

Leverage points: {', '.join(analysis.get('leverage_points', []))}

Apply these innovation principles:
1. Segmentation - Divide into independent parts
2. Asymmetry - Change from symmetrical to asymmetrical
3. Dynamics - Make objects adaptive
4. Periodic action - Use periodic or pulsating actions
5. Continuity - Make all parts work at full load
6. Rushing through - Conduct process at high speed
7. Convert harm to benefit - Use harmful factors to achieve positive effect

Generate solution focusing on:
- Resolving contradictions
- Increasing ideality (benefits/costs+harm)
- Using available resources creatively
- Systematic improvement

Provide structured solution with clear implementation path."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.SYSTEMATIC_INNOVATION
            )
            
        except Exception as e:
            logger.error(f"Error in systematic innovation: {e}")
            return None
    
    async def _analogical_reasoning_approach(self, problem: Problem,
                                           analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Solve by finding analogies in other domains"""
        # Find relevant analogies
        analogous_problems = analysis.get('analogous_problems', [])
        relevant_analogies = self._find_analogies(problem, analogous_problems)
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Solve this problem using analogical reasoning:

{problem.to_prompt_context()}

Consider these analogies:
{json.dumps(relevant_analogies, indent=2)}

Apply analogical problem solving:
1. Map source domain (analogy) to target domain (problem)
2. Identify structural similarities
3. Transfer solution principles
4. Adapt to specific constraints
5. Combine multiple analogies if beneficial

Generate solution that:
- Clearly shows the analogy mapping
- Adapts proven principles to new context
- Maintains feasibility while being innovative

Provide complete solution with implementation details."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.ANALOGICAL_REASONING
            )
            
        except Exception as e:
            logger.error(f"Error in analogical reasoning: {e}")
            return None
    
    async def _reverse_engineering_approach(self, problem: Problem,
                                          analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Work backwards from ideal solution"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Solve using reverse engineering approach:

{problem.to_prompt_context()}

Work backwards from ideal state:
1. Define perfect solution (ignore constraints initially)
2. Identify gaps between ideal and current state
3. Find creative ways to bridge gaps
4. Gradually add constraints back
5. Optimize for feasibility while maintaining innovation

Focus on:
- What would the perfect solution look like?
- What prevents us from achieving it?
- How can we approximate the ideal?
- What constraints can we actually remove or work around?

Generate practical yet innovative solution."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.REVERSE_ENGINEERING
            )
            
        except Exception as e:
            logger.error(f"Error in reverse engineering: {e}")
            return None
    
    async def _combinatorial_approach(self, problem: Problem,
                                    analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Combine existing elements in new ways"""
        try:
            # Extract combinable elements
            elements = self._extract_combinable_elements(problem, analysis)
            
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Use combinatorial creativity to solve:

{problem.to_prompt_context()}

Available elements to combine:
{json.dumps(elements, indent=2)}

Apply combinatorial techniques:
1. Forced relationships - Connect unrelated elements
2. Attribute listing - Mix attributes from different solutions
3. Morphological box - Systematic combination of parameters
4. SCAMPER - Substitute, Combine, Adapt, Modify, Put to other use, Eliminate, Reverse

Generate innovative combinations that:
- Create emergent properties
- Achieve multiple objectives
- Simplify through integration

Provide detailed solution with synergistic benefits."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.COMBINATORIAL_CREATIVITY
            )
            
        except Exception as e:
            logger.error(f"Error in combinatorial approach: {e}")
            return None
    
    async def _biomimicry_approach(self, problem: Problem,
                                 analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Learn from nature's solutions"""
        nature_analogies = self.analogy_database.get('nature', [])
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply biomimicry to solve:

{problem.to_prompt_context()}

Natural systems to consider:
{json.dumps(nature_analogies, indent=2)}

Biomimicry process:
1. Define function (what do we want to do?)
2. Biologize (how does nature do this?)
3. Discover natural models
4. Abstract design principles
5. Emulate nature's strategies

Focus on:
- Efficiency (nature wastes nothing)
- Adaptation (responsive to environment)
- Integration (everything connected)
- Resilience (self-healing, redundancy)

Generate nature-inspired solution."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.BIOMIMICRY
            )
            
        except Exception as e:
            logger.error(f"Error in biomimicry: {e}")
            return None
    
    async def _first_principles_approach(self, problem: Problem,
                                       analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Break down to fundamental truths and build up"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply first principles thinking:

{problem.to_prompt_context()}

Root causes identified: {', '.join(analysis.get('root_causes', []))}

First principles process:
1. Identify and define current assumptions
2. Break down to fundamental truths
3. Create new solution from scratch
4. Ignore how it's "always been done"
5. Focus on physics/logic, not convention

Questions to answer:
- What are we really trying to achieve?
- What are the absolute constraints (physics, not policy)?
- What would we build if starting fresh?
- How would an alien civilization solve this?

Generate groundbreaking solution."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.FIRST_PRINCIPLES
            )
            
        except Exception as e:
            logger.error(f"Error in first principles: {e}")
            return None
    
    async def _design_thinking_approach(self, problem: Problem,
                                      analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Human-centered design approach"""
        stakeholder_needs = analysis.get('stakeholder_needs', [])
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply design thinking to solve:

{problem.to_prompt_context()}

Stakeholder needs: {', '.join(stakeholder_needs)}

Design thinking process:
1. Empathize - Deep understanding of user needs
2. Define - Frame the right problem
3. Ideate - Generate creative solutions
4. Prototype - Quick, testable versions
5. Test - Learn and iterate

Focus on:
- Human needs and desires
- Emotional and functional requirements
- Journey mapping and pain points
- Delightful and intuitive solutions

Generate user-centered innovative solution."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.DESIGN_THINKING
            )
            
        except Exception as e:
            logger.error(f"Error in design thinking: {e}")
            return None
    
    async def _systems_thinking_approach(self, problem: Problem,
                                       analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """See the whole system and its interactions"""
        interdependencies = analysis.get('interdependencies', [])
        
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply systems thinking:

{problem.to_prompt_context()}

System interdependencies: {', '.join(interdependencies)}

Systems thinking approach:
1. Map the whole system
2. Identify feedback loops
3. Find leverage points
4. Consider emergent properties
5. Design interventions

Consider:
- Stocks and flows
- Balancing and reinforcing loops
- Delays and buffers
- System boundaries
- Unintended consequences

Generate holistic solution that improves entire system."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.SYSTEMS_THINKING
            )
            
        except Exception as e:
            logger.error(f"Error in systems thinking: {e}")
            return None
    
    async def _morphological_analysis_approach(self, problem: Problem,
                                             analysis: Dict[str, Any]) -> Optional[CreativeSolution]:
        """Systematic exploration of solution space"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-opus-20240229",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": f"""Apply morphological analysis:

{problem.to_prompt_context()}

Process:
1. Identify key parameters/dimensions
2. List possible values for each parameter
3. Create morphological box (matrix)
4. Explore unusual combinations
5. Select promising configurations

Focus on:
- Complete parameter space
- Non-obvious combinations
- Emergent solutions
- Cross-parameter synergies

Generate innovative solution from morphological exploration."""
                }]
            )
            
            return self._parse_solution(
                response.content[0].text, problem, SolutionApproach.MORPHOLOGICAL_ANALYSIS
            )
            
        except Exception as e:
            logger.error(f"Error in morphological analysis: {e}")
            return None
    
    def _find_analogies(self, problem: Problem, 
                       analogous_problems: List[str]) -> List[Dict[str, Any]]:
        """Find relevant analogies from database"""
        relevant = []
        
        # Search all categories
        for category, analogies in self.analogy_database.items():
            for analogy in analogies:
                # Simple relevance scoring
                relevance_score = 0
                for principle in analogy['principles']:
                    if any(principle in prob.lower() for prob in analogous_problems):
                        relevance_score += 1
                    if principle in problem.description.lower():
                        relevance_score += 0.5
                
                if relevance_score > 0:
                    relevant.append({
                        'category': category,
                        'system': analogy['system'],
                        'principles': analogy['principles'],
                        'relevance': relevance_score
                    })
        
        # Sort by relevance
        relevant.sort(key=lambda x: x['relevance'], reverse=True)
        
        return relevant[:5]
    
    def _extract_combinable_elements(self, problem: Problem,
                                   analysis: Dict[str, Any]) -> Dict[str, List[str]]:
        """Extract elements that can be combined"""
        elements = {
            'technologies': ['AI', 'automation', 'visualization', 'integration'],
            'approaches': ['parallel', 'sequential', 'hierarchical', 'distributed'],
            'resources': ['time', 'computing', 'human', 'data'],
            'interfaces': ['voice', 'visual', 'gesture', 'API'],
            'workflows': ['batch', 'streaming', 'interactive', 'autonomous']
        }
        
        # Add problem-specific elements
        if problem.problem_type == ProblemType.WORKFLOW_OPTIMIZATION:
            elements['optimizations'] = ['caching', 'parallelization', 'elimination', 'combination']
        
        return elements
    
    def _parse_solution(self, solution_text: str, problem: Problem,
                       approach: SolutionApproach) -> Optional[CreativeSolution]:
        """Parse solution from AI response"""
        try:
            # Extract key components (simplified parsing)
            solution = CreativeSolution(
                solution_id=self._generate_solution_id(problem, approach),
                problem_id=problem.problem_id,
                approach=approach,
                description=self._extract_description(solution_text),
                implementation_steps=self._extract_steps(solution_text),
                innovation_score=self._calculate_innovation_score(solution_text),
                feasibility_score=self._calculate_feasibility_score(solution_text),
                impact_score=self._calculate_impact_score(solution_text),
                resources_required=self._extract_resources(solution_text),
                estimated_time=self._estimate_time(solution_text),
                risks=self._extract_risks(solution_text),
                alternatives=self._extract_alternatives(solution_text),
                synergies=self._extract_synergies(solution_text)
            )
            
            return solution
            
        except Exception as e:
            logger.error(f"Error parsing solution: {e}")
            return None
    
    def _generate_solution_id(self, problem: Problem, 
                            approach: SolutionApproach) -> str:
        """Generate unique solution ID"""
        content = f"{problem.problem_id}_{approach.value}_{datetime.now().isoformat()}"
        return hashlib.md5(content.encode()).hexdigest()[:12]
    
    def _extract_description(self, text: str) -> str:
        """Extract solution description"""
        # Simple extraction - would be more sophisticated in practice
        lines = text.split('\n')
        for i, line in enumerate(lines):
            if 'solution' in line.lower() and ':' in line:
                return lines[i+1] if i+1 < len(lines) else line.split(':')[1]
        return text[:200]
    
    def _extract_steps(self, text: str) -> List[Dict[str, Any]]:
        """Extract implementation steps"""
        steps = []
        lines = text.split('\n')
        
        for line in lines:
            if any(marker in line for marker in ['1.', '2.', '3.', '-', '•']):
                step_text = line.strip().lstrip('1234567890.-• ')
                if step_text:
                    steps.append({
                        'description': step_text,
                        'complexity': 'medium',
                        'duration': '1-2 days'
                    })
        
        return steps[:10]  # Limit to 10 steps
    
    def _calculate_innovation_score(self, text: str) -> float:
        """Calculate innovation score from solution text"""
        innovation_keywords = [
            'novel', 'unique', 'breakthrough', 'revolutionary', 'innovative',
            'creative', 'original', 'unprecedented', 'groundbreaking', 'radical'
        ]
        
        score = 0.5  # Base score
        text_lower = text.lower()
        
        for keyword in innovation_keywords:
            if keyword in text_lower:
                score += 0.05
        
        return min(score, 0.95)
    
    def _calculate_feasibility_score(self, text: str) -> float:
        """Calculate feasibility score"""
        feasibility_keywords = [
            'practical', 'implementable', 'realistic', 'achievable', 'straightforward',
            'proven', 'tested', 'viable', 'workable', 'doable'
        ]
        
        difficulty_keywords = [
            'complex', 'difficult', 'challenging', 'risky', 'uncertain',
            'experimental', 'theoretical', 'untested', 'problematic'
        ]
        
        score = 0.6  # Base score
        text_lower = text.lower()
        
        for keyword in feasibility_keywords:
            if keyword in text_lower:
                score += 0.05
        
        for keyword in difficulty_keywords:
            if keyword in text_lower:
                score -= 0.05
        
        return max(0.2, min(score, 0.95))
    
    def _calculate_impact_score(self, text: str) -> float:
        """Calculate impact score"""
        impact_keywords = [
            'significant', 'transformative', 'major', 'substantial', 'dramatic',
            'powerful', 'game-changing', 'impactful', 'revolutionary'
        ]
        
        score = 0.5
        text_lower = text.lower()
        
        for keyword in impact_keywords:
            if keyword in text_lower:
                score += 0.06
        
        # Check for quantified benefits
        import re
        percentages = re.findall(r'(\d+)%', text)
        for pct in percentages:
            if int(pct) > 50:
                score += 0.1
        
        return min(score, 0.95)
    
    def _extract_resources(self, text: str) -> List[str]:
        """Extract required resources"""
        resources = []
        resource_keywords = ['require', 'need', 'resource', 'tool', 'system']
        
        lines = text.split('\n')
        for line in lines:
            if any(keyword in line.lower() for keyword in resource_keywords):
                resources.append(line.strip())
        
        return resources[:5]
    
    def _estimate_time(self, text: str) -> str:
        """Estimate implementation time"""
        import re
        
        # Look for time mentions
        time_patterns = [
            r'(\d+)\s*(hour|day|week|month)',
            r'(few|several)\s*(hours|days|weeks|months)'
        ]
        
        for pattern in time_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(0)
        
        # Default based on step count
        steps = self._extract_steps(text)
        if len(steps) <= 3:
            return "1-2 weeks"
        elif len(steps) <= 6:
            return "2-4 weeks"
        else:
            return "1-2 months"
    
    def _extract_risks(self, text: str) -> List[Dict[str, Any]]:
        """Extract risks from solution"""
        risks = []
        risk_keywords = ['risk', 'challenge', 'concern', 'issue', 'problem']
        
        lines = text.split('\n')
        for line in lines:
            if any(keyword in line.lower() for keyword in risk_keywords):
                risks.append({
                    'description': line.strip(),
                    'severity': 'medium',
                    'mitigation': 'Monitor and adjust as needed'
                })
        
        return risks[:3]
    
    def _extract_alternatives(self, text: str) -> List[str]:
        """Extract alternative approaches"""
        alternatives = []
        alt_keywords = ['alternative', 'another option', 'could also', 'or']
        
        lines = text.split('\n')
        for line in lines:
            if any(keyword in line.lower() for keyword in alt_keywords):
                alternatives.append(line.strip())
        
        return alternatives[:3]
    
    def _extract_synergies(self, text: str) -> List[str]:
        """Extract synergies with existing systems"""
        synergies = []
        synergy_keywords = ['integrate', 'combine', 'work with', 'complement', 'enhance']
        
        lines = text.split('\n')
        for line in lines:
            if any(keyword in line.lower() for keyword in synergy_keywords):
                synergies.append(line.strip())
        
        return synergies[:3]
    
    async def _create_hybrid_solutions(self, solutions: List[CreativeSolution],
                                     problem: Problem) -> List[CreativeSolution]:
        """Create hybrid solutions by combining approaches"""
        hybrids = []
        
        # Try combining top solutions
        for i in range(min(len(solutions), 3)):
            for j in range(i + 1, min(len(solutions), 3)):
                hybrid = await self._hybridize_solutions(
                    solutions[i], solutions[j], problem
                )
                if hybrid:
                    hybrids.append(hybrid)
        
        return hybrids
    
    async def _hybridize_solutions(self, sol1: CreativeSolution,
                                 sol2: CreativeSolution,
                                 problem: Problem) -> Optional[CreativeSolution]:
        """Create hybrid from two solutions"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=800,
                messages=[{
                    "role": "user",
                    "content": f"""Combine these two solutions creatively:

Solution 1 ({sol1.approach.value}): {sol1.description}
Solution 2 ({sol2.approach.value}): {sol2.description}

Create a hybrid that:
- Takes the best of both approaches
- Creates synergistic benefits
- Addresses more objectives
- Maintains feasibility

Provide brief hybrid solution description and key benefits."""
                }]
            )
            
            hybrid_text = response.content[0].text
            
            return CreativeSolution(
                solution_id=f"hybrid_{sol1.solution_id[:6]}_{sol2.solution_id[:6]}",
                problem_id=problem.problem_id,
                approach=SolutionApproach.COMBINATORIAL_CREATIVITY,
                description=f"Hybrid: {hybrid_text[:200]}",
                implementation_steps=sol1.implementation_steps[:3] + sol2.implementation_steps[:3],
                innovation_score=(sol1.innovation_score + sol2.innovation_score) / 2 + 0.1,
                feasibility_score=(sol1.feasibility_score + sol2.feasibility_score) / 2 - 0.1,
                impact_score=(sol1.impact_score + sol2.impact_score) / 2 + 0.05,
                resources_required=list(set(sol1.resources_required + sol2.resources_required)),
                estimated_time="Varies based on chosen elements",
                risks=sol1.risks[:1] + sol2.risks[:1],
                alternatives=[],
                synergies=sol1.synergies + sol2.synergies
            )
            
        except Exception as e:
            logger.error(f"Error creating hybrid: {e}")
            return None
    
    def _rank_solutions(self, solutions: List[CreativeSolution]) -> List[CreativeSolution]:
        """Rank solutions by overall quality"""
        return sorted(solutions, key=lambda s: s.get_overall_score(), reverse=True)
    
    async def _refine_top_solutions(self, solutions: List[CreativeSolution],
                                   problem: Problem) -> List[CreativeSolution]:
        """Refine top solutions for better quality"""
        refined = []
        
        for solution in solutions:
            refined_solution = await self._refine_solution(solution, problem)
            refined.append(refined_solution or solution)
        
        return refined
    
    async def _refine_solution(self, solution: CreativeSolution,
                             problem: Problem) -> Optional[CreativeSolution]:
        """Refine a single solution"""
        try:
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model="claude-3-haiku-20240307",
                max_tokens=600,
                messages=[{
                    "role": "user",
                    "content": f"""Refine this solution:

{solution.description}

Make it more:
1. Practical and implementable
2. Specific with clear actions
3. Measurable with success metrics
4. Risk-aware with mitigation strategies

Keep the innovative aspects while improving feasibility."""
                }]
            )
            
            refined_text = response.content[0].text
            
            # Update solution with refinements
            solution.description = refined_text[:500]
            solution.feasibility_score = min(solution.feasibility_score + 0.1, 0.95)
            
            return solution
            
        except Exception as e:
            logger.error(f"Error refining solution: {e}")
            return None
    
    async def learn_from_implementation(self, solution_id: str,
                                      outcome: Dict[str, Any]):
        """Learn from solution implementation results"""
        # Find solution
        solution = next((s for s in self.solution_history if s.solution_id == solution_id), None)
        if not solution:
            return
        
        # Update approach effectiveness
        success_score = outcome.get('success_score', 0.5)
        self.approach_effectiveness[solution.approach] = (
            self.approach_effectiveness[solution.approach] * 0.8 + success_score * 0.2
        )
        
        # Store pattern
        self.solution_patterns[solution.problem_id].append({
            'approach': solution.approach,
            'success': success_score,
            'implementation_time': outcome.get('actual_time'),
            'key_factors': outcome.get('key_factors', [])
        })
        
        # Update metrics
        if success_score > 0.7:
            self.innovation_metrics['successful_implementations'] += 1
            time_saved = outcome.get('time_saved_hours', 0)
            self.innovation_metrics['time_saved'] += time_saved
    
    def get_innovation_stats(self) -> Dict[str, Any]:
        """Get innovation and problem-solving statistics"""
        return {
            'innovation_metrics': self.innovation_metrics,
            'approach_effectiveness': dict(self.approach_effectiveness),
            'total_solutions': len(self.solution_history),
            'active_problems': len(self.active_problems),
            'success_patterns': {
                problem_id: len(patterns)
                for problem_id, patterns in self.solution_patterns.items()
            }
        }


# Export main classes
__all__ = ['CreativeProblemSolver', 'Problem', 'CreativeSolution', 
           'ProblemType', 'SolutionApproach']