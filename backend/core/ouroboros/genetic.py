"""
Genetic Evolution Module for Ouroboros
=======================================

Implements genetic algorithm for code evolution with:
- Population management
- Fitness evaluation
- Selection strategies (tournament, roulette, rank)
- Crossover and mutation operations
- Elite preservation

The genetic approach allows exploring multiple improvement paths
simultaneously and combining the best features from different solutions.

Author: Trinity System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

from backend.core.ouroboros.engine import (
    CodeChange,
    EvolutionCandidate,
    OuroborosConfig,
    ValidationResult,
)

T = TypeVar('T')


# =============================================================================
# SELECTION STRATEGIES
# =============================================================================

class SelectionStrategy(Enum):
    """Selection strategies for genetic algorithm."""
    TOURNAMENT = "tournament"
    ROULETTE = "roulette"
    RANK = "rank"
    ELITIST = "elitist"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Chromosome:
    """
    A chromosome representing a code improvement approach.

    Genes encode different aspects of the improvement:
    - approach: The general strategy (refactor, optimize, fix)
    - focus: What to focus on (performance, readability, safety)
    - style: Code style preferences
    - parameters: Numeric parameters for the approach
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    genes: Dict[str, Any] = field(default_factory=dict)
    fitness: float = 0.0
    age: int = 0
    lineage: List[str] = field(default_factory=list)

    def mutate(self, mutation_rate: float = 0.3) -> "Chromosome":
        """Create a mutated copy of this chromosome."""
        new_genes = dict(self.genes)

        for key, value in new_genes.items():
            if random.random() < mutation_rate:
                if isinstance(value, bool):
                    new_genes[key] = not value
                elif isinstance(value, int):
                    new_genes[key] = value + random.randint(-2, 2)
                elif isinstance(value, float):
                    new_genes[key] = value * random.uniform(0.8, 1.2)
                elif isinstance(value, str):
                    # For string genes, we might select from alternatives
                    pass

        return Chromosome(
            genes=new_genes,
            lineage=self.lineage + [self.id],
        )

    @classmethod
    def crossover(cls, parent1: "Chromosome", parent2: "Chromosome") -> Tuple["Chromosome", "Chromosome"]:
        """Perform crossover between two chromosomes."""
        all_keys = set(parent1.genes.keys()) | set(parent2.genes.keys())

        child1_genes = {}
        child2_genes = {}

        for key in all_keys:
            if random.random() < 0.5:
                child1_genes[key] = parent1.genes.get(key)
                child2_genes[key] = parent2.genes.get(key)
            else:
                child1_genes[key] = parent2.genes.get(key)
                child2_genes[key] = parent1.genes.get(key)

        child1 = cls(
            genes=child1_genes,
            lineage=[parent1.id, parent2.id],
        )
        child2 = cls(
            genes=child2_genes,
            lineage=[parent1.id, parent2.id],
        )

        return child1, child2

    def to_prompt_modifiers(self) -> str:
        """Convert genes to prompt modifiers for LLM."""
        modifiers = []

        approach = self.genes.get("approach", "general")
        focus = self.genes.get("focus", "balanced")
        style = self.genes.get("style", "clean")

        if approach == "conservative":
            modifiers.append("Make minimal changes while achieving the goal.")
        elif approach == "aggressive":
            modifiers.append("Feel free to make significant restructuring if it improves the code.")
        elif approach == "incremental":
            modifiers.append("Make small, incremental improvements.")

        if focus == "performance":
            modifiers.append("Prioritize runtime performance.")
        elif focus == "readability":
            modifiers.append("Prioritize code readability and maintainability.")
        elif focus == "safety":
            modifiers.append("Prioritize type safety and error handling.")

        if style == "verbose":
            modifiers.append("Use descriptive variable names and add helpful comments.")
        elif style == "minimal":
            modifiers.append("Keep the code concise and avoid unnecessary verbosity.")

        return " ".join(modifiers)


@dataclass
class Population:
    """
    A population of chromosomes for evolution.

    Manages the lifecycle of chromosomes through generations:
    - Initialization
    - Selection
    - Crossover
    - Mutation
    - Replacement
    """
    chromosomes: List[Chromosome] = field(default_factory=list)
    generation: int = 0
    best_fitness: float = 0.0
    average_fitness: float = 0.0
    diversity_score: float = 1.0

    def __len__(self) -> int:
        return len(self.chromosomes)

    def initialize(self, size: int) -> None:
        """Initialize population with random chromosomes."""
        approaches = ["conservative", "aggressive", "incremental", "general"]
        focuses = ["performance", "readability", "safety", "balanced"]
        styles = ["verbose", "minimal", "clean"]

        for _ in range(size):
            genes = {
                "approach": random.choice(approaches),
                "focus": random.choice(focuses),
                "style": random.choice(styles),
                "temperature": random.uniform(0.1, 0.7),
                "creativity": random.uniform(0.0, 1.0),
            }
            self.chromosomes.append(Chromosome(genes=genes))

    def update_stats(self) -> None:
        """Update population statistics."""
        if not self.chromosomes:
            return

        fitnesses = [c.fitness for c in self.chromosomes]
        self.best_fitness = max(fitnesses)
        self.average_fitness = sum(fitnesses) / len(fitnesses)

        # Calculate diversity based on gene variance
        self._calculate_diversity()

    def _calculate_diversity(self) -> None:
        """Calculate population diversity score."""
        if len(self.chromosomes) < 2:
            self.diversity_score = 1.0
            return

        # Count unique gene combinations
        unique_genes = set()
        for c in self.chromosomes:
            gene_hash = hashlib.md5(str(sorted(c.genes.items())).encode()).hexdigest()
            unique_genes.add(gene_hash)

        self.diversity_score = len(unique_genes) / len(self.chromosomes)

    def get_best(self, n: int = 1) -> List[Chromosome]:
        """Get the n best chromosomes by fitness."""
        sorted_chroms = sorted(self.chromosomes, key=lambda c: c.fitness, reverse=True)
        return sorted_chroms[:n]

    def select(
        self,
        n: int,
        strategy: SelectionStrategy = SelectionStrategy.TOURNAMENT,
    ) -> List[Chromosome]:
        """Select n chromosomes using the specified strategy."""
        if strategy == SelectionStrategy.TOURNAMENT:
            return self._tournament_select(n)
        elif strategy == SelectionStrategy.ROULETTE:
            return self._roulette_select(n)
        elif strategy == SelectionStrategy.RANK:
            return self._rank_select(n)
        else:  # ELITIST
            return self.get_best(n)

    def _tournament_select(self, n: int, tournament_size: int = 3) -> List[Chromosome]:
        """Tournament selection."""
        selected = []
        for _ in range(n):
            tournament = random.sample(self.chromosomes, min(tournament_size, len(self.chromosomes)))
            winner = max(tournament, key=lambda c: c.fitness)
            selected.append(winner)
        return selected

    def _roulette_select(self, n: int) -> List[Chromosome]:
        """Roulette wheel selection (fitness proportionate)."""
        total_fitness = sum(c.fitness for c in self.chromosomes)
        if total_fitness == 0:
            return random.sample(self.chromosomes, min(n, len(self.chromosomes)))

        selected = []
        for _ in range(n):
            pick = random.uniform(0, total_fitness)
            current = 0
            for c in self.chromosomes:
                current += c.fitness
                if current >= pick:
                    selected.append(c)
                    break
        return selected

    def _rank_select(self, n: int) -> List[Chromosome]:
        """Rank-based selection."""
        sorted_chroms = sorted(self.chromosomes, key=lambda c: c.fitness)
        ranks = list(range(1, len(sorted_chroms) + 1))
        total_rank = sum(ranks)

        selected = []
        for _ in range(n):
            pick = random.uniform(0, total_rank)
            current = 0
            for i, c in enumerate(sorted_chroms):
                current += ranks[i]
                if current >= pick:
                    selected.append(c)
                    break
        return selected

    def evolve(
        self,
        elite_size: int = 1,
        mutation_rate: float = 0.3,
        crossover_rate: float = 0.7,
    ) -> "Population":
        """Create the next generation through evolution."""
        next_gen = []

        # Preserve elite
        elite = self.get_best(elite_size)
        for e in elite:
            e.age += 1
        next_gen.extend(elite)

        # Fill rest of population
        while len(next_gen) < len(self.chromosomes):
            # Select parents
            parents = self.select(2, SelectionStrategy.TOURNAMENT)

            if random.random() < crossover_rate and len(parents) >= 2:
                # Crossover
                child1, child2 = Chromosome.crossover(parents[0], parents[1])
                children = [child1, child2]
            else:
                # Clone
                children = [Chromosome(genes=dict(p.genes)) for p in parents]

            # Mutate
            for child in children:
                if random.random() < mutation_rate:
                    child = child.mutate(mutation_rate)
                next_gen.append(child)

        # Trim to population size
        next_gen = next_gen[:len(self.chromosomes)]

        new_pop = Population(
            chromosomes=next_gen,
            generation=self.generation + 1,
        )
        new_pop.update_stats()

        return new_pop


# =============================================================================
# FITNESS FUNCTION
# =============================================================================

class FitnessFunction(ABC):
    """Base class for fitness evaluation."""

    @abstractmethod
    async def evaluate(
        self,
        chromosome: Chromosome,
        candidate: EvolutionCandidate,
    ) -> float:
        """Evaluate the fitness of a candidate."""
        pass


class TestBasedFitness(FitnessFunction):
    """Fitness based on test results."""

    def __init__(
        self,
        test_weight: float = 0.6,
        coverage_weight: float = 0.2,
        complexity_weight: float = 0.1,
        time_weight: float = 0.1,
    ):
        self.test_weight = test_weight
        self.coverage_weight = coverage_weight
        self.complexity_weight = complexity_weight
        self.time_weight = time_weight

    async def evaluate(
        self,
        chromosome: Chromosome,
        candidate: EvolutionCandidate,
    ) -> float:
        """
        Evaluate fitness based on multiple criteria:
        - Test pass rate
        - Code coverage
        - Complexity reduction
        - Execution time
        """
        if not candidate.validation:
            return 0.0

        v = candidate.validation

        # Test score (0-1)
        total_tests = v.passed_tests + v.failed_tests
        test_score = v.passed_tests / total_tests if total_tests > 0 else 0.0

        # Coverage score (0-1)
        coverage_score = v.coverage_percent / 100.0

        # Complexity score (lower is better, normalize)
        # TODO: Implement actual complexity calculation
        complexity_score = 0.5

        # Time score (faster is better, normalize with baseline)
        baseline_time = 60.0
        time_score = max(0, 1 - (v.execution_time / baseline_time))

        # Weighted combination
        fitness = (
            self.test_weight * test_score +
            self.coverage_weight * coverage_score +
            self.complexity_weight * complexity_score +
            self.time_weight * time_score
        )

        return fitness


class MultiObjectiveFitness(FitnessFunction):
    """Multi-objective fitness using Pareto dominance."""

    def __init__(self):
        self.objectives = [
            "test_pass_rate",
            "coverage",
            "complexity",
            "maintainability",
        ]

    async def evaluate(
        self,
        chromosome: Chromosome,
        candidate: EvolutionCandidate,
    ) -> float:
        """Evaluate using Pareto ranking."""
        # For simplicity, use weighted sum
        # In full implementation, would use NSGA-II or similar
        if not candidate.validation:
            return 0.0

        v = candidate.validation
        total_tests = v.passed_tests + v.failed_tests

        scores = {
            "test_pass_rate": v.passed_tests / total_tests if total_tests > 0 else 0.0,
            "coverage": v.coverage_percent / 100.0,
            "complexity": 0.5,  # Placeholder
            "maintainability": 0.5,  # Placeholder
        }

        return sum(scores.values()) / len(scores)


# =============================================================================
# GENETIC EVOLVER
# =============================================================================

class GeneticEvolver:
    """
    Orchestrates genetic evolution for code improvement.

    Manages:
    - Population lifecycle
    - Fitness evaluation
    - Selection and reproduction
    - Convergence detection
    """

    def __init__(
        self,
        population_size: int = OuroborosConfig.POPULATION_SIZE,
        elite_size: int = OuroborosConfig.ELITE_SIZE,
        mutation_rate: float = OuroborosConfig.MUTATION_RATE,
        crossover_rate: float = OuroborosConfig.CROSSOVER_RATE,
        max_generations: int = 20,
        convergence_threshold: float = 0.95,
    ):
        self.population_size = population_size
        self.elite_size = elite_size
        self.mutation_rate = mutation_rate
        self.crossover_rate = crossover_rate
        self.max_generations = max_generations
        self.convergence_threshold = convergence_threshold

        self._population: Optional[Population] = None
        self._fitness_function: FitnessFunction = TestBasedFitness()
        self._history: List[Population] = []

    def initialize_population(self) -> Population:
        """Create initial population."""
        self._population = Population()
        self._population.initialize(self.population_size)
        self._history.append(self._population)
        return self._population

    async def evaluate_population(
        self,
        candidates: List[EvolutionCandidate],
    ) -> None:
        """Evaluate fitness for all chromosomes in population."""
        if not self._population:
            return

        # Map chromosomes to candidates
        for i, chromosome in enumerate(self._population.chromosomes):
            if i < len(candidates):
                candidate = candidates[i]
                fitness = await self._fitness_function.evaluate(chromosome, candidate)
                chromosome.fitness = fitness
                candidate.fitness_score = fitness

        self._population.update_stats()

    def evolve(self) -> Population:
        """Create next generation."""
        if not self._population:
            return self.initialize_population()

        self._population = self._population.evolve(
            elite_size=self.elite_size,
            mutation_rate=self.mutation_rate,
            crossover_rate=self.crossover_rate,
        )

        self._history.append(self._population)
        return self._population

    def is_converged(self) -> bool:
        """Check if population has converged."""
        if not self._population:
            return False

        # Check if best fitness is above threshold
        if self._population.best_fitness >= self.convergence_threshold:
            return True

        # Check if diversity is too low (stuck in local optimum)
        if self._population.diversity_score < 0.1:
            return True

        # Check if no improvement for several generations
        if len(self._history) >= 5:
            recent_best = [p.best_fitness for p in self._history[-5:]]
            if max(recent_best) - min(recent_best) < 0.01:
                return True

        return False

    def get_best_chromosome(self) -> Optional[Chromosome]:
        """Get the best chromosome from current population."""
        if not self._population:
            return None
        best = self._population.get_best(1)
        return best[0] if best else None

    def get_statistics(self) -> Dict[str, Any]:
        """Get evolution statistics."""
        if not self._population:
            return {}

        return {
            "generation": self._population.generation,
            "best_fitness": self._population.best_fitness,
            "average_fitness": self._population.average_fitness,
            "diversity": self._population.diversity_score,
            "population_size": len(self._population),
            "history_length": len(self._history),
        }
