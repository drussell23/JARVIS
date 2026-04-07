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

> **Reading guide.** Definitions and theorems are boxed in blockquotes.
> All equations use LaTeX math rendering. Proof steps are labeled for
> cross-reference. Intuition callouts are indented beneath each formal step.

---

### 2.1 Core Definition

> **Definition 1 (RSI System).**
>
> **Given:**
> - A finite set of programs $P$
> - A score function $S : P \to \mathbb{R}$ &emsp; *(lower is better)*
>
> **Procedure:**
> 1. Initialize $p \in P$ as the system's current program.
> 2. Repeat until stopping criterion is satisfied:
>    - Generate $p' \in P$ using $p$.
>    - If $S(p') < S(p)$, replace $p \leftarrow p'$.

A total order over a finite set is isomorphic to a score function — programs can always be ranked.

> **Convention.** &ensp; Lower score = better program = closer to optimal.

---

### 2.2 The Markov Chain Formulation

#### 2.2.1 The Independence Assumption

> **Markov Property.** The way a program generates a new program is
> *independent of the history*. Each program $p$ defines a fixed
> probability distribution over $P$ for what it produces next.

This makes the RSI process a **homogeneous Markov chain**:

| Markov concept | RSI meaning |
|:---|:---|
| **States** | Programs in $P$ |
| **Transition** $p_i \to p_j$ | $p_i$ generates $p_j$ **and** $S(p_j) < S(p_i)$ &ensp; *(accepted)* |
| **Self-loop** $p_i \to p_i$ | $p_i$ generates $p_j$ but $S(p_j) \geq S(p_i)$ &ensp; *(rejected)* |
| **Absorbing state** | The optimal program $p^*$ — once reached, never left |

#### 2.2.2 Transition Rule

From program $p_i$ with generation weights $w_i$ :

$$
T(p_i \to p_j) = \begin{cases} w_i[j] & \text{if } S(p_j) < S(p_i) \quad \textit{(accept: improve)} \\[6pt] 0 & \text{if } S(p_j) \geq S(p_i) \quad \textit{(reject)} \end{cases}
$$

The self-loop absorbs all rejected mass:

$$
T(p_i \to p_i) = \sum_{\{k \;:\; S(p_k) \,\geq\, S(p_i)\}} w_i[k]
$$

In words: transitions to *worse-or-equal* programs are collapsed into a self-loop.

#### 2.2.3 Concrete Example

> **Setup.** &ensp; $P = \{p_1,\, p_2,\, p_3,\, p_4\}$ &ensp; with &ensp; $S(p_i) = i$.
>
> Each program's raw generation distribution:

| Program | $w[\,p_1\,]$ | $w[\,p_2\,]$ | $w[\,p_3\,]$ | $w[\,p_4\,]$ | Description |
|:---:|:---:|:---:|:---:|:---:|:---|
| $p_1$ | 0.97 | 0.01 | 0.01 | 0.01 | Almost always regenerates itself |
| $p_2$ | 0.75 | 0.00 | 0.25 | 0.00 | Generates $p_1$ or $p_3$ |
| $p_3$ | 0.25 | 0.25 | 0.25 | 0.25 | Generates uniformly |
| $p_4$ | 0.00 | 0.58 | 0.00 | 0.42 | Generates $p_2$ or itself |

&nbsp;

**Trace starting from $p_3$ :**

| Step | Current | Generates | Compare | Decision | Result |
|:---:|:---:|:---:|:---|:---:|:---|
| 1 | $p_3$ | $p_4$ | $S(p_4)=4 > S(p_3)=3$ | reject | stay at $p_3$ |
| 2 | $p_3$ | $p_2$ | $S(p_2)=2 < S(p_3)=3$ | accept | move to $p_2$ |
| 3 | $p_2$ | $p_1$ | $S(p_1)=1 < S(p_2)=2$ | accept | move to $p_1$ |
| 4 | $p_1$ | — | absorbing state | done | — |

The replacement rule transforms raw generation probabilities into the actual transition matrix — rejected transitions become self-loops.

---

### 2.3 Score Function Construction — The Dijkstra-Like Algorithm

Wang's central contribution: showing how to **construct** a consistent score function, defined as the expected number of steps to reach the optimal program.

#### 2.3.1 Consistency (Self-Referential Definition)

> **Definition (Consistency).** A score function $S$ is **consistent** if for all $p, p' \in P$ :
>
> $$S(p) > S(p') \;\;\Longrightarrow\;\; \mathbb{E}[\text{steps}(p \to p^*)] \;>\; \mathbb{E}[\text{steps}(p' \to p^*)]$$
>
> where $\mathbb{E}[\text{steps}]$ is computed under the RSI process that $S$ *itself* defines.

This is self-referential: the score must agree with the process it induces.

#### 2.3.2 The Algorithm

> **Algorithm: Score Function Construction**
>
> **Input:** &ensp; Programs $P = \{p_1, \ldots, p_n\}$, &ensp; generation weights $\{w_1, \ldots, w_n\}$
>
> **Output:** &ensp; Consistent score function $S$
>
> ---
>
> **1. &ensp; Initialize.**
> - Fix $p_1$ as the optimal program
> - $S(p_1) \leftarrow 0$
> - $S(p_i) \leftarrow \infty$ &ensp; for all $i \geq 2$
> - Build initial Markov chain with $p_1$ as the sole absorbing state
>
> **2. &ensp; Iterate** &ensp; for $i = 2, 3, \ldots, n$ :
>
> &emsp; **(a)** &ensp; For every program $p$ with $S(p) = \infty$ : compute $\mathbb{E}(p) =$ expected steps to reach $p_1$ under the current Markov chain (only $p_1, \ldots, p_{i-1}$ have finite scores)
>
> &emsp; **(b)** &ensp; $p_i \leftarrow \arg\min\{\,\mathbb{E}(p) : S(p) = \infty\,\}$ &emsp; *(greedy choice)*
>
> &emsp; **(c)** &ensp; $S(p_i) \leftarrow \mathbb{E}(p_i)$
>
> &emsp; **(d)** &ensp; Update the Markov chain: $p_i$ now has a finite score, so other programs can transition *through* $p_i$
>
> **3. &ensp; Terminate** &ensp; when all programs have finite scores.

&nbsp;

**Dijkstra parallel:**

| Dijkstra | This Algorithm |
|:---|:---|
| Graph nodes | Programs in $P$ |
| Edge weights | Generation probabilities $w_i[j]$ |
| Shortest path distance | Expected hitting time $\mathbb{E}[\text{steps}]$ |
| Settling a node | Assigning a finite score $S(p_i) < \infty$ |
| Relaxing neighbors | Updating transitions when new node settles |
| Priority queue extract-min | $\arg\min\{\,\mathbb{E}(p) : S(p) = \infty\,\}$ |
| Complexity $O(n \log n + m)$ | Same: $O(n \log n + m)$ |

#### 2.3.3 Worked Example (Step by Step)

Using $P = \{p_1, p_2, p_3, p_4\}$ with generation weights from Section 2.2.

---

##### Step 0 — Initialize

$$S(p_1) = 0, \qquad S(p_2) = \infty, \qquad S(p_3) = \infty, \qquad S(p_4) = \infty$$

Only $p_1$ has a finite score. Transition matrix $T^{(0)}$:

|  | $p_1$ | $p_2$ | $p_3$ | $p_4$ | Note |
|:---:|:---:|:---:|:---:|:---:|:---|
| $p_1$ | 1 | 0 | 0 | 0 | absorbing |
| $p_2$ | 0.75 | 0.25 | 0 | 0 | $w_2[1]=0.75$ accepted; rest self-loop |
| $p_3$ | 0.25 | 0 | 0.75 | 0 | $w_3[1]=0.25$ accepted; rest self-loop |
| $p_4$ | 0 | 0 | 0 | 1 | $w_4[1]=0$; completely stuck |

How each row is computed:

- **$p_2$** : &ensp; $T(p_2 \to p_1) = w_2[1] = 0.75$ &ensp; | &ensp; self-loop $= w_2[2]+w_2[3]+w_2[4] = 0.25$
- **$p_3$** : &ensp; $T(p_3 \to p_1) = w_3[1] = 0.25$ &ensp; | &ensp; self-loop $= 0.25+0.25+0.25 = 0.75$
- **$p_4$** : &ensp; $T(p_4 \to p_1) = w_4[1] = 0.00$ &ensp; | &ensp; self-loop $= 0.58+0+0.42 = 1.00$

---

##### Step 1 — Compute expected steps to $p_1$

**For $p_2$** — let $E_2$ = expected steps:

$$E_2 = 0.75 \cdot 1 \;+\; 0.25 \cdot (E_2 + 1)$$

> *Reach $p_1$ in 1 step with prob 0.75, or self-loop and retry with prob 0.25.*

$$E_2 = 0.75 + 0.25\,E_2 + 0.25$$

$$E_2 - 0.25\,E_2 = 1.00$$

$$0.75\,E_2 = 1.00$$

$$\boxed{\;E_2 = \frac{4}{3} \approx 1.333\;}$$

&nbsp;

**For $p_3$** — let $E_3$ = expected steps:

$$E_3 = 0.25 \cdot 1 \;+\; 0.75 \cdot (E_3 + 1)$$

$$0.25\,E_3 = 1.00$$

$$\boxed{\;E_3 = 4\;}$$

&nbsp;

**For $p_4$** — let $E_4$ = expected steps:

$$E_4 = 0 \cdot 1 \;+\; 1.0 \cdot (E_4 + 1) \;\;\Longrightarrow\;\; 0 = 1 \quad \text{(contradiction)}$$

$$\boxed{\;E_4 = \infty \quad (p_4 \text{ cannot reach } p_1 \text{ yet})\;}$$

&nbsp;

Select the minimum: $E_2 = \tfrac{4}{3}$ is smallest.

> **Result.** &ensp; $S(p_2) = \dfrac{4}{3}$. &ensp; Add $p_2$ to the Markov chain.

---

##### Step 2 — Update chain ($p_2$ now has finite score)

Since $S(p_2) = \tfrac{4}{3}$ is now finite, other programs with higher scores can transition *to* $p_2$.

Updated transition matrix $T^{(1)}$:

|  | $p_1$ | $p_2$ | $p_3$ | $p_4$ | Note |
|:---:|:---:|:---:|:---:|:---:|:---|
| $p_1$ | 1 | 0 | 0 | 0 | |
| $p_2$ | 0.75 | 0.25 | 0 | 0 | unchanged |
| $p_3$ | 0.25 | **0.25** | 0.50 | 0 | **new:** $w_3[2]=0.25$ to $p_2$ now accepted |
| $p_4$ | 0 | **0.58** | 0 | 0.42 | **new:** $w_4[2]=0.58$ to $p_2$ now accepted |

&nbsp;

**Recompute $E_3$** &ensp; ($p_3$ can now reach $p_1$ directly *or* via $p_2$):

$$E_3 = \underbrace{0.25 \cdot \bigl(S(p_1) + 1\bigr)}_{\text{generate } p_1} \;+\; \underbrace{0.25 \cdot \bigl(S(p_2) + 1\bigr)}_{\text{generate } p_2} \;+\; \underbrace{0.50 \cdot (E_3 + 1)}_{\text{self-loop}}$$

$$E_3 = 0.25\!\cdot\!1 \;+\; 0.25\!\cdot\!\tfrac{7}{3} \;+\; 0.50\,(E_3 + 1)$$

$$E_3 = \tfrac{1}{4} + \tfrac{7}{12} + \tfrac{1}{2}\,E_3 + \tfrac{1}{2}$$

$$\tfrac{1}{2}\,E_3 = \tfrac{3}{12} + \tfrac{7}{12} + \tfrac{6}{12} = \tfrac{16}{12} = \tfrac{4}{3}$$

$$\boxed{\;E_3 = \frac{8}{3} \approx 2.667\;}$$

&nbsp;

**Recompute $E_4$** &ensp; ($p_4$ can now reach $p_2$, and through $p_2$ reach $p_1$):

$$E_4 = 0.58 \cdot \bigl(S(p_2) + 1\bigr) \;+\; 0.42 \cdot (E_4 + 1)$$

$$E_4 = 0.58 \cdot \tfrac{7}{3} + 0.42\,E_4 + 0.42$$

$$0.58\,E_4 = \tfrac{4.06}{3} + 0.42 = 1.353 + 0.42 = 1.773$$

$$\boxed{\;E_4 \approx 3.057\;}$$

&nbsp;

Select the minimum: $E_3 = \tfrac{8}{3}$ is smallest.

> **Result.** &ensp; $S(p_3) = \dfrac{8}{3}$. &ensp; Add $p_3$ to the Markov chain.

By the same procedure, $S(p_4)$ is computed last.

---

##### Summary of Construction

$$S(p_1) = 0 \;\;\leq\;\; S(p_2) = \frac{4}{3} \;\;\leq\;\; S(p_3) = \frac{8}{3} \;\;\leq\;\; S(p_4) \approx 3.06$$

> **Properties verified:**
> - Scores are nondecreasing. &ensp; $\checkmark$
> - Each score equals the expected steps to $p_1$ under the Markov chain that score function induces. &ensp; $\checkmark$ &ensp; *(self-consistent)*
> - Complexity: $O(n \log n + m)$ where $n = |P|$, $m =$ nonzero generation probabilities.

---

### 2.4 The Main Proof — Nondecreasing Scores

This is the paper's central theorem. It guarantees the Dijkstra-like construction always produces a valid, consistent score function.

> **Theorem (Nondecreasing Scores).**
>
> Let $p_i$ be the $i$-th program added to the Markov chain by the construction in Section 2.3. Then:
>
> $$S(p_1) \;\leq\; S(p_2) \;\leq\; \cdots \;\leq\; S(p_n)$$

---

#### 2.4.1 Proof by Induction

##### Base Case &ensp; $(i = 1)$

$$S(p_1) = 0 \qquad \text{and} \qquad S(p_2) \geq 0$$

since $S(p_2)$ is an expected number of steps, which is always non-negative. Therefore $S(p_1) \leq S(p_2)$. &ensp; $\checkmark$

&nbsp;

##### Inductive Hypothesis

Assume $S(p_j) \leq S(p_{j+1})$ holds for all $j < i$.

&nbsp;

##### Inductive Step

**Goal:** &ensp; Show $S(p_i) \leq S(p_{i+1})$.

The proof proceeds in six steps.

---

**Step A. &ensp; Define the key quantities.**

Let $E$ be the expected number of steps from $p_{i+1}$ to $p_1$, computed under the Markov chain at step $i$ (where $p_1, \ldots, p_{i-1}$ have finite scores).

Let $q_{i+1,k}$ denote the probability that $p_{i+1}$ generates $p_k$ &ensp; (the raw generation weight).

> At step $i$, the algorithm selected $p_i$ as the $\infty$-scored program with
> the *minimum* expected steps. Since $p_{i+1}$ was also $\infty$-scored at step $i$,
> its expected steps $E$ satisfies $S(p_i) \leq E$.

---

**Step B. &ensp; Write the recurrence for $E$.**

From $p_{i+1}$ under the step-$i$ chain, each round has two outcomes:

| Event | Probability | Cost |
|:---|:---:|:---|
| Generate $p_k$ with $k < i$ (in chain, finite score) — transition to $p_k$ | $\displaystyle\sum_{k < i} q_{i+1,k}$ | $S(p_k) + 1$ |
| Generate a program with index $\geq i$ (score $\infty$, rejected) — stay put | $1 - \displaystyle\sum_{k < i} q_{i+1,k}$ | $E + 1$ |

This gives:

$$\tag{1} E \;=\; \Bigl(1 - \sum_{k<i} q_{i+1,k}\Bigr)(E + 1) \;+\; \sum_{k<i} q_{i+1,k}\bigl(S(p_k) + 1\bigr)$$

---

**Step C. &ensp; Solve for $E$.**

Expand equation $(1)$:

$$E \;=\; E + 1 \;-\; \Bigl(\sum_{k<i} q_{i+1,k}\Bigr) E \;-\; \sum_{k<i} q_{i+1,k} \;+\; \sum_{k<i} q_{i+1,k}\bigl(S(p_k) + 1\bigr)$$

Collect all $E$ terms on the left:

$$\tag{2} E \cdot \sum_{k<i} q_{i+1,k} \;=\; 1 \;-\; \sum_{k<i} q_{i+1,k} \;+\; \sum_{k<i} q_{i+1,k}\bigl(S(p_k) + 1\bigr)$$

Define two shorthand variables:

> $$b \;\;\triangleq\;\; \sum_{k<i} q_{i+1,k}$$
>
> *Total probability of generating an already-scored program.*

> $$a \;\;\triangleq\;\; 1 - b \;+\; \sum_{k<i} q_{i+1,k}\bigl(S(p_k) + 1\bigr)$$
>
> *Numerator of the expected-steps formula.*

Therefore:

$$\tag{3} \boxed{\; E \;=\; \frac{a}{b} \;}$$

---

**Step D. &ensp; Establish the key inequality.**

By the greedy construction, $p_i$ was chosen at step $i$ as the $\infty$-scored program with the **minimum** expected steps. Since $p_{i+1}$ was also $\infty$-scored at step $i$:

$$S(p_i) \;\leq\; E \;=\; \frac{a}{b}$$

Multiplying both sides by $b > 0$ :

$$\tag{4} \boxed{\; a \;\geq\; S(p_i) \cdot b \;}  \qquad \star\;\textit{Key Inequality}$$

> This is the **linchpin** of the entire proof.
> Everything that follows shows this inequality implies the desired result.

---

**Step E. &ensp; Compute $S(p_{i+1})$ under the updated chain.**

At step $i+1$, program $p_i$ has been added with score $S(p_i)$. Now $p_{i+1}$ can transition to $p_1, \ldots, p_{i-1}$ **and also to $p_i$**. The updated recurrence:

$$S(p_{i+1}) = \underbrace{\Bigl(1 - \sum_{k<i} q_{i+1,k} - q_{i+1,i}\Bigr)\bigl(S(p_{i+1}) + 1\bigr)}_{\text{self-loop}} + \underbrace{\sum_{k<i} q_{i+1,k}\bigl(S(p_k) + 1\bigr)}_{\text{to } p_1 \ldots p_{i-1}} + \underbrace{q_{i+1,i}\bigl(S(p_i) + 1\bigr)}_{\textbf{new: to } p_i}$$

Solving (same algebra as Step C, with the additional $q_{i+1,i}$ term):

$$\tag{5} \boxed{\; S(p_{i+1}) \;=\; \frac{a \;+\; q_{i+1,i} \cdot S(p_i)}{b \;+\; q_{i+1,i}} \;}$$

where $a$ and $b$ are the same quantities from Step C.

> **Observation.** &ensp; Equation $(5)$ is a **weighted average** of $E = \frac{a}{b}$ and $S(p_i)$,
> with weights $b$ and $q_{i+1,i}$ respectively.

---

**Step F. &ensp; Prove $S(p_{i+1}) \geq S(p_i)$.**

We need to show:

$$\frac{a + q_{i+1,i} \cdot S(p_i)}{b + q_{i+1,i}} \;\;\geq\;\; S(p_i)$$

Multiply both sides by $(b + q_{i+1,i}) > 0$ :

$$a + q_{i+1,i} \cdot S(p_i) \;\;\geq\;\; S(p_i) \cdot b \;+\; S(p_i) \cdot q_{i+1,i}$$

The $q_{i+1,i} \cdot S(p_i)$ terms appear on both sides — cancel them:

$$a \;\;\geq\;\; S(p_i) \cdot b$$

**This is exactly the $\star$ Key Inequality from Step D, equation $(4)$.**

$$\blacksquare$$

> **Conclusion.** &ensp; $S(p_i) \leq S(p_{i+1})$. &ensp; By induction:
>
> $$S(p_1) \;\leq\; S(p_2) \;\leq\; \cdots \;\leq\; S(p_n) \qquad \square$$

---

#### 2.4.2 Geometric Intuition

Equation $(5)$ reveals a clean geometric picture. Since $S(p_{i+1})$ is a weighted average:

$$S(p_{i+1}) = \frac{b}{b + q_{i+1,i}} \cdot \underbrace{\frac{a}{b}}_{E} \;\;+\;\; \frac{q_{i+1,i}}{b + q_{i+1,i}} \cdot S(p_i)$$

this is a **convex combination**, so the result lies between the two values:

$$S(p_i) \;\;\leq\;\; S(p_{i+1}) \;\;\leq\;\; E$$

> **In plain English.** &ensp; Adding $p_i$ as an intermediate node can only *help*
> $p_{i+1}$ (by opening a new pathway to $p_1$), but it can never help so much
> that $p_{i+1}$ becomes better than $p_i$ itself.
>
> This mirrors Dijkstra: settling a new node can reduce distances to unsettled
> nodes, but the settled node's distance is always $\leq$ any node settled after it.

---

### 2.5 Key Results

#### Proven Results

| # | Result | Proof |
|:---:|:---|:---|
| 1 | **Existence** — For any finite $P$ with the Markov property, a consistent score function exists. | Constructive: the algorithm in Section 2.3 produces one. |
| 2 | **Computability** — The score can be computed in $O(n \log n + m)$ time. | Dijkstra with priority queue; $n = |P|$, $m =$ nonzero transition probs. |
| 3 | **Nondecreasing Scores** — Programs are added in nondecreasing score order. | Induction proof in Section 2.4. |
| 4 | **Consistency** — $S$ equals expected steps to optimal under the process $S$ defines. | Follows from nondecreasing property + construction. |

#### Empirical Results (Simulation Only — Not Proven)

| # | Result | Evidence |
|:---:|:---|:---|
| 5 | **Logarithmic Convergence** — Expected steps to optimal grow as $O(\log n)$. | Simulations with $n = 2^l$ for $l = 1,\ldots,20$. Linear regression of steps vs. $l$: $R^2 = 0.983$. |
| 6 | **Exponential Rank Improvement** — Ranks improve exponentially per step. | 100 runs with $n = 2^{20}$. Log-scale rank drops linearly with step count. |

---

### 2.6 Accuracy Assessment

#### 2.6.1 What Is Mathematically Sound

| Claim | Verdict | Notes |
|:---|:---:|:---|
| Induction proof (Section 2.4) | **Correct** | Recurrences, algebra, and final cancellation all check out. |
| Dijkstra analogy | **Valid** | Nondecreasing property is the exact analog of Dijkstra's invariant. |
| Markov chain formulation | **Well-defined** | Given the Markov assumption, existence is properly demonstrated. |
| Simulation methodology | **Reasonable** | Standard setup: random subsets, weighted distributions, 10 repeats. |

#### 2.6.2 Limitations and Weaknesses

> **1. &ensp; Circularity (the paper acknowledges this).**
>
> Computing $S$ requires knowing all transition probabilities *and* the
> optimal program $p^*$ in advance. The paper admits: *"the score function
> is precomputed, which takes more time than enumerate every program to
> find the optimal."* The $O(\log n)$ runtime of the RSI procedure is real,
> but the setup cost is $O(n)$ or worse.

> **2. &ensp; The Markov assumption is very restrictive.**
>
> Real self-improving systems learn from experience — their generation
> distributions change based on history. Dropping this assumption
> invalidates the entire framework.

> **3. &ensp; Finite program space.**
>
> Real program spaces are countably infinite (or uncountable if parameterized).
> The proof relies fundamentally on finiteness. Extension would require
> measure-theoretic machinery not present in the paper.

> **4. &ensp; Logarithmic convergence is empirical, not proven.**
>
> The $O(\log n)$ result comes only from simulation. $R^2 = 0.983$ is
> suggestive but not a proof. No theoretical bound is provided.

> **5. &ensp; Narrow simulation setup.**
>
> The first program generates uniformly; others use random subsets with
> random weights. Logarithmic scaling might not hold for adversarial
> or highly structured transition matrices.

> **6. &ensp; Consistency is non-trivial in practice.**
>
> Practical score functions (benchmarks, test pass rates, loss functions)
> almost certainly won't satisfy Wang's consistency property. Robustness
> to inconsistent or noisy scores remains an open problem.

#### 2.6.3 Overall Verdict

> **The math is correct. The result is weaker than it appears.**
>
> Wang proves: *"If you already know the optimal program and all transition
> probabilities, you can construct a score function that makes the RSI
> procedure well-defined and scores nondecreasing."*
>
> This is a valid **existence proof** — not a practical algorithm. The hard
> part of RSI (not knowing the optimal or the transition structure) is
> assumed away.
>
> **Key takeaway for applied work:** RSI can be modeled as Markov chain
> optimization with a Dijkstra-like score construction. The greedy
> "always accept improvements" strategy is provably sound. If the
> logarithmic scaling generalizes, efficient RSI is possible in
> principle — a non-trivial claim given naive enumeration is $O(|P|)$.

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
> See Steps A–F for derivations.

| Symbol | Meaning |
|:---|:---|
| $P = \{p_1, \ldots, p_n\}$ | Finite program space, ordered by score |
| $S(p_i)$ | Score of the $i$-th program added (= expected steps to $p_1$) |
| $q_{i+1,k}$ | Raw probability that $p_{i+1}$ generates $p_k$ |
| $b = \displaystyle\sum_{k<i} q_{i+1,k}$ | Total probability of generating an already-scored program |
| $a = 1 - b + \displaystyle\sum_{k<i} q_{i+1,k}(S(p_k)+1)$ | Numerator of the expected-steps formula |
| $E = \dfrac{a}{b}$ | Expected steps from $p_{i+1}$ to $p_1$ at step $i$ **(before** $p_i$ **is available)** |
| $S(p_{i+1}) = \dfrac{a + q_{i+1,i} \cdot S(p_i)}{b + q_{i+1,i}}$ | Expected steps at step $i+1$ **(after** $p_i$ **is available)** |

**Key inequality chain** &ensp; (follows from $a \geq S(p_i) \cdot b$, the greedy selection guarantee):

$$S(p_i) \;\;\leq\;\; S(p_{i+1}) \;\;\leq\;\; E$$

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
