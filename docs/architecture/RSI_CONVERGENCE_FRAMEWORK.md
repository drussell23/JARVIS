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

> **Reading guide.** This section is structured for study. Definitions are boxed in
> blockquotes. Theorems and proofs use labeled steps you can reference by number.
> Key equations are isolated on their own lines. Margin annotations (`←`) call
> out the intuition behind formal steps.

---

### 2.1 Core Definition

> **Definition 1 (RSI System).**
>
> Given:
> - A finite set of programs **P**
> - A score function **S : P → R**  (lower is better)
>
> Procedure:
> 1. Initialize *p* from **P** as the system's current program.
> 2. Repeat until stopping criterion is satisfied:
>    - Generate *p'* ∈ **P** using *p*.
>    - If *S(p') < S(p)*, replace *p* ← *p'*.

A total order over a finite set is isomorphic to a score function — programs can always be ranked. The convention throughout is:

> **Lower score = better program = closer to optimal.**

---

### 2.2 The Markov Chain Formulation

#### 2.2.1 The Independence Assumption

Wang introduces a simplifying assumption:

> **Markov Property.** The way a program generates a new program is
> *independent of the history*. Each program *p* defines a fixed
> probabilistic distribution over **P** for what it produces next.

This makes the RSI process a **homogeneous Markov chain**:

| Markov concept | RSI meaning |
|:---|:---|
| **States** | Programs in **P** |
| **Transition  *pᵢ → pⱼ*** | *pᵢ* generates *pⱼ* **and** *S(pⱼ) < S(pᵢ)* (accepted) |
| **Self-loop  *pᵢ → pᵢ*** | *pᵢ* generates *pⱼ* but *S(pⱼ) ≥ S(pᵢ)* (rejected — stay put) |
| **Absorbing state** | The optimal program *p** — once reached, never left |

#### 2.2.2 Transition Rule

From program *pᵢ* with generation weights *wᵢ* :

```
                 ⎧  wᵢ[j]                        if S(pⱼ) < S(pᵢ)      ← accept: improve
  T(pᵢ → pⱼ) =  ⎨
                 ⎩  0                              if S(pⱼ) ≥ S(pᵢ)     ← reject
```
```
  T(pᵢ → pᵢ) =  Σ  wᵢ[k]   for all k where S(pₖ) ≥ S(pᵢ)            ← self-loop: stay
```

In words: transitions to *worse-or-equal* programs are collapsed into a self-loop.

#### 2.2.3 Concrete Example

> **Setup.** Let **P** = {p₁, p₂, p₃, p₄} with S(pᵢ) = i.
> Each program's raw generation distribution (weight vector over **P**):

| Program | w₁ | w₂ | w₃ | w₄ | Description |
|:---|:---:|:---:|:---:|:---:|:---|
| **p₁** | 0.97 | 0.01 | 0.01 | 0.01 | Almost always regenerates itself |
| **p₂** | 0.75 | 0.00 | 0.25 | 0.00 | Generates p₁ or p₃ |
| **p₃** | 0.25 | 0.25 | 0.25 | 0.25 | Generates uniformly |
| **p₄** | 0.00 | 0.58 | 0.00 | 0.42 | Generates p₂ or itself |

**Trace starting from p₃ :**

```
  Step 1:   p₃  generates  p₄     S(p₄)=4 > S(p₃)=3     ✗ reject     stay at p₃
  Step 2:   p₃  generates  p₂     S(p₂)=2 < S(p₃)=3     ✓ accept     move to p₂
  Step 3:   p₂  generates  p₁     S(p₁)=1 < S(p₂)=2     ✓ accept     move to p₁
  Step 4:   p₁  is optimal        absorbing state         ■ done
```

The replacement rule transforms raw generation probabilities into the Markov chain's actual transition matrix — rejected transitions become self-loops.

---

### 2.3 Score Function Construction — The Dijkstra-Like Algorithm

Wang's central contribution: showing how to **construct** a consistent score function, defined as the expected number of steps to reach the optimal program.

#### 2.3.1 Consistency (Self-Referential Definition)

> **Definition (Consistency).** A score function *S* is **consistent** if
> for all *p, p' ∈ P* :
>
>     S(p) > S(p')   ⟹   E_steps(p → p*) > E_steps(p' → p*)
>
> where *E_steps* is computed under the RSI process that *S itself defines*.

This is self-referential: the score must agree with the process it induces. Wang's construction achieves this.

#### 2.3.2 The Algorithm

> **Algorithm: Score Function Construction**
>
> ```
> INPUT :  Programs P = {p₁, ..., pₙ}, generation weights {w₁, ..., wₙ}
> OUTPUT:  Consistent score function S
>
> 1.  INITIALIZE
>       Fix p₁ as the optimal program.
>       Set S(p₁) ← 0
>       Set S(pᵢ) ← ∞   for all i ≥ 2
>       Build initial Markov chain: only p₁ (absorbing state)
>
> 2.  ITERATE  for i = 2, 3, ..., n :
>       (a)  For every program p with S(p) = ∞ :
>              Compute E(p) = expected steps to reach p₁
>              under the CURRENT Markov chain
>              (only p₁, ..., p_{i-1} have finite scores)
>
>       (b)  Select pᵢ = argmin { E(p) : S(p) = ∞ }       ← greedy choice
>
>       (c)  Set S(pᵢ) ← E(pᵢ)
>
>       (d)  UPDATE the Markov chain:
>              pᵢ now has a finite score, so other programs
>              can transition THROUGH pᵢ (their self-loops
>              to pᵢ become accepting transitions)
>
> 3.  TERMINATE  when all programs have finite scores.
> ```

**Dijkstra parallel:**

| Dijkstra | This Algorithm |
|:---|:---|
| Graph nodes | Programs |
| Edge weights | Generation probabilities |
| Shortest path distance | Expected hitting time |
| Settling a node | Assigning a finite score |
| Relaxing neighbors | Updating transitions when new node settles |
| Priority queue | Select min expected steps among ∞-scored programs |
| Complexity: O(n log n + m) | Same: O(n log n + m) |

#### 2.3.3 Worked Example (Step by Step)

Using **P** = {p₁, p₂, p₃, p₄} with generation weights from Section 2.2.

---

**STEP 0 — Initialize**

```
  S(p₁) = 0          S(p₂) = ∞          S(p₃) = ∞          S(p₄) = ∞
```

Only p₁ has a finite score. The transition matrix at this stage:

```
       │  p₁     p₂     p₃     p₄
  ─────┼──────────────────────────────
   p₁  │  1      0      0      0        ← absorbing (optimal)
   p₂  │  0.75   0.25   0      0        ← w₂[1]=0.75 to p₁; rest self-loops
   p₃  │  0.25   0      0.75   0        ← w₃[1]=0.25 to p₁; rest self-loops
   p₄  │  0      0      0      1        ← w₄[1]=0.00 to p₁; stuck!
```

How each row is computed:
- **p₂ →** prob to p₁ = w₂[1] = 0.75 (accept: S(p₁) < S(p₂)). Self-loop = w₂[2]+w₂[3]+w₂[4] = 0+0.25+0 = 0.25.
- **p₃ →** prob to p₁ = w₃[1] = 0.25. Self-loop = w₃[2]+w₃[3]+w₃[4] = 0.25+0.25+0.25 = 0.75.
- **p₄ →** prob to p₁ = w₄[1] = 0.00. Self-loop = w₄[2]+w₄[3]+w₄[4] = 0.58+0+0.42 = 1.00.

---

**STEP 1 — Compute expected steps to p₁ for each ∞-scored program**

For **p₂** : Let E₂ = expected steps from p₂ to p₁.

```
  E₂  =  0.75 · 1  +  0.25 · (E₂ + 1)                   ← reach p₁ in 1 step, or self-loop and retry
  E₂  =  0.75  +  0.25·E₂  +  0.25
  E₂ − 0.25·E₂  =  1.00
  0.75·E₂  =  1.00

                   E₂  =  4/3  ≈  1.333
```

For **p₃** : Let E₃ = expected steps from p₃ to p₁.

```
  E₃  =  0.25 · 1  +  0.75 · (E₃ + 1)
  E₃  =  0.25  +  0.75·E₃  +  0.75
  0.25·E₃  =  1.00

                   E₃  =  4  
```

For **p₄** : Let E₄ = expected steps from p₄ to p₁.

```
  E₄  =  0 · 1  +  1.0 · (E₄ + 1)
  E₄  =  E₄ + 1
  0  =  1                                                 ← contradiction!

                   E₄  =  ∞                               ← p₄ cannot reach p₁ yet
```

Select the minimum: **E₂ = 4/3** is smallest.

```
  ┌──────────────────────────────────────┐
  │   Set   S(p₂)  =  4/3               │
  │   Add p₂ to the Markov chain.       │
  └──────────────────────────────────────┘
```

---

**STEP 2 — Update chain (p₂ now has finite score)**

Now that S(p₂) = 4/3, other programs with S > 4/3 can transition *to* p₂.

Updated transition matrix:

```
       │  p₁     p₂     p₃     p₄
  ─────┼──────────────────────────────
   p₁  │  1      0      0      0
   p₂  │  0.75   0.25   0      0        ← unchanged (p₂ already processed)
   p₃  │  0.25   0.25   0.50   0        ← NEW: w₃[2]=0.25 to p₂ now accepted
   p₄  │  0      0.58   0      0.42     ← NEW: w₄[2]=0.58 to p₂ now accepted
```

Recompute expected steps for remaining ∞-scored programs:

For **p₃** (can now reach p₁ directly *or* via p₂):

```
  E₃  =  0.25 · (S(p₁) + 1)                              ← generate p₁: 0 + 1 step
       +  0.25 · (S(p₂) + 1)                              ← generate p₂: 4/3 + 1 steps
       +  0.50 · (E₃ + 1)                                 ← self-loop: retry

  E₃  =  0.25·(1)  +  0.25·(7/3)  +  0.50·(E₃ + 1)
  E₃  =  0.25  +  7/12  +  0.50·E₃  +  0.50
  0.50·E₃  =  0.25  +  7/12  +  0.50
  0.50·E₃  =  3/12  +  7/12  +  6/12
  0.50·E₃  =  16/12  =  4/3

                   E₃  =  8/3  ≈  2.667
```

For **p₄** (can now reach p₂, and through p₂ reach p₁):

```
  E₄  =  0.58 · (S(p₂) + 1)  +  0.42 · (E₄ + 1)
  E₄  =  0.58 · (7/3)  +  0.42·E₄  +  0.42
  0.58·E₄  =  4.06/3  +  0.42
  0.58·E₄  =  1.353  +  0.42  =  1.773

                   E₄  ≈  3.057
```

Select the minimum: **E₃ = 8/3** is smallest.

```
  ┌──────────────────────────────────────┐
  │   Set   S(p₃)  =  8/3               │
  │   Add p₃ to the Markov chain.       │
  └──────────────────────────────────────┘
```

By the same procedure, S(p₄) is computed last.

---

**Summary of construction:**

```
  ┌──────────────────────────────────────────────────────────────┐
  │                                                              │
  │   S(p₁)  =  0       ≤       ← optimal (by definition)       │
  │   S(p₂)  =  4/3     ≤       ← 1.33 expected steps           │
  │   S(p₃)  =  8/3     ≤       ← 2.67 expected steps           │
  │   S(p₄)  ≈  3.06            ← 3.06 expected steps           │
  │                                                              │
  │   Scores are nondecreasing.  ✓                               │
  │   Scores equal expected steps to p₁ under the induced        │
  │   Markov chain.  ✓  (self-consistent)                        │
  │                                                              │
  │   Complexity:  O(n log n + m)                                │
  │     n = |P| = 4,   m = nonzero generation probabilities      │
  │                                                              │
  └──────────────────────────────────────────────────────────────┘
```

---

### 2.4 The Main Proof — Nondecreasing Scores

This is the paper's central theorem. It guarantees the Dijkstra-like construction always produces a valid, consistent score function.

---

> **Theorem (Nondecreasing Scores).**
> Let *pᵢ* be the *i*-th program added to the Markov chain
> by the construction in Section 2.3. Then:
>
>     S(p₁)  ≤  S(p₂)  ≤  ...  ≤  S(pₙ)

---

#### 2.4.1 Proof by Induction

##### Base Case  ( i = 1 )

```
  S(p₁) = 0      and      S(p₂) ≥ 0                      ← S(p₂) is an expected number of steps
                                                              which is always non-negative
  ∴   S(p₁) ≤ S(p₂)   ✓
```

##### Inductive Hypothesis

Assume S(pⱼ) ≤ S(pⱼ₊₁) holds for all j < i.

##### Inductive Step

**Goal:** Show S(pᵢ) ≤ S(pᵢ₊₁).

We proceed in five clearly labeled steps.

---

**STEP A.  Define the key quantities.**

Let *E* = expected number of steps from pᵢ₊₁ to reach p₁, computed under the Markov chain **at step *i***, where programs p₁, ..., pᵢ₋₁ have finite scores.

> Note: At step *i*, the algorithm has just selected pᵢ as the ∞-scored
> program with the *minimum* expected steps. So for any other ∞-scored
> program (including pᵢ₊₁), its expected steps *E* satisfies **S(pᵢ) ≤ E**.

Let *q*ᵢ₊₁,ₖ = probability that pᵢ₊₁ generates pₖ  (the raw generation weight).

---

**STEP B.  Write the recurrence for *E*.**

From pᵢ₊₁ under the step-*i* chain, two things can happen each round:

```
  ┌─────────────────────────────────────────────────────────────────────┐
  │  With prob  Σ_{k<i} q_{i+1,k}  :                                  │
  │    pᵢ₊₁ generates some pₖ with k < i                              │
  │    (already in the chain, finite score)                            │
  │    ⟹  transition to pₖ, then S(pₖ) more steps to p₁              │
  │    ⟹  cost = S(pₖ) + 1                                            │
  │                                                                     │
  │  With prob  1 − Σ_{k<i} q_{i+1,k}  :                              │
  │    pᵢ₊₁ generates a program with index ≥ i                        │
  │    (score ∞, rejected — no improvement)                            │
  │    ⟹  stay at pᵢ₊₁, try again                                     │
  │    ⟹  cost = E + 1                                                 │
  └─────────────────────────────────────────────────────────────────────┘
```

This gives the recurrence:

```
  E  =  (1 − Σ_{k<i} q_{i+1,k}) · (E + 1)  +  Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

---

**STEP C.  Solve for *E*.**

Expand:

```
  E  =  E + 1
       − (Σ_{k<i} q_{i+1,k}) · E
       − (Σ_{k<i} q_{i+1,k})
       + Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

Move all *E* terms to the left:

```
  E · (Σ_{k<i} q_{i+1,k})  =  1  −  Σ_{k<i} q_{i+1,k}  +  Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)
```

Define two shorthand variables:

```
  ┌───────────────────────────────────────────────────────────────────┐
  │                                                                   │
  │   b  =  Σ_{k<i}  q_{i+1,k}                                      │
  │          ↑                                                        │
  │          total probability of generating                          │
  │          an already-scored program                                │
  │                                                                   │
  │   a  =  1 − b  +  Σ_{k<i}  q_{i+1,k} · (S(pₖ) + 1)            │
  │          ↑                                                        │
  │          numerator of the expected-steps formula                  │
  │                                                                   │
  └───────────────────────────────────────────────────────────────────┘
```

Therefore:

```
                         a
                 E   =  ───
                         b
```

---

**STEP D.  Establish the key inequality:  a ≥ S(pᵢ) · b**

By the greedy construction, pᵢ was chosen at step *i* as the ∞-scored program with the **minimum** expected steps. Since pᵢ₊₁ was also ∞-scored at step *i*, its expected steps *E* must be at least as large:

```
                     S(pᵢ)  ≤  E

                              a
               ⟹    S(pᵢ)  ≤  ─
                              b

               ⟹    a  ≥  S(pᵢ) · b                   ★ KEY INEQUALITY
```

> This inequality is the **linchpin** of the entire proof. Everything
> that follows is just showing that it implies the desired result.

---

**STEP E.  Compute S(pᵢ₊₁) under the updated chain.**

At step *i+1*, program pᵢ has been added to the chain with score S(pᵢ). Now pᵢ₊₁ can transition not only to p₁, ..., pᵢ₋₁ but **also to pᵢ**. The updated recurrence:

```
  S(pᵢ₊₁)  =  (1 − Σ_{k<i} q_{i+1,k} − q_{i+1,i}) · (S(pᵢ₊₁) + 1)      ← self-loop
             +  Σ_{k<i} q_{i+1,k} · (S(pₖ) + 1)                            ← to p₁...pᵢ₋₁
             +  q_{i+1,i} · (S(pᵢ) + 1)                                     ← NEW: to pᵢ
```

Solving (same algebra as Step C, with the extra *q*ᵢ₊₁,ᵢ term):

```
                    a  +  q_{i+1,i} · S(pᵢ)
  S(pᵢ₊₁)   =    ──────────────────────────
                    b  +  q_{i+1,i}
```

where *a* and *b* are the same quantities from Step C.

> **Observation.** This is a **weighted average** of *E* = a/b and *S(pᵢ)*,
> with weights *b* and *q*ᵢ₊₁,ᵢ respectively.

---

**STEP F.  Prove S(pᵢ₊₁) ≥ S(pᵢ).**

We need to show:

```
     a + q_{i+1,i} · S(pᵢ)
    ────────────────────────   ≥   S(pᵢ)
     b + q_{i+1,i}
```

Multiply both sides by (*b* + *q*ᵢ₊₁,ᵢ), which is positive:

```
     a + q_{i+1,i} · S(pᵢ)   ≥   S(pᵢ) · b  +  S(pᵢ) · q_{i+1,i}
```

The *q*ᵢ₊₁,ᵢ · S(pᵢ) terms appear on both sides — cancel them:

```
     a   ≥   S(pᵢ) · b
```

**This is exactly the ★ KEY INEQUALITY from Step D.**

```
  ┌─────────────────────────────────────────────────┐
  │                                                  │
  │   ∴   S(pᵢ)  ≤  S(pᵢ₊₁)                       │
  │                                                  │
  │   By induction, S(p₁) ≤ S(p₂) ≤ ... ≤ S(pₙ)   │
  │                                                  │
  │                                         ∎  QED   │
  └─────────────────────────────────────────────────┘
```

---

#### 2.4.2 Geometric Intuition

The closed-form for S(pᵢ₊₁) reveals a clean geometric picture:

```
                    a  +  q_{i+1,i} · S(pᵢ)
  S(pᵢ₊₁)   =    ──────────────────────────        (weighted average)
                    b  +  q_{i+1,i}
```

This is a **convex combination** of two values:

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │   S(pᵢ)       ◄──────── S(pᵢ₊₁) ────────►        E = a/b      │
  │   (lower)       sits somewhere here               (higher)      │
  │                                                                  │
  │   Weight:        q_{i+1,i}                         b             │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

Since E ≥ S(pᵢ) and this is a convex combination:

```
                S(pᵢ)   ≤   S(pᵢ₊₁)   ≤   E
```

**In plain English:** Adding pᵢ as an intermediate node can only *help* pᵢ₊₁ (by opening a new pathway to p₁), but it can never help so much that pᵢ₊₁ becomes better than pᵢ itself.

This mirrors Dijkstra: settling a new node can reduce distances to unsettled nodes, but the settled node's distance is always ≤ any node settled after it.

---

### 2.5 Key Results

#### Proven Results

| # | Result | Proof |
|:---:|:---|:---|
| 1 | **Existence** — For any finite **P** with the Markov property, a consistent score function exists. | Constructive: the algorithm in Section 2.3 produces one. |
| 2 | **Computability** — The score can be computed in **O(n log n + m)** time. | Dijkstra with priority queue; *n* = \|**P**\|, *m* = nonzero transition probabilities. |
| 3 | **Nondecreasing Scores** — Programs are added in nondecreasing score order. | Induction proof in Section 2.4. |
| 4 | **Consistency** — The score equals expected steps to optimal under the process it defines. | Follows from nondecreasing property + construction. |

#### Empirical Results (Simulation Only — Not Proven)

| # | Result | Evidence |
|:---:|:---|:---|
| 5 | **Logarithmic Convergence** — Expected steps to optimal grow as O(log n). | Simulations with n = 2^l for l = 1,...,20. Linear regression of steps vs. l: R² = 0.983. |
| 6 | **Exponential Rank Improvement** — Ranks improve exponentially per step before convergence. | 100 runs with n = 2²⁰. Log-scale rank drops linearly with step count (Figure 3). |

---

### 2.6 Accuracy Assessment

#### 2.6.1 What Is Mathematically Sound

| Claim | Verdict | Notes |
|:---|:---:|:---|
| Induction proof (Section 2.4) | **Correct** | All recurrence equations, algebraic manipulations, and the final cancellation step check out. |
| Dijkstra analogy | **Valid** | Nondecreasing property is the exact analog of Dijkstra's invariant. Well-established algorithmic territory. |
| Markov chain formulation | **Well-defined** | Given the Markov assumption, the framework is rigorous. Existence is properly demonstrated via constructive proof. |
| Simulation methodology | **Reasonable** | Standard setup: random subsets, weighted distributions, 10 repeats per config. |

#### 2.6.2 Limitations and Weaknesses

> **1. Circularity (the paper acknowledges this).**
>
> Computing S requires knowing all transition probabilities *and* the
> optimal program p* in advance. The paper admits: *"the score function
> is precomputed, which takes more time than enumerate every program to
> find the optimal."* The logarithmic runtime of the RSI procedure is
> real, but the setup cost is O(n) or worse — you've already done more
> work than brute-force search.

> **2. The Markov assumption is very restrictive.**
>
> Real self-improving systems learn from experience — their generation
> distributions change based on what they've tried before. Dropping this
> assumption invalidates the entire framework. The paper acknowledges
> this as future work.

> **3. Finite program space.**
>
> Real program spaces are countably infinite (or uncountable if
> parameterized). The proof relies fundamentally on finiteness to
> guarantee termination and well-defined expected hitting times.
> Extension to infinite spaces would require measure-theoretic machinery
> not present in the paper.

> **4. Logarithmic convergence is empirical, not proven.**
>
> The O(log n) result comes only from simulation, not from a theorem.
> R² = 0.983 is suggestive but not a proof. The paper does not provide
> a theoretical convergence bound.

> **5. Narrow simulation setup.**
>
> The first program generates uniformly; others generate over random
> subsets with random weights. The logarithmic scaling might not hold
> for adversarial or highly structured transition matrices.

> **6. Consistency is non-trivial in practice.**
>
> Wang constructs a consistent score function, but practical score
> functions (benchmarks, test pass rates, loss functions) almost
> certainly won't satisfy consistency. The paper flags robustness
> to inconsistent or noisy scores as an open problem.

#### 2.6.3 Overall Verdict

```
  ┌──────────────────────────────────────────────────────────────────┐
  │                                                                  │
  │  THE MATH IS CORRECT.                                           │
  │  THE RESULT IS WEAKER THAN IT APPEARS.                          │
  │                                                                  │
  │  Wang proves:                                                    │
  │  "If you already know the optimal program and all transition     │
  │   probabilities, you can construct a score function that makes   │
  │   the RSI procedure well-defined and scores nondecreasing."     │
  │                                                                  │
  │  This is a valid EXISTENCE PROOF — not a practical algorithm.   │
  │  The hard part of RSI (not knowing the optimal or the           │
  │  transition structure) is assumed away.                          │
  │                                                                  │
  │  KEY TAKEAWAY FOR APPLIED WORK:                                 │
  │  RSI can be modeled as Markov chain optimization with a         │
  │  Dijkstra-like score construction. The greedy "always accept    │
  │  improvements" strategy is provably sound. If the logarithmic   │
  │  scaling generalizes, efficient RSI is possible in principle    │
  │  — a non-trivial claim given naive enumeration is O(|P|).      │
  │                                                                  │
  └──────────────────────────────────────────────────────────────────┘
```

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

> Quick reference for the notation used in the full proof (Section 2.4).
> See Section 2.4 Steps A-F for derivations.

```
  SYMBOL                                      MEANING
  ──────                                      ───────
  P = {p₁, ..., pₙ}                          Finite program space, ordered by score

  S(pᵢ)                                      Score of the i-th program added
                                              (= expected steps to reach p₁)

  q_{i+1,k}                                  Raw probability that pᵢ₊₁ generates pₖ

  b = Σ_{k<i} q_{i+1,k}                      Total prob of generating an already-scored program

  a = 1 − b + Σ_{k<i} q_{i+1,k}·(S(pₖ)+1)  Numerator of the expected-steps formula

  E = a / b                                   Expected steps from pᵢ₊₁ to p₁
                                              at step i (BEFORE pᵢ is available)

  S(pᵢ₊₁) = (a + q_{i+1,i}·S(pᵢ))          Expected steps from pᵢ₊₁ to p₁
           / (b + q_{i+1,i})                  at step i+1 (AFTER pᵢ is available)


  KEY INEQUALITY CHAIN:

        S(pᵢ)   ≤   S(pᵢ₊₁)   ≤   E

  Follows from:   a ≥ S(pᵢ)·b    (greedy selection guarantee, Step D)
```

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
