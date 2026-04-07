# RSI Convergence Framework for Ouroboros

> **Theoretical Foundation**: Maps Wenyi Wang's _"A Formulation of RSI & Its Possible Efficiency"_ (UBC, arXiv:1805.06610) onto the Ouroboros self-development pipeline, extending the formulation to handle non-stationary, multi-repository, memory-augmented recursive self-improvement.

**Status**: Architecture specification  
**Audience**: Developers extending Ouroboros governance, researchers studying RSI convergence  
**Dependencies**: `self_evolution.py`, `graduation_orchestrator.py`, `oracle.py`, `orchestrator.py`, `ledger.py`

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Wang's RSI Formulation — Full Mathematical Treatment](#2-wangs-rsi-formulation--full-mathematical-treatment)
   - [2.1 Core Definition](#21-core-definition)
   - [2.2 Markov Chain Formulation](#22-markov-chain-formulation)
   - [2.3 Score Function Construction — The Dijkstra-Like Algorithm](#23-score-function-construction--the-dijkstra-like-algorithm)
   - [2.4 The Main Proof — Nondecreasing Scores (Full Detail)](#24-the-main-proof--nondecreasing-scores-full-detail)
   - [2.5 Key Results](#25-key-results)
   - [2.6 Accuracy Assessment](#26-accuracy-assessment)
3. [Mapping Wang to Ouroboros](#3-mapping-wang-to-ouroboros)
4. [Gap Analysis](#4-gap-analysis)
5. [Improvement 1: Composite Score Function](#5-improvement-1-composite-score-function)
6. [Improvement 2: Convergence Monitoring](#6-improvement-2-convergence-monitoring)
7. [Improvement 3: Adaptive Graduation Threshold](#7-improvement-3-adaptive-graduation-threshold)
8. [Improvement 4: Oracle Pre-Scoring](#8-improvement-4-oracle-pre-scoring)
9. [Improvement 5: Transition Probability Tracking](#9-improvement-5-transition-probability-tracking)
10. [Improvement 6: Vindication Reflection](#10-improvement-6-vindication-reflection)
11. [Cross-Cutting Concerns](#11-cross-cutting-concerns)
12. [Mathematical Appendix](#12-mathematical-appendix)
13. [References](#13-references)

---

## 1. Executive Summary

Ouroboros is already a functional RSI system: it generates code patches, validates them, and graduates proven capabilities into permanent agents. However, it lacks the **mathematical backbone** that Wang's formulation provides. Specifically:

- **No unified score function** — quality signals (pytest, coverage, complexity, lint) exist independently but are never composed into a single consistent metric.
- **Static graduation threshold** — the fixed count of 3 successful uses ignores the probabilistic quality of those successes.
- **No convergence monitoring** — the system cannot detect whether it's improving logarithmically (healthy), plateauing, or oscillating.
- **No pre-scoring** — every candidate must go through full validation; there's no fast approximate quality gate.
- **No technique-level transition tracking** — the 9 self-evolution techniques are selected without empirical probability data.
- **No self-reflection on improvement trajectory** — the system validates patches but never asks "will this make future patches better?"

This document specifies six improvements that give Ouroboros a mathematically grounded RSI convergence framework, drawing directly from Wang's theorems while extending them for the Trinity ecosystem's unique properties.

---

## 2. Wang's RSI Formulation — Full Mathematical Treatment

### 2.1 Core Definition

**Definition 1 (RSI System)**: Given a finite set of programs _P_ and a score function _S_ over _P_:
1. Initialize _p_ from _P_ as the system's current program.
2. Repeat until criterion satisfied: generate _p' ∈ P_ using _p_.
3. If _p'_ is better than _p_ according to _S_, replace _p_ by _p'_.

A total order over a finite set is isomorphic to a score function: programs can always be ranked. Lower score = better program (closer to optimal).

### 2.2 Markov Chain Formulation

Wang makes a simplifying assumption: **the way a program generates a new program is independent of history**. Each program _p_ defines a fixed probabilistic distribution over _P_ for what it produces next. This independence assumption makes the RSI process a **homogeneous Markov chain** where:

- **States** = programs in _P_
- **Transition probability** from _p_i_ to _p_j_ = probability that _p_i_ generates _p_j_, subject to the replacement rule (only transition if _S(p_j) < S(p_i)_)
- The optimal program _p*_ is an **absorbing state** (once reached, never left — it has the lowest score)

**Concrete example.** Consider _P = {p₁, p₂, p₃, p₄}_ with _S(pᵢ) = i_. Each program's generation distribution is a weight vector _wᵢ_ over _P_:

```
w₁ = [0.97, 0.01, 0.01, 0.01]   (p₁ almost always generates itself)
w₂ = [0.75, 0.00, 0.25, 0.00]   (p₂ generates p₁ or p₃)
w₃ = [0.25, 0.25, 0.25, 0.25]   (p₃ generates uniformly)
w₄ = [0.00, 0.58, 0.00, 0.42]   (p₄ generates p₂ or itself)
```

Starting from _p₃_: First, _p₃_ generates _p₄_. Since _S(p₄) = 4 > S(p₃) = 3_, the current program is **not** updated (p₄ is worse). Then _p₃_ generates _p₂_. Since _S(p₂) = 2 < S(p₃) = 3_, the current program updates to _p₂_. Next, _p₂_ generates _p₁_. Since _S(p₁) = 1 < S(p₂) = 2_, we update to _p₁_. Since _p₁_ has the lowest score (rank 1), no future program can improve on it — it is the absorbing state.

The replacement rule modifies the raw generation probabilities into the Markov chain's actual transition matrix: transitions to worse programs are redirected as self-loops (staying at the current program).

### 2.3 Score Function Construction — The Dijkstra-Like Algorithm

Wang's central contribution is showing how to construct a **consistent** score function as the expected number of steps to reach the optimal program. The algorithm is analogous to Dijkstra's shortest-path algorithm:

#### 2.3.1 Definition of Consistency

A score function _S_ is **consistent** if for all _p, p' ∈ P_: _S(p) > S(p')_ implies that the expected number of steps to reach the optimal program from _p_ is greater than from _p'_, following the RSI process defined by _S_ itself.

This is a self-referential definition — the score function must agree with the process it induces.

#### 2.3.2 Construction Algorithm

1. **Initialize**: Fix _p₁_ as the optimal program. Set _S(p₁) = 0_. Set _S(pᵢ) = ∞_ for all _i ≥ 2_. Build the initial Markov chain with only _p₁_ as an absorbing state.

2. **Iterate**: At each step _i_ (for _i = 2, 3, ..., n_):
   - For every program _p_ with _S(p) = ∞_, compute the expected number of steps to reach _p₁_ under the **current** Markov chain (which includes only _p₁, ..., p_{i-1}_ with finite scores).
   - Select the program _pᵢ_ with the **minimum** expected hitting time among all ∞-scored programs.
   - Set _S(pᵢ)_ = that minimum expected hitting time.
   - **Update the Markov chain**: Adding _pᵢ_ with a finite score changes the transition structure — other programs can now transition through _pᵢ_ as an intermediate state (since _pᵢ_ now has a finite score, transitions to _pᵢ_ become "accepting" transitions rather than self-loops).

3. **Terminate**: When all programs have finite scores.

This is directly analogous to Dijkstra: the "distance" is expected steps rather than path weight, and "relaxation" happens when adding a new intermediate node changes the expected hitting times of remaining nodes.

#### 2.3.3 Worked Example

Using the same _P = {p₁, p₂, p₃, p₄}_ with generation weights from Section 2.2.

**Step 0 — Initialize:**
- _S(p₁) = 0_, _S(p₂) = S(p₃) = S(p₄) = ∞_
- Initial transition matrix (only _p₁_ has finite score; transitions to ∞-scored programs become self-loops):

```
     p₁    p₂    p₃    p₄
p₁ [ 1     0     0     0  ]    ← absorbing
p₂ [ 0.75  0.25  0     0  ]    ← can reach p₁ (prob 0.75) or self-loop (0 + 0.25 + 0)
p₃ [ 0.25  0.25  0.50  0  ]    ← note: p₂,p₃,p₄ transitions become self-loops (0.25+0.25+0.25 stay → 0.75 raw, but only 0.25 goes to worse-or-equal)
p₄ [ 0     0     0     1  ]    ← can't reach any finite-scored program except... wait
```

Actually, the transition matrix is computed as follows: from _pᵢ_, the probability of transitioning to _pⱼ_ is _wᵢ[j]_ if _S(pⱼ) < S(pᵢ)_, and the probability of staying at _pᵢ_ is the sum of _wᵢ[k]_ for all _k_ where _S(pₖ) ≥ S(pᵢ)_.

At Step 0, only _S(p₁) = 0_ is finite. So:
- From _p₂_: prob of going to _p₁_ = _w₂[1]_ = 0.75. Prob of self-loop = 0 + 0.25 + 0 = 0.25. _(p₂, p₃, p₄ have ∞ scores)_
- From _p₃_: prob of going to _p₁_ = _w₃[1]_ = 0.25. Prob of self-loop = 0.25 + 0.25 + 0.25 = 0.75.
- From _p₄_: prob of going to _p₁_ = _w₄[1]_ = 0.00. Prob of self-loop = 0.58 + 0 + 0.42 = 1.0.

```
     p₁    p₂    p₃    p₄
p₁ [ 1     0     0     0  ]
p₂ [ 0.75  0.25  0     0  ]
p₃ [ 0.25  0     0.75  0  ]
p₄ [ 0     0     0     1  ]
```

**Step 1 — Compute expected steps to _p₁_ for each ∞-scored program:**

- From _p₂_: Let _E₂_ = expected steps. At each step, with prob 0.75 we reach _p₁_ (done in 1 step), with prob 0.25 we self-loop (try again). So _E₂_ = 0.75 · 1 + 0.25 · (_E₂_ + 1). Solving: _E₂_ = 1/0.75 = **4/3**.

- From _p₃_: _E₃_ = 0.25 · 1 + 0.75 · (_E₃_ + 1). Solving: _E₃_ = 1/0.25 = **4**.

- From _p₄_: _E₄_ = 0 · 1 + 1.0 · (_E₄_ + 1). This gives _0 = 1_, which is undefined — **_p₄_ cannot reach _p₁_ under the current chain**. _E₄_ = ∞.

Minimum is _E₂_ = 4/3, so set **_S(p₂) = 4/3_**. Add _p₂_ to the chain.

**Step 2 — Update the chain with _p₂_ (now _S(p₂) = 4/3_ is finite):**

Transition matrix updates — programs can now transition to _p₂_ (since it has a finite score lower than theirs):

```
     p₁    p₂    p₃    p₄
p₁ [ 1     0     0     0  ]
p₂ [ 0.75  0.25  0     0  ]
p₃ [ 0.25  0.25  0.50  0  ]    ← p₃ can now go to p₂ (w₃[2]=0.25)
p₄ [ 0     0.58  0     0.42]   ← p₄ can now go to p₂ (w₄[2]=0.58)
```

Recompute expected steps:
- From _p₃_: _E₃_ = 0.25·(0 + 1) + 0.25·(4/3 + 1) + 0.50·(_E₃_ + 1). Solving: _E₃_ · 0.50 = 0.25 + 0.25 · 7/3 + 0.50 = 0.25 + 7/12 + 0.50 = **8/3** / 1 → _E₃_ = **8/3**.

- From _p₄_: _E₄_ = 0.58·(4/3 + 1) + 0.42·(_E₄_ + 1). Solving: _E₄_ · 0.58 = 0.58 · 7/3 + 0.42 → _E₄_ ≈ **3.057**.

Minimum is _E₃_ = 8/3 ≈ 2.667, so set **_S(p₃) = 8/3_**. By similar procedure, _S(p₄)_ is computed last.

**Properties of the construction:**
1. Programs are added in **nondecreasing score order**: _S(p₁) = 0 ≤ S(p₂) = 4/3 ≤ S(p₃) = 8/3 ≤ S(p₄)_.
2. The score equals the expected steps to reach _p₁_ under the Markov chain defined by the score function itself — so the score is **self-consistent**.
3. The algorithm runs in **_O(n log n + m)_** time, analogous to Dijkstra with a priority queue, where _n_ = |P| and _m_ = total number of non-zero generation probabilities.

### 2.4 The Main Proof — Nondecreasing Scores (Full Detail)

This is the paper's central theorem. It guarantees the Dijkstra-like construction produces a valid, consistent score function.

#### 2.4.1 Theorem Statement

Let _pᵢ_ be the _i_-th program added to the Markov chain by the construction in Section 2.3. Then _S(p₁) ≤ S(p₂) ≤ ... ≤ S(pₙ)_ for all feasible _i_.

#### 2.4.2 Proof by Induction

**Base case (_i = 1_):** _S(p₁) = 0_ and _S(p₂) ≥ 0_ because _S(p₂)_ is an expected number of steps, which is non-negative. So _S(p₁) ≤ S(p₂)_. ✓

**Inductive hypothesis:** Assume _S(pⱼ) ≤ S(p_{j+1})_ holds for all _j < i_.

**Inductive step:** We need to show _S(pᵢ) ≤ S(p_{i+1})_.

**Step A — Define _E_ (the "old-chain" expected steps for _p_{i+1}_):**

Let _E_ be the expected number of steps from _p_{i+1}_ to reach _p₁_, under the Markov chain at step _i_ (i.e., **before** _p_{i+1}_ is assigned its score — only _p₁, ..., pᵢ_ have finite scores at this point, but we're computing _E_ before _pᵢ_ itself was added too... actually, _E_ is computed at step _i_ where _p₁,...,p_{i-1}_ have finite scores and _pᵢ$ was just selected).

Let me be precise: At step _i_, programs _p₁, ..., p_{i-1}_ have finite scores. The algorithm selects _pᵢ_ as the ∞-scored program with minimum expected steps. So _S(pᵢ) ≤ E_ where _E_ is the expected steps for any other ∞-scored program (including _p_{i+1}_) at step _i_.

Let _q_{i+1,k}_ denote the probability that _p_{i+1}_ generates _pₖ_.

_E_ satisfies the recurrence:

```
E = (1 - Σ_{k<i} q_{i+1,k}) · (E + 1) + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

**Interpretation:** From _p_{i+1}_ under the step-_i_ chain:
- With probability _Σ_{k<i} q_{i+1,k}_, it generates some _pₖ_ with _k < i_ (a program already in the chain with finite score). We transition to _pₖ_, which takes _S(pₖ)_ more steps to reach _p₁_, plus 1 for the current generation step. Total: _S(pₖ) + 1_.
- With probability _(1 - Σ_{k<i} q_{i+1,k})_, it generates a program with index _≥ i_ (score ∞ in the current chain). Since this program is no better, the current program doesn't update — we stay at _p_{i+1}_ and try again. Total: _E + 1_ (one wasted step, then we're back to the same situation).

**Step B — Solve for _E_:**

Expanding the recurrence:

```
E = E + 1 - (Σ_{k<i} q_{i+1,k}) · E - (Σ_{k<i} q_{i+1,k}) + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

Collecting _E_ terms:

```
E · (Σ_{k<i} q_{i+1,k}) = 1 - Σ_{k<i} q_{i+1,k} + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

Define:
- **_b = Σ_{k<i} q_{i+1,k}_** (total probability of generating an already-scored program)
- **_a = 1 - b + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)_** (the numerator)

Then:

```
E = a / b
```

**Step C — Establish _S(pᵢ) ≤ E_:**

By the greedy construction, at step _i_ the algorithm chose _pᵢ_ as the ∞-scored program with the **minimum** expected steps to _p₁_. Since _p_{i+1}_ was also ∞-scored at step _i_ (it hasn't been selected yet), its expected steps _E_ must be at least as large:

```
S(pᵢ) ≤ E  ⟹  S(pᵢ) ≤ a/b  ⟹  a ≥ S(pᵢ) · b
```

This inequality is the linchpin of the entire proof.

**Step D — Compute _S(p_{i+1})_ (the "new-chain" expected steps):**

At step _i+1_, program _pᵢ_ has been added to the chain with score _S(pᵢ)_. Now _p_{i+1}_ can transition not only to _p₁, ..., p_{i-1}_ but also to _pᵢ_. The updated recurrence is:

```
S(p_{i+1}) = (1 - Σ_{k<i} q_{i+1,k} - q_{i+1,i}) · (S(p_{i+1}) + 1)
           + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
           + q_{i+1,i} · (S(pᵢ) + 1)
```

The new term _q_{i+1,i} · (S(pᵢ) + 1)_ accounts for the probability of generating _pᵢ_ (which now has a finite score and can be transitioned to).

Solving (same algebra as Step B, with the extra term):

```
S(p_{i+1}) = (a + q_{i+1,i} · S(pᵢ)) / (b + q_{i+1,i})
```

where _a_ and _b_ are the same quantities defined in Step B.

**Step E — Prove the inequality _S(p_{i+1}) ≥ S(pᵢ)_:**

We need:

```
(a + q_{i+1,i} · S(pᵢ)) / (b + q_{i+1,i})  ≥  S(pᵢ)
```

Multiply both sides by _(b + q_{i+1,i})_ (positive):

```
a + q_{i+1,i} · S(pᵢ)  ≥  S(pᵢ) · b + S(pᵢ) · q_{i+1,i}
```

The _q_{i+1,i} · S(pᵢ)_ terms cancel:

```
a  ≥  S(pᵢ) · b
```

**This is exactly the inequality established in Step C.** ∎

#### 2.4.3 Geometric Intuition

The formula _S(p_{i+1}) = (a + q_{i+1,i} · S(pᵢ)) / (b + q_{i+1,i})_ is a **weighted average** between:
- _E = a/b_ (the old expected steps, without _pᵢ_ as an intermediate), weighted by _b_
- _S(pᵢ)_ (the expected steps from _pᵢ_), weighted by _q_{i+1,i}_

Since _E ≥ S(pᵢ)_ and this is a convex combination, the result lies between _S(pᵢ)_ and _E_:

```
S(pᵢ)  ≤  S(p_{i+1})  ≤  E
```

Adding _pᵢ_ as an intermediate can only **help** _p_{i+1}_ (by providing a new pathway to _p₁_), but never enough to make _p_{i+1}_ better than _pᵢ_ itself. This mirrors Dijkstra's algorithm: settling a new node can only reduce (or maintain) distances to unsettled nodes, and the settled node's distance is always ≤ the next node settled.

### 2.5 Key Results

1. **Existence**: For any finite _P_ with the Markov property, a consistent score function exists (proven constructively by the algorithm in Section 2.3).
2. **Computability**: The score can be computed in _O(n log n + m)_ time (_n_ = programs, _m_ = total non-zero transition probabilities), using a priority queue analogous to Dijkstra.
3. **Nondecreasing Scores**: Programs are added in nondecreasing score order — proven by induction in Section 2.4.
4. **Consistency**: The score function equals the expected number of steps to reach the optimal program under the process defined by that score function — follows directly from the nondecreasing property.
5. **Logarithmic Convergence (Empirical)**: Simulations with _n = 2^l_ for _l = 1, ..., 20_ show expected steps grow linearly with _l_ = _log₂(n)_. Linear regression of expected steps vs. _l_ yields R² = 0.983. Rank of the uniform-generating starting program vs. _n_ also shows R² = 1.0.
6. **Exponential Rank Improvement (Empirical)**: In simulations with _n = 2²⁰_, 100 runs show that program ranks improve exponentially (on a log scale) at each step before converging to the global optimum.

### 2.6 Accuracy Assessment

#### 2.6.1 What Is Mathematically Sound

- **The proof is correct.** The induction in Section 2.4, the recurrence equations, and the final algebraic step all check out. There are no errors in the mathematical reasoning.
- **The Dijkstra analogy is valid.** The greedy construction genuinely mirrors shortest-path computation. The nondecreasing property is the analog of Dijkstra's key invariant (settled distances never decrease). This is well-established algorithmic territory.
- **The Markov chain formulation is well-defined.** Given the Markov assumption, the framework is rigorous and the existence of a consistent score function is properly demonstrated via constructive proof.
- **The simulation methodology is reasonable.** The experimental setup (random subsets, weighted distributions, 10 repeats per configuration) is standard for this type of empirical study.

#### 2.6.2 Limitations and Weaknesses

1. **Circularity of the score function (acknowledged by the paper).** Computing _S_ requires knowing all transition probabilities _and_ the optimal program _p*_ in advance. The paper admits: "the score function is precomputed, which takes more time than enumerate every program to find the optimal." The logarithmic runtime of the RSI _procedure_ is real, but the setup cost is _O(n)_ or worse — so you've already done more work than brute-force search to set up the conditions for fast search.

2. **The Markov assumption is very restrictive.** Real self-improving systems learn from experience — their generation distributions should change based on what they've tried before. Dropping this assumption invalidates the entire framework. The paper acknowledges this as future work ("one may expand the model by embedding histories").

3. **Finite program space.** Real program spaces are countably infinite (or uncountable if parameterized). The proof relies fundamentally on finiteness to guarantee termination and well-defined expected hitting times. Extension to infinite spaces would require measure-theoretic machinery not present in the paper.

4. **Logarithmic convergence is empirical, not proven.** The _O(log n)_ result comes only from simulation (Figures 2-3), not from a theorem. The paper does not provide a theoretical bound on convergence speed. The R² = 0.983 is suggestive but not a proof.

5. **Simulation setup is narrow.** The first program generates uniformly, others generate over random subsets with random weights. This is a specific class of transition structures. The logarithmic scaling might not hold for adversarial or highly structured transition matrices.

6. **The consistency requirement is non-trivial in practice.** Wang constructs a consistent score function, but practical score functions (benchmarks, loss functions, test pass rates) almost certainly won't satisfy the consistency property. The paper does not address how robust the procedure is to inconsistent or noisy scores — it explicitly flags this as an open problem in Section 5.

#### 2.6.3 Overall Verdict

The math is **correct but the result is weaker than it appears at first glance**. Wang proves:

> _"If you already know the optimal program and all transition probabilities, you can construct a score function that makes the RSI procedure well-defined and the scores monotonically nondecreasing."_

This is a valid **existence proof** for a class of RSI systems. The practical gap is that the hard part of RSI — not knowing the optimal program or the transition structure — is assumed away. The paper is honest about this (Section 5), pointing to oracle score functions and Vingean reflection as open problems.

The key takeaway for applied work is the **structural insight**: RSI can be modeled as Markov chain optimization with a Dijkstra-like score construction, and the greedy "always accept improvements" strategy is provably sound under these assumptions. The logarithmic scaling, if it generalizes, means efficient RSI is at least _possible in principle_ — a non-trivial claim given that naive enumeration is linear in _|P|_.

---

## 3. Mapping Wang to Ouroboros

### 3.1 Structural Correspondence

| Wang's Formulation | Ouroboros Equivalent | Location |
|---|---|---|
| Program set _P_ | Candidate patches generated by the pipeline | `candidate_generator.py` |
| Current program _p_ | Current codebase state (HEAD of each repo) | `git log HEAD` |
| Score function _S(p)_ | **Gap: no unified score** — risk tiers + validation results + metrics exist independently | `risk_engine.py`, `self_evolution.py:416-512` |
| Generate _p'_ using _p_ | 10-phase pipeline: CLASSIFY -> ... -> GENERATE | `orchestrator.py:914-1078` |
| Replace _p_ by _p'_ if better | APPLY + VERIFY phases; graduation for ephemeral tools | `orchestrator.py:1715-1919`, `graduation_orchestrator.py:249-349` |
| Optimal program _p*_ | **No fixed optimum** — target shifts as requirements evolve | N/A (non-stationary) |
| Markov transitions | Phase transitions in the 10-phase FSM | `op_context.py` |
| Absorbing state | `COMPLETE` phase (terminal) | `op_context.py` |

### 3.2 Where Ouroboros Exceeds Wang's Model

1. **Memory**: Wang assumes memoryless generation. Ouroboros uses hierarchical memory (technique #8), runtime prompt adaptation (technique #1), and negative constraints (technique #3). This is strictly more powerful — the system learns from history.

2. **Multi-Repository**: Wang's formulation is single-program. Ouroboros operates across 3 repos (JARVIS, J-Prime, Reactor) with cross-repo Saga pattern for atomicity. This is a multi-space RSI system.

3. **Multi-Model Cascade**: Wang assumes a single generation mechanism. Ouroboros has a 3-tier cascade (Doubleword 397B -> Claude -> Local J-Prime) with cost-gated routing. Different "generators" have different probability distributions.

4. **Non-Stationary Target**: Wang's _p*_ is fixed. Ouroboros's "optimal" shifts as requirements change, bugs are discovered, and capabilities are added. This is a **restless bandit** setting.

5. **Structured Validation**: Wang's score is a single number. Ouroboros has a 5-layer validation pipeline (compile check, contract test, AST validation, security scan, pytest) providing multi-dimensional quality signals.

### 3.3 Where Ouroboros Falls Short of Wang's Model

1. **No consistent score function**: The quality signals are never composed into a single metric that satisfies Wang's consistency requirement.

2. **No convergence tracking**: The `MultiVersionEvolutionTracker` (technique #6) tracks success/failure counts but doesn't compute convergence rate or compare against logarithmic baselines.

3. **Static decision boundaries**: The graduation threshold (3 uses) and risk tiers (SAFE_AUTO / APPROVAL_REQUIRED / BLOCKED) are fixed rather than derived from probabilistic analysis.

4. **No pre-scoring**: Every candidate goes through full validation. There's no fast approximate quality gate analogous to Wang's score function check before replacement.

5. **No technique-level transition probabilities**: The 9 self-evolution techniques are applied without tracking which technique produces the best results for which domain.

---

## 4. Gap Analysis

### 4.1 Summary Table

| Gap | Severity | Wang's Solution | Ouroboros Extension | New Component |
|---|---|---|---|---|
| No unified score | **Critical** | Expected steps to optimal via DP | Composite score from 5 quality signals | `CompositeScoreFunction` |
| No convergence tracking | **High** | Logarithmic convergence theorem | Real-time convergence monitoring with plateau detection | `ConvergenceTracker` |
| Static graduation threshold | **High** | Score-as-expected-steps naturally adapts | Bayesian adaptive threshold from success probability | `AdaptiveGraduationThreshold` |
| No pre-scoring | **Medium** | Oracle score function (suggested) | TheOracle GraphRAG extended with quality estimation | `OraclePreScorer` |
| No technique probabilities | **Medium** | Markov transition matrix | Empirical P(success \| technique, domain, complexity) | `TransitionProbabilityTracker` |
| No self-reflection | **Low** | Vingean reflection (cited) | "Will this patch improve future patches?" check | `VindicationReflector` |

### 4.2 Dependency Graph

```
TransitionProbabilityTracker ──────────────┐
                                           │
CompositeScoreFunction ───┬── ConvergenceTracker
                          │
                          ├── AdaptiveGraduationThreshold
                          │
                          └── OraclePreScorer
                                           │
                          VindicationReflector (uses all above)
```

**Build order**: CompositeScoreFunction first (everything depends on it), then the rest can be built in parallel, with VindicationReflector last.

---

## 5. Improvement 1: Composite Score Function

### 5.1 Motivation

Wang proves that a **consistent** score function exists for any Markov RSI system. Consistency means: if _S(p) > S(p')_, then the expected number of steps from _p_ to optimal is genuinely greater than from _p'_.

Ouroboros currently has 5 independent quality signals in `self_evolution.py:416-512` (`CodeMetricsReport`):
- `line_count`, `function_count`, `avg_complexity`, `max_complexity`
- `has_docstrings`, `docstring_coverage`
- `import_count`, `lint_issues`

Plus external signals from validation:
- `validation_passed` (boolean from `orchestrator.py:1093-1344`)
- `test_coverage_delta` (from pytest)
- `blast_radius` (from `oracle.py` `compute_blast_radius()`)

These are never composed into a single comparable metric.

### 5.2 Design

#### 5.2.1 Score Components

The composite score _S_ is computed from 5 normalized sub-scores, each in [0.0, 1.0] where **lower is better** (consistent with Wang's convention where lower score = closer to optimal):

| Component | Symbol | Source | Normalization |
|---|---|---|---|
| Test delta | _s_test_ | pytest pass rate before/after | `1.0 - (pass_rate_after - pass_rate_before)` clamped to [0, 1] |
| Coverage delta | _s_cov_ | coverage % before/after | `1.0 - (coverage_after - coverage_before)` clamped to [0, 1] |
| Complexity delta | _s_cx_ | avg cyclomatic complexity delta | `sigmoid(complexity_after - complexity_before)` |
| Lint delta | _s_lint_ | lint issue count delta | `sigmoid(lint_after - lint_before)` |
| Blast radius | _s_br_ | TheOracle blast radius | `min(1.0, total_affected / 50)` |

#### 5.2.2 Composite Formula

```
S(patch) = w_test * s_test + w_cov * s_cov + w_cx * s_cx + w_lint * s_lint + w_br * s_br
```

Default weights (sum to 1.0):

| Weight | Value | Rationale |
|---|---|---|
| _w_test_ | 0.40 | Tests are the primary quality gate |
| _w_cov_ | 0.20 | Coverage prevents blind spots |
| _w_cx_ | 0.15 | Complexity correlates with future bug density |
| _w_lint_ | 0.10 | Style issues signal deeper problems |
| _w_br_ | 0.15 | Large blast radius = risky change |

#### 5.2.3 Consistency Property

For the score to be **Wang-consistent**, we need: if _S(patch_A) < S(patch_B)_, then patch_A is genuinely closer to a "good" codebase state. This holds because:
1. Each sub-score measures improvement (delta) not absolute value.
2. Weights are fixed (no model judgment in scoring).
3. Normalization is monotonic (sigmoid, linear clamp).

The score is **not** an expected number of steps (Wang's ideal), but it's a **consistent proxy** — monotonically related to code quality improvement.

#### 5.2.4 Data Structure

```python
@dataclass(frozen=True)
class CompositeScore:
    """Wang-consistent composite quality score for a patch/operation."""
    test_delta: float       # s_test component [0, 1]
    coverage_delta: float   # s_cov component [0, 1]
    complexity_delta: float # s_cx component [0, 1]
    lint_delta: float       # s_lint component [0, 1]
    blast_radius: float     # s_br component [0, 1]
    composite: float        # Weighted sum [0, 1], lower = better
    op_id: str              # Operation that produced this score
    timestamp: float        # When computed
```

#### 5.2.5 Integration Point

Computed at the end of the VERIFY phase (`orchestrator.py:1851-1919`), after `PatchBenchmarker` has run. Stored in the operation ledger as a new `SCORE_COMPUTED` state.

### 5.3 File Location

`backend/core/ouroboros/governance/composite_score.py` — new file, ~200 lines.

---

## 6. Improvement 2: Convergence Monitoring

### 6.1 Motivation

Wang's simulation results (Figure 2, Figure 3) show:
- Expected steps to optimal grow **linearly** with _log(n)_ (Figure 2a).
- Program ranks improve **exponentially** before convergence (Figure 3).

If Ouroboros is converging healthily, its composite scores should follow a similar pattern: **monotonically decreasing** composite scores (improving quality) with a **logarithmic** relationship between operations and improvement.

If the scores plateau, oscillate, or increase, the system is stuck — and should trigger Dynamic Re-Planning (technique #5).

### 6.2 Design

#### 6.2.1 Convergence States

```python
class ConvergenceState(str, Enum):
    IMPROVING = "improving"           # Scores decreasing, on track
    LOGARITHMIC = "logarithmic"       # Matching Wang's O(log n) prediction
    PLATEAUED = "plateaued"           # No improvement for N operations
    OSCILLATING = "oscillating"       # Scores bouncing up and down
    DEGRADING = "degrading"           # Scores increasing (getting worse)
    INSUFFICIENT_DATA = "insufficient_data"  # < 5 data points
```

#### 6.2.2 Detection Algorithm

Given the last _N_ composite scores _[S_1, S_2, ..., S_N]_ (chronological order):

1. **Trend**: Compute linear regression slope _m_ of scores over time.
   - _m < -epsilon_ -> IMPROVING
   - _|m| < epsilon_ -> PLATEAUED
   - _m > epsilon_ -> DEGRADING

2. **Logarithmic fit**: Fit _S = a * log(t) + b_ and compute R-squared.
   - R^2 > 0.8 AND _a < 0_ -> LOGARITHMIC (Wang's prediction confirmed)

3. **Oscillation**: Compute sign changes in consecutive differences.
   - If > 60% of differences alternate sign -> OSCILLATING

4. **Plateau**: If the standard deviation of the last _k_ scores < _delta_ -> PLATEAUED.

Parameters:
- _N_ = 20 (window size for analysis)
- _epsilon_ = 0.01 (slope threshold)
- _k_ = 5 (plateau window)
- _delta_ = 0.02 (plateau standard deviation threshold)

#### 6.2.3 Triggered Actions

| State | Action | Mechanism |
|---|---|---|
| IMPROVING | Continue current strategy | No intervention |
| LOGARITHMIC | Log validation of Wang's prediction | Ledger entry + narration |
| PLATEAUED | Trigger Dynamic Re-Planning (technique #5) | `DynamicRePlanner.suggest_replan()` |
| OSCILLATING | Increase negative constraints; narrow generation scope | `NegativeConstraintStore.add_constraint()` |
| DEGRADING | Emergency alert; pause autonomous operations | `EmergencyProtocolEngine` escalation |

#### 6.2.4 Data Structure

```python
@dataclass(frozen=True)
class ConvergenceReport:
    """Periodic convergence analysis of the Ouroboros pipeline."""
    state: ConvergenceState
    window_size: int              # How many scores analyzed
    slope: float                  # Linear regression slope
    r_squared_log: float          # R^2 of logarithmic fit
    oscillation_ratio: float      # Fraction of sign-alternating diffs
    plateau_stddev: float         # Stddev of last k scores
    scores_analyzed: int          # Total scores in history
    recommendation: str           # Human-readable action
    timestamp: float
```

#### 6.2.5 Integration Point

The `ConvergenceTracker` runs after every `COMPLETE` operation. It reads the last _N_ composite scores from the ledger, computes the `ConvergenceReport`, and stores it. If the state is PLATEAUED, OSCILLATING, or DEGRADING, it triggers the appropriate action.

Connected to `_publish_outcome()` in `orchestrator.py:1973-2090` — the existing outcome publishing method already calls into self-evolution components.

### 6.3 File Location

`backend/core/ouroboros/governance/convergence_tracker.py` — new file, ~250 lines.

---

## 7. Improvement 3: Adaptive Graduation Threshold

### 7.1 Motivation

The current graduation threshold is a fixed constant (`graduation_orchestrator.py:38`):
```python
_GRADUATION_THRESHOLD = 1 if _DEBUG_MUTATION else int(os.environ.get("JARVIS_GRADUATION_THRESHOLD", "3"))
```

Wang's score function is the **expected number of steps to optimal**. This naturally adapts: a program that's already close to optimal has a low score (few expected steps), while a distant program has a high score.

A fixed threshold of 3 treats all ephemeral tools equally:
- A tool with 100% success rate across 3 diverse contexts should graduate quickly.
- A tool with 60% success rate in 3 narrow contexts should need more evidence.

### 7.2 Design

#### 7.2.1 Bayesian Estimation

Model each ephemeral tool's success probability as a Beta distribution:
- Prior: `Beta(alpha=1, beta=1)` (uniform — no prior knowledge)
- After _s_ successes and _f_ failures: `Beta(alpha=1+s, beta=1+f)`

The **posterior mean** success probability is:
```
p_success = (1 + s) / (2 + s + f)
```

#### 7.2.2 Adaptive Threshold Formula

The graduation threshold adapts based on:

```
threshold = max(MIN_THRESHOLD, ceil(CONFIDENCE_USES / p_success))
```

Where:
- `MIN_THRESHOLD = 2` — never graduate on fewer than 2 uses (prevents lucky single-shot graduation)
- `CONFIDENCE_USES = 2.0` — base confidence factor
- `p_success` — posterior mean from Beta distribution

**Examples**:
| Successes | Failures | p_success | Threshold |
|---|---|---|---|
| 3 | 0 | 0.80 | ceil(2.0 / 0.80) = 3 |
| 2 | 0 | 0.75 | ceil(2.0 / 0.75) = 3 |
| 2 | 1 | 0.60 | ceil(2.0 / 0.60) = 4 |
| 3 | 2 | 0.57 | ceil(2.0 / 0.57) = 4 |
| 1 | 2 | 0.40 | ceil(2.0 / 0.40) = 5 |
| 5 | 0 | 0.86 | ceil(2.0 / 0.86) = 3 |

#### 7.2.3 Context Diversity Bonus

A tool used 3 times for the same goal provides less evidence than 3 times across diverse goals. Add a diversity factor:

```
unique_goals = number of distinct goal hashes
diversity = min(1.0, unique_goals / total_uses)
effective_p = p_success * (0.5 + 0.5 * diversity)
threshold = max(MIN_THRESHOLD, ceil(CONFIDENCE_USES / effective_p))
```

This means:
- 3 successes, all same goal (diversity=0.33) -> effective_p = 0.80 * 0.67 = 0.53 -> threshold = 4
- 3 successes, all different goals (diversity=1.0) -> effective_p = 0.80 * 1.0 = 0.80 -> threshold = 3

#### 7.2.4 Integration Point

Replace the static threshold check in `EphemeralUsageTracker.record_usage()` (`graduation_orchestrator.py:137-157`). Instead of comparing `success_count >= self._threshold`, compute the adaptive threshold from the Beta posterior and diversity factor.

The `_GRADUATION_THRESHOLD` env var becomes the `MIN_THRESHOLD` floor rather than the fixed count.

### 7.3 File Location

Logic added directly to `backend/core/ouroboros/governance/graduation_orchestrator.py` inside `EphemeralUsageTracker`, plus a small helper dataclass `AdaptiveThresholdResult` — no new file needed, ~60 lines of additions.

---

## 8. Improvement 4: Oracle Pre-Scoring

### 8.1 Motivation

Wang's future work section identifies the core practical limitation: the score function requires precomputing expected steps over **all** programs. He suggests an "oracle score function" that evaluates without processing all candidates.

TheOracle (`oracle.py`) already has:
- `compute_blast_radius()` — impact analysis (lines 892-988)
- `get_context_for_improvement()` — rich context including risk_level, dependencies, callers
- `query_relevant_nodes()` — semantic search with relevance scoring

Extending TheOracle to produce a **fast approximate quality score** for candidate patches — before full validation — would:
1. Skip obviously bad candidates (blast radius too high, touching critical files).
2. Prioritize promising candidates when multiple are generated.
3. Reduce wasted validation cycles.

### 8.2 Design

#### 8.2.1 Pre-Score Components

The pre-score is a fast heuristic (no model calls, no test execution):

| Signal | Source | Weight | Computation |
|---|---|---|---|
| Blast radius risk | `compute_blast_radius()` | 0.30 | risk_level mapping: low=0.0, medium=0.3, high=0.7, critical=1.0 |
| File coupling | `get_dependencies()` + `get_dependents()` | 0.25 | `min(1.0, (deps + dependents) / 20)` |
| Structural complexity | `CodeMetricsAnalyzer.analyze()` | 0.20 | `min(1.0, max_complexity / 30)` |
| Test coverage proximity | File neighborhood test counterparts | 0.15 | `0.0 if test exists else 1.0` |
| Change locality | Files in same module vs. cross-module | 0.10 | `1.0 - (same_module_files / total_files)` |

#### 8.2.2 Pre-Score Formula

```
PreScore(candidate) = sum(weight_i * signal_i)  for i in signals
```

Range: [0.0, 1.0], lower = more promising candidate.

#### 8.2.3 Gating Logic

- PreScore < 0.3 -> **FAST_TRACK**: Skip to full validation immediately.
- PreScore in [0.3, 0.7) -> **NORMAL**: Proceed through standard pipeline.
- PreScore >= 0.7 -> **WARN**: Log warning, still proceed (pre-score is heuristic, not authoritative).

The pre-score **never blocks** a candidate — it only prioritizes and warns. Full validation is always the authoritative gate.

#### 8.2.4 Integration Point

Called in the GENERATE phase (`orchestrator.py:914-1078`), after a candidate is produced but before entering VALIDATE. If multiple candidates are generated (retry loop), sort by pre-score and validate best-first.

### 8.3 File Location

`backend/core/ouroboros/governance/oracle_prescorer.py` — new file, ~150 lines.

---

## 9. Improvement 5: Transition Probability Tracking

### 9.1 Motivation

Wang's Markov chain formulation assigns each program a **fixed probabilistic distribution** over the next program. In Ouroboros, the "programs" are the 9 self-evolution techniques, and the "distribution" is the probability that a given technique produces a successful patch for a given domain.

Currently, technique selection is implicit — the orchestrator applies techniques in a fixed order (prompt adaptation, negative constraints, metrics feedback all injected in pre-GENERATE layers at `orchestrator.py:700-913`). There's no empirical tracking of which technique works best for which domain.

### 9.2 Design

#### 9.2.1 Transition Matrix

Track an empirical transition matrix:

```
P(success | technique, domain, complexity)
```

Where:
- **technique** in {prompt_adaptation, module_mutation, negative_constraints, metrics_feedback, dynamic_replanning, multi_version_evolution, generate_verify_refine, hierarchical_memory, auto_documentation}
- **domain** in {backend, frontend, vision, voice, governance, neural_mesh, infrastructure, ...} (extracted from target file paths)
- **complexity** in {trivial, light, heavy_code, complex} (from `BrainSelector.TaskComplexity`)

#### 9.2.2 Recording

After each `COMPLETE` or `FAILED` operation, record:
```python
@dataclass
class TechniqueOutcome:
    technique: str
    domain: str
    complexity: str
    success: bool
    composite_score: float  # From CompositeScoreFunction
    op_id: str
    timestamp: float
```

#### 9.2.3 Probability Estimation

Use Laplace-smoothed empirical frequencies:

```
P(success | tech, domain, complexity) = (1 + successes) / (2 + total)
```

With fallback hierarchy:
1. Full key: `(technique, domain, complexity)` — if >= 5 observations
2. Partial key: `(technique, domain)` — if >= 5 observations
3. Technique only: `(technique)` — always available after first use
4. Global prior: `0.5` — before any data

#### 9.2.4 Technique Routing

When the orchestrator enters the pre-GENERATE injection phase, it queries the tracker:
```python
best_techniques = tracker.rank_techniques(domain="governance", complexity="heavy_code")
# Returns: [("module_mutation", 0.82), ("metrics_feedback", 0.71), ("negative_constraints", 0.65), ...]
```

The top-ranked techniques are injected with higher priority (more context space allocated in the generation prompt).

#### 9.2.5 Integration Point

- **Recording**: In `_publish_outcome()` (`orchestrator.py:1973-2090`), which already records outcomes to self-evolution components.
- **Routing**: In the pre-GENERATE injection layers (`orchestrator.py:700-913`), where self-evolution techniques are currently applied.

### 9.3 File Location

`backend/core/ouroboros/governance/transition_tracker.py` — new file, ~200 lines.

---

## 10. Improvement 6: Vindication Reflection

### 10.1 Motivation

Wang cites Fallenstein & Soares (2015) on **Vingean reflection**: the ability of a self-improving system to reason about whether its modifications will actually improve it. This is the deepest theoretical question in RSI — can a system prove that its rewrites are beneficial before applying them?

Ouroboros has validation (does the patch pass tests?) but not **trajectory reflection** (will this patch make future patches better or worse?). A patch could pass all tests but:
- Increase code coupling, making future patches harder.
- Reduce blast radius predictability.
- Introduce patterns that confuse future generation models.

### 10.2 Design

#### 10.2.1 Reflection Signals

The vindication reflector computes **three forward-looking signals** using only deterministic analysis (no model calls):

1. **Coupling Trajectory** (will dependencies increase?):
   ```
   coupling_before = len(oracle.get_dependencies(target)) + len(oracle.get_dependents(target))
   coupling_after = estimated_coupling_from_patch(candidate)
   coupling_delta = (coupling_after - coupling_before) / max(1, coupling_before)
   ```
   Negative delta = good (reducing coupling). Positive = concerning.

2. **Blast Radius Trajectory** (will future changes be riskier?):
   ```
   br_before = oracle.compute_blast_radius(target).total_affected
   br_after = estimated_blast_radius_after_patch(candidate)
   br_delta = (br_after - br_before) / max(1, br_before)
   ```

3. **Entropy Trajectory** (is the code becoming more or less predictable?):
   ```
   complexity_before = metrics.avg_complexity
   complexity_after = estimated_complexity_after_patch(candidate)
   entropy_delta = (complexity_after - complexity_before) / max(1, complexity_before)
   ```

#### 10.2.2 Vindication Score

```
V(candidate) = -1.0 * (w1 * coupling_delta + w2 * br_delta + w3 * entropy_delta)
```

Weights: `w1 = 0.40, w2 = 0.35, w3 = 0.25`

Range: [-1.0, 1.0]
- V > 0: Patch improves future improvement capacity (vindicating).
- V < 0: Patch degrades future improvement capacity (concerning).
- V near 0: Neutral.

#### 10.2.3 Gating Logic

The vindication score does **not** block patches. It provides advisory information:

| V Score | Advisory | Action |
|---|---|---|
| V > 0.2 | "This patch improves the codebase's evolvability." | Log positive signal. |
| V in [-0.2, 0.2] | Neutral. | No action. |
| V in [-0.5, -0.2) | "Caution: this patch may make future improvements harder." | Narrate warning. |
| V < -0.5 | "Warning: this patch significantly increases coupling/complexity." | Narrate + emit telemetry. |

#### 10.2.4 Integration Point

Called in the GATE phase (`orchestrator.py:1545-1637`), after security review but before APPROVE. The vindication score is attached to the `OperationContext` and recorded in the ledger.

### 10.3 File Location

`backend/core/ouroboros/governance/vindication_reflector.py` — new file, ~180 lines.

---

## 11. Cross-Cutting Concerns

### 11.1 Persistence

All new components follow the existing persistence pattern in `self_evolution.py`:
- JSON files in `~/.jarvis/ouroboros/evolution/` (or component-specific subdirs).
- `_load()` in `__init__` -> reads JSON.
- `_persist()` after each mutation -> writes JSON.
- Silent fail on I/O errors (fail-open).

### 11.2 Ledger Integration

New ledger states added to `OperationState` enum in `ledger.py:43-72`:
- `SCORE_COMPUTED` — after composite score calculation.
- `CONVERGENCE_CHECKED` — after convergence analysis.
- `PRE_SCORED` — after oracle pre-scoring.
- `VINDICATION_CHECKED` — after vindication reflection.

### 11.3 Telemetry

All new components emit telemetry via the existing `TelemetryBus` pattern:
- Schema: `ouroboros.rsi_convergence@1.0.0`
- Source: component name (e.g., `composite_score`, `convergence_tracker`)
- Partition: `"rsi_convergence"`

### 11.4 Voice Narration

Convergence state changes and vindication warnings are narrated via `get_lifecycle_narrator().enqueue()`, following the pattern in `graduation_orchestrator.py:1039-1044`.

### 11.5 Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `OUROBOROS_RSI_SCORE_WEIGHTS` | `"0.40,0.20,0.15,0.10,0.15"` | Composite score weights (test,cov,cx,lint,br) |
| `OUROBOROS_CONVERGENCE_WINDOW` | `20` | Number of scores for convergence analysis |
| `OUROBOROS_CONVERGENCE_EPSILON` | `0.01` | Slope threshold for trend detection |
| `OUROBOROS_ADAPTIVE_GRAD_MIN` | `2` | Minimum graduation threshold |
| `OUROBOROS_ADAPTIVE_GRAD_CONFIDENCE` | `2.0` | Base confidence factor for adaptive threshold |
| `OUROBOROS_PRESCORER_ENABLED` | `true` | Enable/disable oracle pre-scoring |
| `OUROBOROS_VINDICATION_ENABLED` | `true` | Enable/disable vindication reflection |

### 11.6 Boundary Principle Compliance

Following the Symbiotic Boundary Principle:
- **Deterministic (100% of new code)**: All 6 components use deterministic computation only. No LLM calls. No heuristics that require model inference.
- **Agentic (0%)**: None of the new components invoke models. They process signals produced by existing agentic components (generation, validation) but the RSI convergence framework itself is purely mathematical.

This is deliberate: the convergence framework measures and governs the agentic components; it must not itself be agentic, or it would need its own governance — an infinite regress.

---

## 12. Mathematical Appendix

### 12.0 Summary of Wang's Proof Notation

Quick reference for the notation used in the full proof (Section 2.4):

| Symbol | Meaning |
|---|---|
| _P = {p₁, ..., pₙ}_ | Finite program space, ordered by score |
| _S(pᵢ)_ | Score of the _i_-th program added (= expected steps to _p₁_) |
| _q_{i+1,k}_ | Raw probability that _p_{i+1}_ generates _pₖ_ |
| _b = Σ_{k<i} q_{i+1,k}_ | Total probability of generating an already-scored program |
| _a = 1 - b + Σ_{k<i} q_{i+1,k}·(S(pₖ)+1)_ | Numerator of the expected-steps formula |
| _E = a/b_ | Expected steps from _p_{i+1}_ to _p₁_ at step _i_ (before _pᵢ_ is available) |
| _S(p_{i+1}) = (a + q_{i+1,i}·S(pᵢ)) / (b + q_{i+1,i})_ | Expected steps after _pᵢ_ becomes available |

**Key inequality chain:** _S(pᵢ) ≤ S(p_{i+1}) ≤ E_, which follows from _a ≥ S(pᵢ)·b_ (greedy selection guarantee).

### 12.1 Sigmoid Normalization

Used for complexity and lint deltas:
```
sigmoid(x) = 1 / (1 + exp(-x))
```

Properties:
- Maps (-inf, inf) to (0, 1)
- sigmoid(0) = 0.5 (no change = neutral score)
- Monotonically increasing (larger delta = worse score)

### 12.2 Beta Distribution for Adaptive Threshold

Prior: Beta(1, 1) = Uniform(0, 1)
Posterior after s successes, f failures: Beta(1+s, 1+f)
Mean: (1+s) / (2+s+f)
Variance: (1+s)(1+f) / ((2+s+f)^2 * (3+s+f))

The 95% credible interval narrows as evidence accumulates:
- After 3 successes, 0 failures: mean=0.80, 95% CI=[0.45, 0.97]
- After 10 successes, 0 failures: mean=0.92, 95% CI=[0.75, 0.99]
- After 10 successes, 2 failures: mean=0.79, 95% CI=[0.59, 0.93]

### 12.3 Wang's Convergence Results

**Proven (Section 2.4):** For a finite program space with the Markov property, the Dijkstra-like construction produces scores in nondecreasing order: _S(p₁) ≤ S(p₂) ≤ ... ≤ S(pₙ)_. This guarantees the score function is consistent.

**Empirical only (Section 2.5, items 5-6):** For randomly generated transition structures with _n = 2^l_ programs:
- Expected steps from the uniform-generating program to optimal: _O(log n)_, with R² = 0.983 linear fit to _l_.
- Rank improvement per step: exponential in expectation (Figure 3 of the paper).

**Applicability to Ouroboros:** Ouroboros violates the Markov assumption (it learns from history) and has no fixed optimal (the target shifts). However, the **shape** of convergence — logarithmic improvement with exponential rank jumps — serves as a health baseline for the `ConvergenceTracker` (Section 6). See Section 2.6 for a full accuracy assessment of Wang's results.

### 12.4 Linear Regression for Trend Detection

Given scores _[S_1, ..., S_N]_ at times _[t_1, ..., t_N]_:

```
slope m = (N * sum(t_i * S_i) - sum(t_i) * sum(S_i)) / (N * sum(t_i^2) - (sum(t_i))^2)
```

- m < -epsilon: improving (scores decreasing)
- |m| < epsilon: plateaued
- m > epsilon: degrading (scores increasing)

### 12.5 Logarithmic Fit

Fit _S = a * ln(t) + b_ using least squares on transformed data _(ln(t), S)_:
```
R^2 = 1 - SS_res / SS_tot
```

Where _SS_res_ = sum of squared residuals from the log fit, _SS_tot_ = total sum of squares around mean.

R^2 > 0.8 with _a < 0_ means scores are decreasing logarithmically — matching Wang's prediction.

---

## 13. References

1. **Wang, W.** (2018). _A Formulation of Recursive Self-Improvement and Its Possible Efficiency_. arXiv:1805.06610. University of British Columbia.

2. **Chalmers, D.J.** (2010). _The Singularity_. Science Fiction and Philosophy: From Time Travel to Superintelligence, pp. 171-224.

3. **Fallenstein, B. & Soares, N.** (2015). _Vingean Reflection: Reliable Reasoning for Self-Improving Agents_. Tech. rep., MIRI.

4. **Schmidhuber, J.** (2003). _Godel Machines: Self-Referential Universal Problem Solvers Making Provably Optimal Self-Improvements_. arXiv cs.LO/0309048.

5. **Steunebrink, B.R. & Schmidhuber, J.** (2012). _Towards an Actual Godel Machine Implementation_. Theoretical Foundations of AGI, pp. 173-195. Springer.

6. **Yampolskiy, R.V.** (2015). _From Seed AI to Technological Singularity via Recursively Self-Improving Software_. arXiv:1502.06512.

7. **Beer, S.** (1972). _Brain of the Firm_. Allen Lane, The Penguin Press. (Viable System Model — theoretical basis for the Symbiotic Boundary Principle.)

---

_Document version: 1.1.0_  
_Last updated: 2026-04-06_  
_Author: JARVIS Ouroboros Governance_  
_v1.1.0: Added full step-by-step mathematical proof of Wang's nondecreasing scores theorem, worked example, geometric intuition, and detailed accuracy assessment._
