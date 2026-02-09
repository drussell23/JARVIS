"""
Math Solver for JARVIS v240.0
==============================

Sympy-based deterministic math solver that intercepts equations before
they reach the LLM. 7B models are unreliable at arithmetic (e.g., Mistral-7B
answered 5x+3=18 as x=11 instead of x=3). This ensures exact answers.

Capabilities:
- Algebraic equations: 5x+3=18 -> x=3
- Systems of equations: x+y=10 and x-y=2 -> x=6, y=4
- Arithmetic: 5+5 -> 10, 2^10 -> 1024
- Calculus: derivative of x^2 -> 2x, integral of 2x -> x^2
- Simplification: (x^2-1)/(x-1) -> x+1

Security: All input is sanitized via whitelist before sympy parsing.

Author: Derek J. Russell
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict
from enum import Enum

logger = logging.getLogger(__name__)

# Module-level sympy import with availability flag
try:
    import sympy
    from sympy.parsing.sympy_parser import (
        parse_expr,
        standard_transformations,
        implicit_multiplication,
    )
    SYMPY_AVAILABLE = True
except ImportError:
    SYMPY_AVAILABLE = False
    logger.warning("[MathSolver] sympy not installed — math solving disabled")


# =============================================================================
# INPUT SANITIZATION (security-critical)
# =============================================================================

# Only allow math-safe characters: digits, letters (variables), operators,
# parentheses, dots, commas, equals, spaces
_SAFE_MATH_CHARS = re.compile(r'^[\d a-zA-Z\+\-\*/\^\(\)\.\,\=\s]+$')

# Reject any token that could be used for code injection via parse_expr
_DANGEROUS_TOKENS = re.compile(
    r'__|import|exec|eval|open|system|compile|getattr|setattr|delattr'
    r'|globals|locals|__builtins__|lambda|class|def\s',
    re.IGNORECASE,
)


# =============================================================================
# CLASSIFICATION PATTERNS (compiled once at module level)
# =============================================================================

_EQUATION_WITH_VARIABLE = re.compile(
    r'(\d+\s*[a-zA-Z][\s\+\-\*/\^\d a-zA-Z()]*=\s*[\-]?\d+[\.\d]*)'  # 5x+3=18
    r'|([a-zA-Z][\s\+\-\*/\^\d a-zA-Z()]*=\s*[\-]?\d+[\.\d]*)'        # x+3=18
)

_PURE_ARITHMETIC = re.compile(
    r'([\-]?\d+[\.\d]*\s*[\+\-\*/\^\%]\s*[\-]?\d+[\.\d]*'
    r'(?:\s*[\+\-\*/\^\%]\s*[\-]?\d+[\.\d]*)*)'
)

_CALCULUS_VERB = re.compile(
    r'\b(derivative|differentiate|integrate|integral)\b', re.IGNORECASE
)

_MATH_VERB = re.compile(
    r'\b(solve|calculate|compute|simplify|factor|expand|evaluate)\b',
    re.IGNORECASE,
)


# =============================================================================
# DATA TYPES
# =============================================================================

class MathExpressionType(Enum):
    ARITHMETIC = "arithmetic"
    ALGEBRAIC = "algebraic"
    SYSTEM = "system"
    CALCULUS = "calculus"
    SIMPLIFICATION = "simplification"
    NONE = "none"


@dataclass
class MathResult:
    solved: bool
    expression_type: MathExpressionType
    original: str
    solution: str
    solution_steps: List[str] = field(default_factory=list)
    numeric_result: Optional[float] = None
    variables: Optional[Dict[str, str]] = None


# =============================================================================
# SOLVER
# =============================================================================

class MathSolver:
    """
    Deterministic math solver using sympy.

    Call detect_and_solve() to check if a query contains math
    and solve it if possible. Returns MathResult.
    """

    _TRANSFORMATIONS = None

    @classmethod
    def _get_transformations(cls):
        if cls._TRANSFORMATIONS is None and SYMPY_AVAILABLE:
            cls._TRANSFORMATIONS = standard_transformations + (implicit_multiplication,)
        return cls._TRANSFORMATIONS

    @staticmethod
    def _sanitize(expr: str) -> str:
        """Sanitize expression before sympy parsing. Rejects dangerous input."""
        if _DANGEROUS_TOKENS.search(expr):
            raise ValueError(f"Rejected unsafe expression: {expr[:40]}")
        if not _SAFE_MATH_CHARS.match(expr):
            raise ValueError(f"Expression contains disallowed characters: {expr[:40]}")
        return expr.replace('^', '**')

    def detect_and_solve(self, query: str) -> MathResult:
        """
        Detect math in query and solve if possible.

        Synchronous -- sympy is CPU-bound and fast (<50ms for typical equations).
        """
        _not_solved = MathResult(
            solved=False,
            expression_type=MathExpressionType.NONE,
            original=query,
            solution="",
        )

        if not SYMPY_AVAILABLE:
            return _not_solved

        try:
            expr_type, extracted = self._classify(query)
            if expr_type == MathExpressionType.NONE:
                return _not_solved

            if expr_type == MathExpressionType.ARITHMETIC:
                return self._solve_arithmetic(query, extracted)
            elif expr_type == MathExpressionType.ALGEBRAIC:
                return self._solve_algebraic(query, extracted)
            elif expr_type == MathExpressionType.SYSTEM:
                return self._solve_system(query, extracted)
            elif expr_type == MathExpressionType.CALCULUS:
                return self._solve_calculus(query, extracted)
            else:
                return self._solve_simplification(query, extracted)

        except Exception as e:
            logger.warning(f"[MathSolver] Failed to solve '{query[:60]}': {e}")
            return _not_solved

    def _classify(self, query: str) -> Tuple[MathExpressionType, str]:
        """Classify the type of math expression in the query."""
        # Calculus (most specific verb)
        if _CALCULUS_VERB.search(query):
            return MathExpressionType.CALCULUS, query

        # Algebraic equation (variable + operator + equals + number)
        eq_match = _EQUATION_WITH_VARIABLE.search(query)
        if eq_match:
            matched = eq_match.group(1) or eq_match.group(2)
            return MathExpressionType.ALGEBRAIC, matched.strip()

        # Pure arithmetic (digits + operators, no variables needed)
        arith_match = _PURE_ARITHMETIC.search(query)
        if arith_match:
            return MathExpressionType.ARITHMETIC, arith_match.group(1).strip()

        return MathExpressionType.NONE, ""

    # -------------------------------------------------------------------------
    # ARITHMETIC: 5+5, 2^10, 100/4
    # -------------------------------------------------------------------------

    def _solve_arithmetic(self, query: str, expr: str) -> MathResult:
        safe_expr = self._sanitize(expr)
        result = sympy.sympify(safe_expr)
        numeric = float(result) if result.is_number else None

        return MathResult(
            solved=True,
            expression_type=MathExpressionType.ARITHMETIC,
            original=expr,
            solution=str(result),
            solution_steps=[f"{expr} = {result}"],
            numeric_result=numeric,
        )

    # -------------------------------------------------------------------------
    # ALGEBRAIC: 5x+3=18 -> x=3
    # -------------------------------------------------------------------------

    def _solve_algebraic(self, query: str, expr: str) -> MathResult:
        parts = expr.split('=')
        if len(parts) != 2:
            return MathResult(
                solved=False, expression_type=MathExpressionType.ALGEBRAIC,
                original=expr, solution="",
            )

        lhs_str = self._sanitize(parts[0].strip())
        rhs_str = self._sanitize(parts[1].strip())
        transformations = self._get_transformations()

        lhs = parse_expr(lhs_str, transformations=transformations)
        rhs = parse_expr(rhs_str, transformations=transformations)

        equation = sympy.Eq(lhs, rhs)
        free_vars = equation.free_symbols

        if not free_vars:
            is_true = bool(lhs.equals(rhs))
            return MathResult(
                solved=True,
                expression_type=MathExpressionType.ALGEBRAIC,
                original=expr,
                solution=str(is_true),
                solution_steps=[f"{expr} is {is_true}"],
            )

        solutions = sympy.solve(equation, list(free_vars))

        if not solutions:
            return MathResult(
                solved=False, expression_type=MathExpressionType.ALGEBRAIC,
                original=expr, solution="No solution",
            )

        # Normalize solutions to dict form
        if isinstance(solutions, dict):
            var_solutions = solutions
        elif isinstance(solutions, list):
            var = list(free_vars)[0]
            if len(solutions) == 1:
                var_solutions = {var: solutions[0]}
            else:
                var_solutions = {var: solutions}
        else:
            var_solutions = {list(free_vars)[0]: solutions}

        solution_str = ", ".join(f"{v} = {s}" for v, s in var_solutions.items())

        # Generate step-by-step for linear equations (ax + b = c)
        steps = self._generate_linear_steps(lhs, rhs, free_vars, var_solutions, expr)

        # Numeric value
        numeric = None
        if len(var_solutions) == 1:
            val = list(var_solutions.values())[0]
            if isinstance(val, list):
                val = val[0]
            try:
                numeric = float(val)
            except (TypeError, ValueError):
                pass

        return MathResult(
            solved=True,
            expression_type=MathExpressionType.ALGEBRAIC,
            original=expr,
            solution=solution_str,
            solution_steps=steps,
            numeric_result=numeric,
            variables={str(k): str(v) for k, v in var_solutions.items()},
        )

    def _generate_linear_steps(self, lhs, rhs, free_vars, var_solutions, expr):
        """Generate step-by-step solution for linear equations."""
        steps = [f"Given: {expr}"]

        if len(free_vars) != 1:
            for var, val in var_solutions.items():
                steps.append(f"{var} = {val}")
            return steps

        var = list(free_vars)[0]

        try:
            # Check if linear: degree 1 in the variable
            diff_lhs = lhs - rhs  # Move everything to one side: lhs - rhs = 0
            poly = sympy.Poly(diff_lhs, var)

            if poly.degree() == 1:
                # Linear: ax + b = 0 where a is leading coeff, b is constant
                coeffs = poly.all_coeffs()  # [a, b] for ax + b
                a = coeffs[0]
                b = coeffs[1] if len(coeffs) > 1 else 0

                # Reconstruct: ax + b = 0 means lhs = rhs was rearranged
                # Original: lhs = rhs
                # Step: subtract constant from both sides
                # Step: divide by coefficient
                solution_val = list(var_solutions.values())[0]
                if isinstance(solution_val, list):
                    solution_val = solution_val[0]

                if b != 0:
                    # Show: subtract b from both sides -> ax = rhs - b
                    new_rhs = rhs - (lhs - a * var)  # This is just: rhs - (lhs without the ax term)
                    # Simpler approach: we know ax = -b, so ax = solution_val * a
                    ax_value = a * solution_val
                    steps.append(
                        f"Subtract {lhs - a * var} from both sides: "
                        f"{a}{var} = {ax_value}"
                    )

                if a != 1 and a != -1:
                    steps.append(
                        f"Divide both sides by {a}: "
                        f"{var} = {solution_val}"
                    )
                elif a == -1:
                    steps.append(
                        f"Multiply both sides by -1: "
                        f"{var} = {solution_val}"
                    )
                else:
                    steps.append(f"{var} = {solution_val}")

                steps.append(f"Answer: {var} = {solution_val}")
                return steps

        except (sympy.PolynomialError, Exception):
            pass

        # Fallback for non-linear or complex equations
        for var_name, val in var_solutions.items():
            if isinstance(val, list):
                for v in val:
                    steps.append(f"{var_name} = {v}")
            else:
                steps.append(f"{var_name} = {val}")
        steps.append(f"Answer: {', '.join(f'{k} = {v}' for k, v in var_solutions.items())}")
        return steps

    # -------------------------------------------------------------------------
    # SYSTEM: x+y=10, x-y=2
    # -------------------------------------------------------------------------

    def _solve_system(self, query: str, expr: str) -> MathResult:
        # Split on "and", ",", ";"
        eq_strings = re.split(r'\s+and\s+|[,;]\s*', expr)
        eq_strings = [s.strip() for s in eq_strings if '=' in s]

        if len(eq_strings) < 2:
            return MathResult(
                solved=False, expression_type=MathExpressionType.SYSTEM,
                original=expr, solution="",
            )

        transformations = self._get_transformations()
        equations = []
        all_vars = set()

        for eq_str in eq_strings:
            parts = eq_str.split('=')
            if len(parts) != 2:
                continue
            lhs = parse_expr(self._sanitize(parts[0].strip()), transformations=transformations)
            rhs = parse_expr(self._sanitize(parts[1].strip()), transformations=transformations)
            eq = sympy.Eq(lhs, rhs)
            equations.append(eq)
            all_vars |= eq.free_symbols

        if len(equations) < 2:
            return MathResult(
                solved=False, expression_type=MathExpressionType.SYSTEM,
                original=expr, solution="",
            )

        solutions = sympy.solve(equations, list(all_vars))

        if not solutions:
            return MathResult(
                solved=False, expression_type=MathExpressionType.SYSTEM,
                original=expr, solution="No solution",
            )

        if isinstance(solutions, dict):
            var_solutions = solutions
        else:
            var_solutions = solutions

        solution_str = ", ".join(f"{v} = {s}" for v, s in var_solutions.items())
        steps = [f"Given: {expr}"]
        for v, s in var_solutions.items():
            steps.append(f"{v} = {s}")
        steps.append(f"Answer: {solution_str}")

        return MathResult(
            solved=True,
            expression_type=MathExpressionType.SYSTEM,
            original=expr,
            solution=solution_str,
            solution_steps=steps,
            variables={str(k): str(v) for k, v in var_solutions.items()},
        )

    # -------------------------------------------------------------------------
    # CALCULUS: derivative of x^2 -> 2x
    # -------------------------------------------------------------------------

    def _solve_calculus(self, query: str, expr_str: str) -> MathResult:
        query_lower = query.lower()

        # Extract expression after "of" or "for"
        expr_match = re.search(
            r'(?:derivative|differentiate|integral|integrate)\s+(?:of\s+)?(.+)',
            query, re.IGNORECASE,
        )
        if not expr_match:
            return MathResult(
                solved=False, expression_type=MathExpressionType.CALCULUS,
                original=query, solution="",
            )

        math_expr = self._sanitize(expr_match.group(1).strip())
        transformations = self._get_transformations()
        parsed = parse_expr(math_expr, transformations=transformations)

        # Detect variable from free symbols instead of hardcoding x
        free_vars = parsed.free_symbols
        if not free_vars:
            return MathResult(
                solved=False, expression_type=MathExpressionType.CALCULUS,
                original=query, solution="No variable found",
            )

        if len(free_vars) == 1:
            var = free_vars.pop()
        else:
            # Check for "with respect to <var>"
            wrt_match = re.search(r'with\s+respect\s+to\s+([a-zA-Z])', query)
            if wrt_match:
                var = sympy.Symbol(wrt_match.group(1))
            else:
                # Default to alphabetically first variable
                var = sorted(free_vars, key=str)[0]

        if 'deriv' in query_lower or 'different' in query_lower:
            result = sympy.diff(parsed, var)
            op = "Derivative"
        else:
            result = sympy.integrate(parsed, var)
            op = "Integral"

        return MathResult(
            solved=True,
            expression_type=MathExpressionType.CALCULUS,
            original=query,
            solution=f"{result}",
            solution_steps=[f"{op} of {expr_match.group(1).strip()} with respect to {var} = {result}"],
        )

    # -------------------------------------------------------------------------
    # SIMPLIFICATION
    # -------------------------------------------------------------------------

    def _solve_simplification(self, query: str, expr: str) -> MathResult:
        safe_expr = self._sanitize(expr)
        transformations = self._get_transformations()
        parsed = parse_expr(safe_expr, transformations=transformations)
        simplified = sympy.simplify(parsed)

        return MathResult(
            solved=True,
            expression_type=MathExpressionType.SIMPLIFICATION,
            original=expr,
            solution=str(simplified),
            solution_steps=[f"{expr} simplifies to {simplified}"],
        )

    # -------------------------------------------------------------------------
    # RESPONSE FORMATTING
    # -------------------------------------------------------------------------

    def format_response(self, result: MathResult) -> str:
        """Format a human-readable response from the math result."""
        if not result.solved:
            return ""

        if result.expression_type == MathExpressionType.ARITHMETIC:
            return result.solution

        if result.expression_type in (
            MathExpressionType.ALGEBRAIC,
            MathExpressionType.SYSTEM,
        ):
            return "\n".join(result.solution_steps)

        if result.expression_type == MathExpressionType.CALCULUS:
            return "\n".join(result.solution_steps)

        if result.expression_type == MathExpressionType.SIMPLIFICATION:
            return "\n".join(result.solution_steps)

        return result.solution


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================

_solver: Optional[MathSolver] = None


def get_math_solver() -> MathSolver:
    """Get or create the math solver singleton."""
    global _solver
    if _solver is None:
        _solver = MathSolver()
        logger.info("[MathSolver] Initialized (sympy available: %s)", SYMPY_AVAILABLE)
    return _solver
