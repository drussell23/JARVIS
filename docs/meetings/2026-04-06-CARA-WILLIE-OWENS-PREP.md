# Cara - Phone Screen Prep

**Date:** April 6, 2026 at 11:30 AM (10 minutes)
**Recruiter:** Willie Owens (3rd-party, Paraform)
**Company:** Cara — domain-specific AI platform for insurance
**Role:** Software Engineer (Full-stack)
**Location:** San Francisco (South Park), on-site 5 days/week
**Salary:** $120K - $210K + equity
**Team:** 19 people, $6M raised, founded 2024
**Tech Stack:** React, Golang, TypeScript
**Manager:** Nik Ansal

---

## What Cara Does

Cara builds **autonomous digital workers** for the insurance industry. Their AI
platform enables agencies and brokerages to automate servicing, accelerate sales,
and scale operations with a 24/7 digital workforce.

In plain terms: insurance agencies have employees who spend all day doing
repetitive tasks — processing claims, entering data, following up on quotes,
pulling information from different systems. Cara is building AI workers that
handle those tasks 24/7, so human employees can focus on the work that actually
requires human judgment.

### What They're Looking For

- 1+ years full-stack engineering experience
- High ownership and problem-solving ability
- Engineers who thrive in a scrappy, high-ownership startup environment
- Build features across frontend, backend, and infrastructure
- Strengthen AI infrastructure, async processing, and observability
- Collaborate directly with customers on tailored solutions

### The Core Problems Cara Is Solving

These are the hard engineering challenges behind building autonomous digital
workers. Every one of them maps to something I solved building Ouroboros:

1. **Making AI workers that actually do tasks reliably** — not just chat, but
   take real actions (fill forms, pull data, navigate systems) without breaking
2. **Safety and trust** — when an AI worker does something on behalf of a
   customer, it can't make mistakes. There need to be guardrails
3. **Knowing what the AI is doing** (observability) — insurance is regulated;
   you need a clear record of every action and decision
4. **Working across multiple systems** — insurance agencies use dozens of
   different tools; the AI needs to coordinate actions across all of them
5. **Getting smarter over time** — the AI should learn from what works and
   what doesn't, not repeat the same mistakes

---

## Question 1: Complex Technical Project You Owned End-to-End

### The Analogy: A Self-Healing Body

> Imagine your body's immune system. When you get a cut, you don't rush to the
> hospital every time. Your body detects the wound, sends white blood cells,
> repairs the tissue, checks that the repair worked, and remembers the infection
> so it can fight it faster next time. All automatic. No doctor needed.
>
> That's what I built — but for software. An AI system that acts as its own
> immune system: it detects problems, figures out fixes, checks they're safe,
> applies them, and learns for next time. No human in the loop.
>
> **And this is the exact same idea behind what Cara is building.** Cara's
> digital workers are like an immune system for an insurance agency — they detect
> work that needs doing (a claim comes in, a quote needs follow-up), figure out
> the right steps, execute safely, verify the result, and get smarter over time.
> Same architecture, different domain.

### The Answer (~2 Minutes)

> **Open with the connection to Cara:**
>
> "The project I want to talk about is directly related to what Cara is building.
> Cara creates autonomous digital workers for insurance — AI that takes real
> actions on behalf of customers. I built something called Ouroboros, which is
> an autonomous digital worker for software engineering. Same core problem,
> different domain."

> **The Problem (Keep It Simple):**
>
> "The challenge was: how do you build an AI system that can do real work on its
> own — not just answer questions, but detect problems, figure out solutions,
> execute them safely, and learn from the results? That's the hard part of
> autonomy. Chatbots are easy. Agents that take reliable action are hard."
>
> *This is Cara's exact challenge — their digital workers don't just chat with
> customers, they take real actions in insurance systems.*

> **How I Broke It Down (Five Layers, With Cara Parallels):**
>
> "I split the problem into five layers:
>
> 1. **Detection** — The system automatically spots when something needs
>    attention, like a test failing or a process running slow. Think of it like
>    a smoke detector — it watches for problems so you don't have to.
>
>    *Cara parallel: Their digital workers need to detect when a task needs
>    doing — a new claim arrives, a renewal is coming up, a customer sends
>    a request.*
>
> 2. **Planning & Routing** — Once a problem is detected, the system figures
>    out the best approach. Simple problems get a simple fix. Complex ones get
>    escalated to more powerful AI models. Like an ER triage — a sprained ankle
>    doesn't need a surgeon.
>
>    *Cara parallel: Not every insurance task is the same complexity. A simple
>    data entry task vs. a complex multi-step claims process need different
>    approaches.*
>
> 3. **Safety Gating** — Before any action is taken, a rules-based system
>    checks: is this safe to do automatically, does it need a human to approve,
>    or should it be blocked entirely? Like a pharmacist checking a
>    prescription before it goes to the patient.
>
>    *Cara parallel: This is critical for insurance. An AI worker filling out
>    a form is one thing. An AI worker approving a $50,000 claim needs human
>    oversight. You need clear rules about what the AI can and can't do on
>    its own.*
>
> 4. **Safe Execution** — Actions happen in an isolated environment first. If
>    anything goes wrong, it rolls back automatically. Nothing touches the real
>    system until we know it works. Like a test kitchen — you taste the dish
>    before serving it to customers.
>
>    *Cara parallel: When a digital worker takes an action in a customer's
>    insurance system, it can't half-finish and leave things in a broken state.
>    It needs to either complete the whole task or cleanly undo what it started.*
>
> 5. **Learning & Memory** — The system remembers what worked and what failed.
>    If a particular approach caused problems before, it avoids it next time.
>    It gets smarter with every task.
>
>    *Cara parallel: If a digital worker figures out that a particular insurance
>    carrier's portal requires a specific workflow, it should remember that for
>    next time instead of re-learning from scratch.*"

> **What Made It Hard (Vision & Trust — Directly Relevant to Cara):**
>
> "The hardest part was making the system work reliably in a visual environment —
> navigating real interfaces, clicking buttons, filling forms.
>
> Traditional automation is like giving someone written directions: 'click the
> third button from the left.' It works until the website changes and the button
> moves. Then everything breaks.
>
> So I built a feedback loop: after every action, the system takes a screenshot,
> analyzes it with a vision AI, and asks 'did that actually work?' If it sees an
> error page or something unexpected, it adapts — like a person who sees a 'Page
> Not Found' and decides to search for the page instead.
>
> I also added voice narration — the system speaks out loud about what it's
> doing: 'Opening the browser... navigating to the page... that didn't work, let
> me try another approach.' This was huge for trust. When an autonomous system
> explains itself in real time, people trust it.
>
> *This maps directly to Cara's challenge. Their digital workers need to navigate
> insurance portals, carrier websites, and agency management systems — all of
> which have different interfaces that change regularly. And in insurance,
> trust and transparency are everything. Customers and regulators need to see
> exactly what the AI did and why.*"

> **Close With Ownership & Scale:**
>
> "I owned this end-to-end — designed the architecture, wrote the code, tested
> it, shipped it. About 7,000 lines of core logic, 130+ modules, 40+ commits
> in the last month. Vision verification, voice narration, tool orchestration —
> all shipped to production."

### Key Phrases (Recruiter-Friendly)

- "Autonomous digital worker" — exact language Cara uses
- "Same core problem, different domain" — makes the connection instantly
- "Safety gating" — shows you think about consequences
- "Trust and transparency" — critical for insurance
- "End-to-end ownership" — shows you don't need hand-holding

---

## Question 2: Experience Working in a Fast-Paced Setting

### The Analogy: One-Person Pit Crew

> In Formula 1, a pit crew has 20+ people — one for each tire, one for fuel, one
> for the wing. Each person is a specialist. In a startup, you're all of those
> people at once. You're changing tires, refueling, and adjusting the wing —
> fast, correctly, and without dropping anything.
>
> That's how I work. I'm the sole engineer on an autonomous AI system. I'm the
> architect, the frontend developer, the backend developer, the infrastructure
> engineer, and the QA team. I don't get to hand things off and wait. I make
> decisions quickly and ship.
>
> **This is exactly what a 19-person startup like Cara needs.** At that size,
> everyone wears multiple hats. You can't afford to have someone who only does
> one thing or needs weeks to make a decision.

### The Answer (~1.5 Minutes)

> **Open With the Parallel:**
>
> "JARVIS is essentially my own startup. I'm the sole engineer building a full
> autonomous AI system — the same kind of product Cara is building, just in a
> different domain. There's no team to delegate to. I own the architecture, the
> AI infrastructure, the browser automation, the observability layer — all of
> it.
>
> So I operate the way a small startup has to operate: fast decisions, fast
> shipping, and full accountability."

> **Prove the Pace With Specifics:**
>
> "In the last month alone, I shipped three major features:
>
> - A **vision verification system** — the AI can now see what's on screen and
>   verify its own actions worked. This is the kind of feature Cara would need
>   for their digital workers navigating insurance portals.
>
> - A **real-time voice narration layer** — the system explains what it's doing
>   as it works, which builds trust. Cara's customers would want the same thing:
>   transparency into what the digital worker is doing on their behalf.
>
> - A **tool orchestration pipeline** — a safe, bounded system for the AI to use
>   tools (read files, run searches, execute tests) without going off the rails.
>   Same challenge Cara faces: giving AI workers access to real systems while
>   keeping them controlled.
>
> Three significant features, all shipped to production, all in one month."

> **Show Startup Judgment (Not Just Speed):**
>
> "But fast doesn't mean reckless. What keeps me fast is making strong decisions
> early.
>
> For example, I chose to make the safety system rule-based instead of AI-based.
> A rule-based system is less 'smart,' but it's completely predictable — you can
> always explain exactly why a decision was made. In insurance, that matters even
> more than in my domain. Regulators want to know why the AI approved something.
> 'The AI thought it was fine' isn't an acceptable answer. 'Rule 4 says claims
> under $5,000 with complete documentation can be auto-approved' is.
>
> That one decision saved me countless hours of debugging unpredictable behavior,
> which let me move faster on everything else.
>
> I also scope ruthlessly. When cross-repo changes were causing partial failures,
> I didn't patch around it — I built a proper transaction system with automatic
> rollback. A little more upfront time, but it eliminated an entire category of
> bugs. That's the kind of tradeoff you have to make in a startup — invest where
> it compounds."

> **Close With Why Cara:**
>
> "A 19-person team building autonomous AI workers for insurance is exactly where
> I want to be. The problems Cara is solving — reliable AI execution, safety
> gating, observability, working across messy real-world systems — those are the
> exact problems I've been solving. I'd be coming in with direct experience, not
> a learning curve."

### Key Phrases (Recruiter-Friendly)

- "Same kind of product, different domain" — instant relevance
- "Three major features in one month" — concrete proof of pace
- "Fast doesn't mean reckless" — shows mature judgment
- "Regulators want to know why" — shows you understand insurance context
- "Direct experience, not a learning curve" — powerful closing line

---

## Quick-Reference: Cara's Problems and Your Solutions

Use this as a cheat sheet. If Willie asks a follow-up, pull from this:

| Cara's Problem | Your Ouroboros Solution | Simple Explanation |
|---|---|---|
| AI workers that take real actions | Tool-use orchestration with 5 tools, bounded rounds | "My AI doesn't just suggest things — it reads files, runs searches, executes tests, all on its own" |
| Safety so AI doesn't make mistakes | Deterministic risk engine (rule-based, not AI-based) | "Every action gets classified: safe to auto-do, needs human approval, or blocked. Clear rules, not AI guessing" |
| Transparency / observability | Voice narration + ledger logging | "The system explains what it's doing in real time and keeps a complete record of every action and decision" |
| Working across multiple systems | Saga transactions across 3 repos with rollback | "When the AI needs to change things in multiple systems at once, it either completes everything or undoes everything cleanly" |
| Getting smarter over time | Memory engine + learning from failures | "The system remembers what worked and what didn't — it doesn't repeat the same mistakes" |
| Navigating real interfaces | Vision verification loop (screenshot + AI analysis) | "After every action, the system looks at the screen and checks: did that actually work? If not, it adapts" |
| Handling different complexity levels | Brain selector routes to different AI models | "Simple tasks get a fast, lightweight approach. Complex ones get escalated to more powerful models. Like ER triage" |

---

## Logistics & Tips

- **Duration:** 10 minutes — keep answers tight (2 min max each)
- **Willie is a recruiter, not an engineer** — lead with analogies, then specifics
- **Thread the Cara connection throughout** — don't save it for the end
- **Leave time for your questions** (ask 1-2):
  - "What does the interview process look like after this call?"
  - "What's the team's biggest priority right now?"
  - "How quickly is the team looking to fill this role?"
- **If Willie asks "why Cara?":**
  > "I've been building an autonomous AI agent system for the past several months —
  > the exact same class of product Cara is building. When I saw this role, it
  > was the first time I've seen a company solving the same problems I've been
  > deep in. I wouldn't be ramping up on the concepts — I'd be bringing direct
  > experience from day one."
