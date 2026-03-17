# Pax AI Interview Prep — Derek J. Russell
**Date:** March 17, 2026, 4:00 PM PST
**Format:** Google Meet (Video), 45 minutes
**Interviewer:** Chris Le, Co-Founder & CTO
**Role:** Junior Software Engineer

---

## 1. ABOUT PAX AI

### Company Overview
Pax AI is building the **easiest platform for U.S. duty drawback** — helping companies recover tariffs paid on imported goods when those goods are later exported, returned, or destroyed. Think **"TurboTax for import duty refunds."**

### The Problem
- **$10-15 billion** in eligible tariff refunds go **unclaimed every year** (80% of eligible refunds)
- Traditional customs brokerages charge 10-20% fees and refuse claims under $100K
- The process is extremely manual, taking 6-12+ months with legacy tools
- The underlying math is a complex **combinatorial optimization problem** — matching millions of import records to export records to maximize refunds

### The Solution
- AI-powered automation reduces processing from **6+ months to ~10 working days**
- No $100K minimum — serves brands under $50M in revenue
- Recovers **3-5% of revenue** for eligible companies
- Algorithms generate **15% more in refunds** vs. traditional methods

### Business Model
- **B2B SaaS** with success-based pricing (charge based on refunds recovered)
- **Channel partnerships** with customs brokers and freight forwarders
- Revenue tripled after Trump's 2025 tariff announcements (10%+ across the board, up to 145% on Chinese goods)
- Crossed **$1M in booking revenue** in less than a year

### Funding & Stage
- **Y Combinator S24**
- **$4.5M seed round** (April 2025), led by Initialized Capital
- Other investors: Sancus, Basis Set, Soma Capital, General Catalyst, Flexport angels
- **7-person team**, San Francisco-based, in-person

### Tech Stack (from job listings)
- **Frontend:** React, Next.js
- **Backend:** FastAPI, SQLModel (Python)
- **Database:** PostgreSQL
- **AI/LLM:** OpenAI + Anthropic models, prompt engineering (plug-and-play architecture)
- Three AI application areas:
  1. **Data extraction** — LLMs parse unstructured docs (PDFs, invoices, ERP exports)
  2. **Chatbot/application assistance** — AI helps users draft government applications
  3. **Optimization engine** — Deterministic algorithms (NOT LLM) for import/export matching

### Key Competitor
- **Charter Brokerage** — 30% market share, ~$1B processed annually, acquired by Berkshire Hathaway for $500M in 2014

---

## 2. ABOUT CHRIS LE (Your Interviewer)

- **Co-Founder & CTO** of Pax AI
- Second-time founder, 6+ years in software engineering
- **Amazon:** Built and scaled supply chain network systems (where he met the logistics space)
- **TikTok:** Built e-commerce merchant systems from 0 to 1
- **Brex:** Built financial rewards and billing systems
- **Kyros:** His first startup — live-streaming e-commerce platform in Singapore
- Education: Simon Fraser University
- Met co-founder Penny Chen through YC's co-founder matching platform

### What Chris Values (from Rippling interview)
- Engineers who **build things independently and learn by doing**
- **Work trials over traditional interviews** — real code, real codebase
- Scrappy, product-focused mentality
- People who **figure things out and execute**
- Clear reasoning over perfect answers
- Communication and curiosity

---

## 3. YOUR INTRODUCTION (30-second version)

> "Hi Chris, great to meet you. I'm Derek Russell — I recently graduated from Cal Poly San Luis Obispo in Computer Science. For the past year and a half, I've been building JARVIS, which is an autonomous AI operating system that spans three repositories — JARVIS as the body handling macOS integration and voice biometrics, J-Prime as the mind running 11 specialist LLM models on GCP infrastructure, and Reactor-Core as the training and continuous learning engine. The system includes a self-governing pipeline called Ouroboros that autonomously detects issues, generates patches, validates them, and applies them across all three repos with saga-style safety guarantees. I'm excited about Pax because you're using AI to automate complex regulatory workflows at the intersection of logistics and compliance — which is exactly the kind of hard, real-world problem I love solving."

---

## 4. THE TRINITY ECOSYSTEM — DEEP WALKTHROUGH

Use this framework for **every project question**: Problem → Technology Choice → How It Works → Challenges → What You'd Improve.

### 4A. JARVIS (Body)

**What problem were you solving?**
> I wanted to build a personal AI assistant that goes beyond chat — one that can see my screen, hear my voice, authenticate who I am, control my computer, and autonomously improve its own code. The core challenge was orchestrating dozens of subsystems (voice, vision, LLM inference, computer control, governance) into a single coherent system.

**Why these technologies?**
- **Python + asyncio**: Async-first architecture because every subsystem involves I/O (network, audio, disk). Blocking calls in an AI assistant are unacceptable — the system must listen, think, and act concurrently.
- **FastAPI**: Backend API + WebSocket for real-time bidirectional communication between frontend and kernel.
- **React**: Frontend dashboard on port 3000 for visual monitoring.
- **ECAPA-TDNN**: State-of-the-art speaker verification model — 192-dimensional embeddings give robust voice biometric authentication with an 85% confidence threshold.
- **Cloud SQL (PostgreSQL)**: Durable storage for voiceprints (59 samples) and system state.

**How does the system work?**
- **unified_supervisor.py** is a 101K-line monolithic kernel with a 7-zone boot sequence:
  - Zone 0-2: Protection, imports, utilities (signal handlers, BLAS guards, logging)
  - Zone 3-4: Resource managers and intelligence layer (GCP VM, ML routing)
  - Zone 5: Process orchestration (Trinity startup, health checks)
  - Zone 6: Ouroboros governance (autonomous self-programming)
  - Zone 7: Entry point and main event loop
- The kernel is a **singleton** (`JarvisSystemKernel`) managing lifecycle of all subsystems with circuit breakers, distributed locking, and graceful shutdown.

**What challenges did you encounter?**
1. **asyncio.wait_for() cancels tasks on timeout** — learned to use `asyncio.shield()` for tasks that must continue after timeout. `CancelledError` is `BaseException` in Python 3.9+, not caught by `except Exception`.
2. **OpenBLAS ARM64 GEMM bugs** — numpy 1.26.x crashes on M-series Macs. Fixed with `OPENBLAS_CORETYPE=ARMV8` and single-threaded BLAS.
3. **Signal handler safety** — must reset to default BEFORE any work; use `os._exit()` not `sys.exit()`.
4. **Memory pressure on 16GB Mac** — built a memory budget broker with pressure-aware offloading to GCP Spot VMs when RAM exceeds 85%.
5. **Distributed locking across processes** — built DLM v3.2 with two-tier locking (asyncio.Lock for speed + fcntl.flock for cross-process safety).

**What would you improve?**
> The 101K-line monolith is the biggest tech debt. I'd break it into microservices with clear API boundaries — a voice service, a governance service, a routing service. I'd also add proper OpenTelemetry tracing for end-to-end observability instead of custom logging. And I'd invest in a proper message queue (like NATS or Redis Streams) instead of direct async communication between subsystems.

---

### 4B. J-PRIME (Mind)

**What problem were you solving?**
> I needed a cognitive engine that could handle diverse task types — math, coding, translation, chain-of-thought reasoning — without paying $3.67/hour for GPU inference. The key insight was that **task-aware routing to specialist models** outperforms a single large generalist model, especially on a budget.

**Why these technologies?**
- **llama-cpp-python**: C++ inference engine with Python bindings. Runs quantized GGUF models on CPU efficiently.
- **GCP Compute Engine (g2-standard-4 + NVIDIA L4)**: Production inference with GPU acceleration at ~43-47 tok/s.
- **Q4_K_M quantization**: 4-bit mixed precision — compresses 7B models from ~14GB to ~4.4GB while retaining 95%+ quality.
- **Golden Image strategy**: All 11 models pre-baked into the VM image — eliminates the 30-60 minute download on cold boot.

**How does the system work?**
- **11 specialist models** (40.4 GB total), 8 routable:
  - Phi-3.5-mini (3.8B) — fast lightweight tasks
  - Qwen2.5-Math-7B — 83.6% on MATH benchmark
  - Qwen2.5-Coder-7B — 70.4% HumanEval
  - DeepSeek-R1-Distill-Qwen-7B — chain-of-thought, 55.5% AIME 2024
  - Gemma-2-9B — general default
  - Mistral-7B — multilingual translation
  - Llama-3.1-8B — 128K context window
- **GCPModelSwapCoordinator** maps task types to optimal models
- **PrimeRouter** provides 3-tier inference fallback: PRIME_API → PRIME_LOCAL → CLAUDE
- **Deadline propagation**: Monotonic deadline flows through all layers — each layer caps its timeout to remaining time, preventing destructive cascading timeouts.

**What challenges did you encounter?**
1. **Model swap latency** (~20-30s): Mitigated with sticky routing to avoid thrashing.
2. **Cold boot time**: Solved with golden image — total boot to inference-ready is ~87 seconds.
3. **Circuit breaker design**: Had to make it endpoint-aware so it resets when the endpoint changes (hot-swap scenarios).
4. **CPU-only inference initially**: 3-5 tok/s was too slow for complex tasks. Upgraded to g2-standard-4 with NVIDIA L4 GPU — now ~43-47 tok/s.

**What would you improve?**
> I'd add speculative decoding (TinyLlama-1.1B is already staged) to boost throughput 2-3x. I'd also implement proper model caching with LRU eviction instead of single-model-at-a-time loading. And I'd build a proper A/B testing framework to continuously measure which routing decisions actually improve user outcomes.

---

### 4C. REACTOR-CORE (Nerves)

**What problem were you solving?**
> The system needs to continuously improve. Reactor-Core collects experience data — which model answered well, which answers got escalated to Claude, what the user corrected — and uses that to fine-tune models via LoRA/DPO, creating a feedback loop where the system literally gets smarter from every interaction.

**Why these technologies?**
- **DPO (Direct Preference Optimization)**: Learns from pairs of good/bad responses without needing a separate reward model.
- **LoRA**: Parameter-efficient fine-tuning — trains 0.1% of parameters while achieving 95% of full fine-tune quality.
- **GCP Spot VMs**: Training on interruptible VMs at 60-91% discount, with automatic recovery on preemption.

**How does the system work?**
1. JARVIS logs every inference request and its outcome
2. Escalation events (where J-Prime failed and Claude succeeded) create preference pairs
3. Reactor-Core runs DPO training on these pairs periodically
4. Improved model weights are deployed back to J-Prime's golden image
5. Cycle repeats — continuous improvement loop

---

### 4D. OUROBOROS GOVERNANCE PIPELINE (The Crown Jewel)

**What problem were you solving?**
> Most AI assistants are static — they never improve their own code. I wanted JARVIS to be **self-governing**: detect issues, generate fixes, validate them, and apply them safely across all three repos. The hard part isn't generating code — it's doing it **safely** with rollback guarantees.

**How does the system work?**
```
Sensors detect issues (test failures, code complexity, voice commands)
    → Intake Router deduplicates (60s window)
    → Context Expansion (TheOracle semantic search + file neighborhood graph)
    → Code Generation (J-Prime primary, Claude fallback)
    → Validation (lint + test per repo)
    → Governance Gate (trust graduation per file)
    → Apply via B+ saga (ephemeral branches)
    → Verify (run tests again post-apply)
    → Complete (ff-only merge to main)
```

**Key safety mechanisms:**
- **B+ Branch Isolation**: Every patch goes to `ouroboros/saga-<op_id>/<repo>` — never touches main until verified
- **Two-tier locking**: asyncio.Lock (fast in-process) + fcntl.flock (cross-process)
- **Trust graduation**: Files start in GOVERNED mode; trust increases with successful patches
- **File touch cooldown**: Max 3 touches per file per 10-minute window
- **Schema 2c.1**: Cross-repo patches coordinate changes across all three repos atomically
- **Noop detection**: If change already exists, fast-path to COMPLETE (no wasted work)
- **1,361 tests** validating the governance pipeline

---

## 5. WHY PAX AI IS A FIT FOR YOU

Frame your answers around these alignments:

| Pax AI Needs | Your Experience |
|---|---|
| FastAPI + Python backend | JARVIS kernel is 101K lines of Python, FastAPI backend on port 8010 |
| LLM integration (OpenAI, Anthropic) | Built 3-tier inference stack with 11 models + Claude fallback |
| Data extraction from docs | Voice biometric processing, audio signal analysis, structured extraction |
| PostgreSQL | Cloud SQL PostgreSQL for voiceprints, system state |
| Automation of complex workflows | Ouroboros governance pipeline automates code review/apply/verify |
| Small team, ship fast | Solo-built entire Trinity ecosystem — 3 repos, 100K+ lines |
| React/Next.js frontend | Portfolio site in Next.js/TypeScript, React frontend for JARVIS |
| Handle ambiguity | Designed adaptive authentication with fallback chains |

---

## 6. SAMPLE QUESTIONS & ANSWERS

### "Walk me through your favorite project."

> "JARVIS is a three-part autonomous AI system. At its core, the problem I was solving was: how do you build an AI assistant that doesn't just respond to commands but actually understands context, authenticates who you are by voice, runs inference on its own infrastructure, and improves its own code autonomously?
>
> The body — JARVIS — handles macOS integration, voice biometric auth with ECAPA-TDNN embeddings, and orchestration through a 7-zone boot sequence. The mind — J-Prime — runs on GCP with task-aware routing across 11 specialist models, each chosen for what they're best at: Qwen-Math for math, Qwen-Coder for code, DeepSeek-R1 for chain-of-thought reasoning. The nerves — Reactor-Core — continuously trains on escalation data using DPO.
>
> The most impressive part is Ouroboros — the self-governing pipeline that detects test failures or code quality issues, generates patches using J-Prime, validates them with lint and tests, and applies them through saga-style branch isolation. It's run 1,361 tests to validate the safety of that process."

### "How have you used AI or LLMs in your projects?"

> "Deeply. J-Prime serves 11 quantized GGUF models through llama-cpp-python with task-aware routing — a math query goes to Qwen2.5-Math, a coding question to Qwen2.5-Coder. The routing is done by a Phi classifier with grammar-constrained output so the classification is guaranteed valid.
>
> For the Ouroboros pipeline, I use both J-Prime and Claude as code generation backends. The system sends structured prompts with file neighborhoods — a graph of related files across 7 edge categories — so the model has semantic context about what it's changing.
>
> I also use ECAPA-TDNN for voice biometric authentication — the model generates 192-dimensional speaker embeddings, and I do cosine similarity matching against stored voiceprints. The confidence threshold is 85%, but I built multi-factor fusion that combines voice confidence with behavioral analysis and contextual intelligence for more robust authentication."

### "What technical challenges did you encounter?"

> "Three big ones stand out:
>
> **First, timeout cascading in async systems.** When you have a request flowing through 4 layers — WebSocket → Router → GCP client → inference — each layer had its own timeout. If the outer timeout fires, `asyncio.wait_for()` cancels the inner task, which is a `BaseException` in Python 3.9+ and doesn't get caught by `except Exception`. The fix was deadline propagation — a single monotonic deadline flows through all layers, and each layer computes its own remaining time.
>
> **Second, memory pressure on a 16GB Mac.** Loading even one 7B model takes 4-5GB. I built a memory budget broker that monitors system RAM and, when pressure exceeds 85%, automatically creates GCP Spot VMs to offload inference. The golden image strategy means those VMs boot to serving in under 90 seconds.
>
> **Third, the Ouroboros safety problem.** Autonomous code modification is dangerous. A bad patch could break the system that generates patches. I solved this with B+ branch isolation — every change goes to an ephemeral branch, gets validated with lint and full test suite, and only merges via fast-forward. If validation fails, the branch is preserved for forensics but never touches main."

### "What development tools do you use to build faster?"

> "Claude Code is my primary AI development tool — I use it extensively for code generation, debugging, and code review. I've also used Cursor and GitHub Copilot. For JARVIS specifically, I built custom tooling: a Textual-based TUI dashboard that shows real-time system state, a voice narrator that speaks every governance decision, and ops logging with frozen audit trails for every operation. For infrastructure, I use GCP CLI for VM management, Docker for containerization, and git with saga-style branching for safe autonomous code changes."

### "How would you build a simple AI-powered automation tool?"

> "For a duty drawback context — say, extracting line items from import invoices — I'd start with FastAPI for the backend, use an LLM like Claude or GPT-4 for document parsing with structured output (JSON schema enforcement), store results in PostgreSQL, and build a simple React frontend for review. The key is separating the AI extraction step from the deterministic validation step — LLMs are great at understanding unstructured text, but the business logic for matching imports to exports should be algorithmic, not AI-generated, because it needs zero error tolerance.
>
> I'd add a human-in-the-loop review step where extracted data is presented for confirmation before entering the optimization pipeline. And I'd instrument everything with logging so you can audit every extraction decision."

### "Tell me about a technical problem you had to debug."

> "One of the trickiest was a SIGSEGV crash that only happened during JARVIS startup on ARM64 Macs. It was intermittent — sometimes boot succeeded, sometimes it crashed with no useful stack trace.
>
> I traced it to OpenBLAS 0.3.23.dev (bundled with numpy 1.26.x) having GEMM bugs on ARM64. The crash happened when multiple threads triggered BLAS operations during parallel import of native C extensions. The fix was two-part: set `OPENBLAS_CORETYPE=ARMV8` to force a stable kernel, and set `OPENBLAS_NUM_THREADS=1` before ANY imports happen — literally in Zone 0 of the boot sequence, before numpy even loads.
>
> The lesson was that low-level numerical libraries have platform-specific bugs that surface under concurrency, and the only reliable fix is environment-level guards before the library initializes."

---

## 7. QUESTIONS TO ASK CHRIS

Pick 3-4 from this list depending on conversation flow. Aim for questions that show you've researched the company and are thinking about the actual work.

### About the Product & Technology
1. **"How does the optimization engine handle the combinatorial matching between import and export records? Is it more constraint-based or does it use graph-matching approaches?"**
   - Shows you understand the core technical problem.

2. **"You mentioned LLMs for data extraction from invoices and shipping documents. What's the biggest challenge there — schema variability across different document formats, or accuracy on edge cases?"**
   - Directly relevant to the work you'd be doing.

3. **"How do you handle the human-in-the-loop review step for LLM extractions? Is it a confidence-threshold-based routing, or does every extraction go through review?"**
   - Shows you think about production AI systems, not just model quality.

### About the Team & Role
4. **"What does a typical week look like for an engineer on the team? How much time is spent on new features vs. improving existing systems?"**
   - Practical question about day-to-day work.

5. **"What's the most impactful project a junior engineer has shipped at Pax so far?"**
   - Shows you want to make impact quickly.

6. **"With the tariff landscape changing rapidly, how does the team prioritize what to build next? Is it more customer-driven or regulation-driven?"**
   - Shows business awareness.

### About Growth & Culture
7. **"What does the path from junior to senior engineer look like at Pax? What skills or contributions would accelerate that?"**
   - Shows long-term thinking.

8. **"You went from Amazon and TikTok to founding Pax. What's the biggest difference in how you think about building software at a 7-person startup vs. at scale?"**
   - Personal question for Chris that shows genuine interest in his perspective.

---

## 8. HOW THE SYSTEMS WORK — DETAILED EXPLANATIONS

Use these when Chris asks "walk me through how this works" or "explain the architecture." Each has a **30-second elevator pitch**, a **2-minute technical walkthrough**, and **follow-up answers** for when he digs deeper.

---

### 8A. THE TRINITY ECOSYSTEM (High-Level)

**30-second pitch:**
> "JARVIS is a distributed AI system split into three repos that map to Body, Mind, and Nerves. The Body handles macOS integration — voice, vision, computer control, and orchestration. The Mind runs on GCP and does LLM inference across 11 specialist models with task-aware routing. The Nerves handle continuous learning — collecting experience data and fine-tuning models. All three start with a single command: `python3 unified_supervisor.py`. The supervisor discovers the other repos, starts them in the correct order, runs health checks, and coordinates everything through a 7-zone boot sequence."

**2-minute technical walkthrough:**
> "The architecture follows a clear separation of concerns. JARVIS — the body — is the control plane. It's a 101K-line Python monolith called `unified_supervisor.py` that acts as the kernel. It boots through 7 sequential zones:
>
> - Zones 0-2 handle protection and foundation — signal handlers, BLAS threading guards for ARM64, imports, logging, distributed locks.
> - Zone 3 sets up resource managers — Docker, GCP VM management, port checking.
> - Zone 4 initializes the intelligence layer — ML-based command routing, goal inference.
> - Zone 5 is process orchestration — this is where Trinity starts. It spawns J-Prime on port 8000 and Reactor on port 8090, polls their `/health` endpoints until they report `READY`, and preserves model loading progress during the handoff.
> - Zone 6 is the Ouroboros governance system — the self-programming loop I'm most proud of.
> - Zone 7 is the entry point and main event loop.
>
> Communication between repos happens through HTTP health endpoints, a shared state directory under `~/.jarvis/`, and an EventBridge that forwards governance events bidirectionally. There's also a `CrossRepoNarrator` that converts events from Prime and Reactor back into voice narration — with a loop-break guard so JARVIS doesn't narrate its own events back to itself.
>
> The key architectural principle is graceful degradation. If J-Prime is unreachable, inference falls back to Claude API. If governance fails, the kernel continues normally. If any zone times out (30s per zone), it's skipped and the system keeps booting. Nothing crashes the kernel."

**If Chris asks: "Why a monolith instead of microservices?"**
> "Honestly, it started as a prototype and grew. The monolith gives me fast iteration — I can change anything in one file and test immediately. But I recognize it's the biggest piece of tech debt. If I rebuilt it, I'd extract clear services: a voice service, a governance service, a routing service, each with its own API boundary. That said, the monolith is internally organized — the 7-zone structure acts like module boundaries, and the classes inside have clear single responsibilities: `JarvisSystemKernel` (~33K lines) is the actual kernel, `HealthCheckOrchestrator` (~15K) handles health, `WorkflowOrchestrator` (~11K) handles workflows. So it's a monolith by file, but modular by design."

---

### 8B. JARVIS (Body) — How It Actually Works

**When Chris asks: "So what does JARVIS actually do when you talk to it?"**
> "When I say a voice command, here's what happens end to end:
>
> 1. **Audio capture** — A `FullDuplexDevice` (CoreAudio HAL) continuously captures audio. VAD (Voice Activity Detection) identifies when I'm speaking.
> 2. **Intent detection** — The audio is transcribed (Whisper/Vosk), then classified. I built a zero-hardcoding ML command router — no keyword matching. It uses Apple's NaturalLanguage framework via a Swift bridge for grammar analysis, plus a Python neural network that learns from every interaction.
> 3. **Routing** — The `PrimeRouter` decides where to send the request: J-Prime on GCP (primary), local model (secondary), or Claude API (tertiary fallback). It uses circuit breakers per endpoint — after 5 consecutive failures, the endpoint is marked OPEN and skipped for 60 seconds.
> 4. **Inference** — The request goes to J-Prime with deadline propagation. A single monotonic deadline flows through all layers — WebSocket to Router to GCP client to inference. Each layer computes remaining time and caps its own timeout. This prevents destructive cascading timeouts.
> 5. **Response** — The response streams back through the async pipeline (5 stages: RECEIVED, INTENT_ANALYSIS, COMPONENT_LOADING, PROCESSING, RESPONSE_GENERATION), each with a 30-second timeout and circuit breaker protection.
> 6. **Voice output** — The response is spoken using `safe_say()` — which renders to a temp file via `say -o`, then plays via `afplay`. This avoids GIL contention and device conflicts with the input capture.
>
> The whole thing is async-first. Before I rebuilt the pipeline, JARVIS would get stuck on 'Processing...' for 5-35 seconds because synchronous subprocess calls blocked the event loop. After converting to fully async with `asyncio.create_subprocess_exec()`, response time dropped to 0.1-0.5 seconds."

**If Chris asks: "What about the voice authentication?"**
> "Voice unlock is a 16-step pipeline. The core is ECAPA-TDNN — a state-of-the-art speaker verification model that generates 192-dimensional embeddings from audio. I store 59 voiceprint samples per user in Cloud SQL PostgreSQL.
>
> When I say 'unlock my screen,' JARVIS captures audio, extracts an embedding, and computes cosine similarity against my stored voiceprints. The threshold is 85% confidence. But it's not just voice — I built multi-factor fusion:
>
> - Voice confidence gets 60% weight
> - Behavioral analysis (time-of-day patterns, unlock intervals) gets 25%
> - Contextual intelligence (location, failed attempts history) gets 15%
>
> I also built physics-aware anti-spoofing: a `ReverbAnalyzer` estimates room reverberation time (RT60) to detect playback attacks. A `VocalTractAnalyzer` verifies formant frequencies match an adult male vocal tract length (16-20cm). And a `DopplerAnalyzer` detects natural movement patterns that recordings can't replicate.
>
> For cost optimization, I added a ChromaDB semantic cache — if a voice sample is 98%+ similar to a recent successful auth, it short-circuits the full pipeline. Hit rate is 70-80%, reducing per-auth cost from $0.011 to $0.001."

---

### 8C. J-PRIME (Mind) — How It Actually Works

**When Chris asks: "How does the inference server work?"**
> "J-Prime runs on a GCP g2-standard-4 with an NVIDIA L4 GPU. It serves 11 specialist models, 8 actively routable.
>
> The key innovation is **task-aware routing**. Instead of one big model for everything, I match the task to the best specialist:
>
> - Math query goes to Qwen2.5-Math-7B (83.6% on MATH benchmark)
> - Code task goes to Qwen2.5-Coder-7B (70.4% HumanEval)
> - Chain-of-thought reasoning goes to DeepSeek-R1-Distill-Qwen-7B (55.5% AIME 2024)
> - Fast lightweight query goes to Phi-3.5-mini (3.8B, ~3s latency)
> - Long context goes to Llama-3.1-8B (128K context window)
> - Translation goes to Mistral-7B
> - General default goes to Gemma-2-9B
>
> All models are Q4_K_M quantized — 4-bit mixed precision that compresses 7B models from ~14GB to ~4.4GB while keeping 95%+ quality.
>
> The `GCPModelSwapCoordinator` handles routing. If the optimal model is already loaded, inference starts immediately. If not, there's a ~20-30s model swap. To prevent thrashing, I use sticky routing — same task type keeps the model loaded.
>
> With the L4 GPU, I get ~43-47 tokens/sec. The API is OpenAI-compatible (`/v1/chat/completions`), so any standard client works."

**If Chris asks: "What's the golden image strategy?"**
> "The problem: creating a fresh GCP VM means downloading 40GB of models — 30-60 minutes on cold boot. My solution: pre-bake all 11 models into a GCP disk image. When a VM spins up from this image:
> 1. GCP creates the VM (~26s)
> 2. Startup script launches the server (~30s)
> 3. Default model loads from local SSD — zero network downloads
> 4. Total: ~87 seconds from nothing to serving
>
> This matters because I also use GCP Spot VMs as overflow when my Mac's RAM exceeds 85%. Spot VMs can be preempted any time, so fast recovery is critical. The golden image makes recovery near-instant."

---

### 8D. REACTOR-CORE (Nerves) — How It Actually Works

**When Chris asks: "How does the learning loop work?"**
> "Reactor-Core creates a feedback loop where the system gets smarter from every interaction.
>
> The insight: every time J-Prime fails and escalates to Claude API, that's a training signal. Claude's answer becomes the 'preferred' response, J-Prime's failed answer is the 'rejected' response. That pair feeds into DPO — Direct Preference Optimization — which fine-tunes models without needing a separate reward model.
>
> The pipeline:
> 1. JARVIS logs every inference request, which model handled it, and the outcome
> 2. Escalation events create preference pairs
> 3. Reactor runs LoRA fine-tuning — parameter-efficient, modifies only 0.1% of weights while getting 95% of full fine-tune quality
> 4. Improved weights deploy back to J-Prime's golden image with a probation period — monitored for quality regression
> 5. If quality drops, automatic rollback to previous weights
>
> Training runs on GCP Spot VMs at 60-91% cost discount. Since training isn't latency-sensitive, preemption is fine — the pipeline auto-recovers from the last checkpoint."

---

### 8E. OUROBOROS — The Self-Programming Loop

**When Chris asks: "What is Ouroboros? How does it work?"**
> "Ouroboros is the system that lets JARVIS autonomously improve its own code across all three repos without human intervention. The hard problem isn't generating code — it's doing it safely with rollback guarantees.
>
> The flow:
>
> **Detection** — Four sensor types run per repo:
> - `BacklogSensor` polls `.jarvis/backlog.json` every 30 seconds
> - `TestFailureSensor` watches for pytest failures (needs 2+ consecutive to avoid flakes)
> - `OpportunityMinerSensor` scans for cyclomatic complexity >= 10 every 5 minutes
> - `VoiceCommandSensor` listens for direct commands
>
> **Dedup** — 60-second window prevents duplicate operations.
>
> **Context Expansion** — `TheOracle` does semantic search, builds a `FileNeighborhood` graph across 7 edge categories (imports, tests, configs, etc.) so the generator has full context.
>
> **Generation** — Primary: J-Prime on GCP. Fallback: Claude API. Uses schema 2b.1 (single-repo) or 2c.1 (cross-repo coordinated patches).
>
> **Validation** — Lint + full test suite per repo. Fails = patch rejected.
>
> **Safe Apply via B+ Branch Isolation:**
> - Create ephemeral branch: `ouroboros/saga-<op_id>/<repo>`
> - Apply patch, commit on ephemeral branch
> - Validate again post-apply
> - If all repos pass: fast-forward merge to main (guarantees linear history)
> - If any fail: `_bplus_compensate_all()` rolls back every repo to pre-saga state
>
> **Safety guarantees:**
> - Two-tier locking: asyncio.Lock + fcntl.flock, acquired in sorted order (prevents deadlock)
> - File touch cooldown: max 3 modifications per file per 10 minutes
> - Trust graduation: high-risk files require stricter validation
> - Frozen 15-field `SagaLedgerArtifact` audit trail per operation
> - `SagaMessageBus`: passive observer, zero execution authority
> - 1,361 tests validating the governance pipeline
>
> Every decision is narrated in real-time through voice and TUI."

**If Chris asks: "What if it breaks itself?"**
> "Three protection layers. First: no patch merges unless lint + tests pass. Second: B+ branch isolation — changes never touch main until validated. Failed branches are preserved for forensics. Third: trust graduation and file-touch cooldowns prevent runaway modification loops."

---

### 8F. ARCHITECTURE DIAGRAMS ON YOUR GITHUB PROFILE

If Chris pulls up your GitHub profile and asks about the diagrams:

**The main Architecture at a Glance diagram:**
> "This shows the layered structure. The Unified Supervisor at the top is the single entry point with its 7-zone boot sequence. Below it: JARVIS Body on port 8010 for computer use, voice, and vision. J-Prime on port 8000 for LLM inference. Reactor on port 8090 for training. GCP Golden Image is on-demand for cloud inference. Spot VMs are the memory-pressure fallback. Frontend on port 3000 is the web UI."

**The Ouroboros Loop diagram:**
> "This shows the full governance pipeline top to bottom. Sensors at the top detect issues. Intake router deduplicates. Operation context decides single-repo vs cross-repo schema. Candidate generator tries J-Prime first, Claude fallback. Orchestrator runs validate, review, apply, commit phases. CommProtocol at the bottom sends every event through five transports: logging, TUI, voice narration, ops logger, and EventBridge for cross-repo communication."

**The Triple Authority Resolution diagram (Mermaid):**
> "This solved a real distributed systems bug — three repos had independent supervisors making conflicting lifecycle decisions, causing restart storms. The fix: `RootAuthorityWatcher` is the policy brain (decides WHAT). `ProcessOrchestrator` is the execution plane (does HOW). Handshake gate blocks 'healthy but incompatible' subsystems using schema compatibility checks. Escalation ladder is deterministic: drain, then SIGTERM, then process-group SIGKILL."

**The Adaptive Timeout diagram (Mermaid):**
> "Instead of hardcoded timeouts, there's a policy chain: env var overrides win, then learned adaptive values (p95 latency + load factor), then static defaults. Shadow mode lets me validate the adaptive system without applying it. Feedback loop at the bottom tracks operation outcomes to improve future decisions."

---

### 8G. OTHER GITHUB PROJECTS CHRIS MIGHT ASK ABOUT

**Connect-Four-AI (TypeScript, 2 stars):**
> "An AI-powered Connect Four game using minimax with alpha-beta pruning and a heuristic evaluation function. Built in TypeScript with React. A fun project to understand adversarial search algorithms at a fundamental level."

**MLForge (C++):**
> "A modular ML framework I'm building in C++ for implementing and benchmarking ML algorithms from scratch — gradient descent, backpropagation, optimization algorithms. The goal is understanding the math behind ML, not just calling library functions."

**JARVIS-Portfolio (TypeScript/Next.js):**
> "My portfolio and technical blog built with Next.js, showcasing projects and technical write-ups."

**Ironman-Suit-Simulation (Python):**
> "A simulation of Iron Man suit engineering integrating AI, systems programming, and robotics — modeling sensor fusion, power management, and flight dynamics."

---

### 8H. CONNECTING YOUR EXPERIENCE TO PAX AI

After any technical deep-dive, bridge back to Pax:

**After explaining the async pipeline:**
> "This is relevant to Pax — processing duty drawback claims involves orchestrating multiple async operations: parsing documents, matching records, validating against regulations. The async pipeline patterns I built — circuit breakers, deadline propagation, graceful degradation — apply directly."

**After explaining task-aware routing:**
> "At Pax, you're using LLMs for data extraction from different document types. The same routing principle could optimize extraction accuracy — one model might be better at invoices while another handles bills of lading."

**After explaining Ouroboros safety:**
> "In duty drawback, safety matters even more than in my project. The validation-before-apply pattern, audit trails, and rollback guarantees are the kind of engineering discipline you need when incorrect calculations mean real money and compliance risk."

**After explaining the golden image:**
> "This is infrastructure-as-code thinking. At Pax's stage, having reproducible, fast-booting infrastructure means you can scale without configuration drift. Whether it's model serving or application deployment, the principle is the same."

---

## 9. INTERVIEW STRATEGY (45-Minute Breakdown)

| Time | Phase | Goal |
|------|-------|------|
| 0-5 min | Introductions | Your 30-second intro. Be warm, confident. |
| 5-20 min | Project deep-dive | Chris will ask about JARVIS. Use the Problem → Tech → How → Challenges → Improve framework. Be concise — 2-3 minutes per answer max. |
| 20-35 min | Technical discussion | LLM experience, system design thinking, debugging stories. Relate everything back to what Pax needs. |
| 35-42 min | Your questions | Ask 3-4 questions from the list above. |
| 42-45 min | Wrap-up | Express enthusiasm. "I'm excited about this — the intersection of AI and regulatory automation is exactly the kind of hard problem I want to work on." |

### Key Principles
- **Be concise.** 45 minutes goes fast. Target 2 minutes per answer, 3 max for deep dives.
- **Use the STAR-lite method**: Situation (1 sentence) → Action (2-3 sentences) → Result (1 sentence).
- **Relate to Pax.** After every technical answer, briefly connect it: "This is similar to how you'd want to handle X at Pax."
- **Show you build, not just study.** Chris values engineers who ship. Every answer should reference real code, real deployments, real failures you fixed.
- **Admit what you'd improve.** This shows maturity. "If I rebuilt it, I'd do X differently because I learned Y."
- **Think out loud.** If Chris asks a design question, verbalize your reasoning. He wants to see how you think, not just the answer.

### Things to AVOID
- Don't over-explain. If Chris wants more detail, he'll ask.
- Don't be defensive about the monolith. Acknowledge it, explain the trade-off, say what you'd change.
- Don't pretend you know duty drawback deeply. Say you've researched it and are excited to learn the domain.
- Don't talk about JARVIS for 20 minutes straight. Keep it tight, let Chris guide the conversation.

---

## 10. LOGISTICS CHECKLIST

- [ ] Test Google Meet link 15 minutes before
- [ ] Camera on, good lighting (face lit, not backlit)
- [ ] Quiet environment, no background noise
- [ ] Have this doc open on a second screen for reference
- [ ] Water nearby
- [ ] Professional but relaxed appearance (Pax is a startup — no suit needed)
- [ ] Close unnecessary browser tabs and notifications
- [ ] Have your GitHub profile open: https://github.com/drussell23
- [ ] Have the JARVIS repo open in case Chris asks to see something

---

## 11. CLOSING STATEMENT

When the interview ends:

> "Chris, thanks so much for your time. I'm genuinely excited about what Pax is building — the duty drawback space is a massive untapped opportunity, and the way you're combining LLM-powered extraction with deterministic optimization is exactly the right approach. I'd love to be part of the team helping ship that. Looking forward to next steps."

---

*Good luck, Derek. You've built something genuinely impressive. Let Chris see the engineer behind it.*
