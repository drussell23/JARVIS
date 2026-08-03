# JARVIS Learning Database - Comprehensive Guide

## Table of Contents
1. [Overview](#overview)
2. [Architecture & Design](#architecture--design)
3. [How It Works](#how-it-works)
4. [Database Schema](#database-schema)
5. [Purpose & Role](#purpose--role)
6. [Integration with UAE + SAI](#integration-with-uae--sai)
7. [Test Scenarios](#test-scenarios)
8. [Edge Cases](#edge-cases)
9. [Limitations](#limitations)
10. [Potential Improvements](#potential-improvements)
11. [Troubleshooting](#troubleshooting)
12. [Performance Tuning](#performance-tuning)

---

## Overview

### What Is the Learning Database?

The **JARVIS Learning Database** is a hybrid persistent memory system that enables JARVIS to:
- **Remember** user patterns across sessions
- **Learn** from every interaction
- **Predict** future actions based on history
- **Adapt** to changes in the environment
- **Improve** performance over time

### Key Features

| Feature | Description | Benefit |
|---------|-------------|---------|
| **Hybrid Storage** | SQLite (structured) + ChromaDB (semantic) | Best of both worlds |
| **Async I/O** | Non-blocking database operations | No performance impact |
| **Adaptive Caching** | Smart LRU cache with TTL | 85%+ cache hit rate |
| **ML-Powered** | Embeddings for similarity search | Generalizes to new situations |
| **Cross-Session** | Survives restarts/crashes | True persistent memory |
| **Self-Optimizing** | Auto-cleanup, VACUUM, indexing | Maintains performance |

### Architecture Type

```
Hybrid Architecture = SQLite (OLTP) + ChromaDB (Vector Store) + Async + ML
```

---

## Architecture & Design

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                    JARVIS Learning Database                          │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │                     Application Layer                           │ │
│  │  (UAE, SAI, Memory Quantizer, System Monitor, etc.)            │ │
│  └──────────────────────────┬──────────────────────────────────────┘ │
│                             │                                        │
│                             ↓                                        │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │              JARVISLearningDatabase (API Layer)                │ │
│  │                                                                │ │
│  │  Public Methods:                                               │ │
│  │  • store_pattern()      • get_pattern_by_type()               │ │
│  │  • store_action()       • learn_display_pattern()             │ │
│  │  • store_goal()         • get_learning_metrics()              │ │
│  │  • analyze_patterns()   • boost_pattern_confidence()          │ │
│  └──────────────────────────┬──────────────────────────────────────┘ │
│                             │                                        │
│         ┌───────────────────┴────────────────────┐                  │
│         │                                        │                  │
│         ↓                                        ↓                  │
│  ┌──────────────────┐                   ┌────────────────────────┐ │
│  │  SQLite Layer    │                   │   ChromaDB Layer       │ │
│  │  (aiosqlite)     │                   │   (chromadb)           │ │
│  │                  │                   │                        │ │
│  │ • Async queries  │                   │ • Vector embeddings    │ │
│  │ • ACID compliant │                   │ • Similarity search    │ │
│  │ • Connection pool│                   │ • Semantic matching    │ │
│  │ • Auto-commit    │                   │ • 3 collections:       │ │
│  │ • Transactions   │                   │   - goal_embeddings    │ │
│  │                  │                   │   - pattern_embeddings │ │
│  │ 17 Tables:       │                   │   - context_embeddings │ │
│  │ • patterns       │                   │                        │ │
│  │ • actions        │                   │ Storage:               │ │
│  │ • goals          │                   │ • ~/.jarvis/learning/  │ │
│  │ • display_...    │                   │   chroma_embeddings/   │ │
│  │ • workspace_...  │                   │                        │ │
│  │ • temporal_...   │                   │                        │ │
│  │ • ...            │                   │                        │ │
│  └──────────────────┘                   └────────────────────────┘ │
│         │                                        │                  │
│         ↓                                        ↓                  │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                   Caching Layer                              │  │
│  │                                                              │  │
│  │  • AdaptiveCache: LRU + TTL (1000 entries, 3600s TTL)      │  │
│  │  • pattern_cache: Recently used patterns                    │  │
│  │  • goal_cache: Active goals                                 │  │
│  │  • embeddings_cache: Computed embeddings                    │  │
│  │  • query_cache: Frequent queries                            │  │
│  │                                                              │  │
│  │  Hit Rate: 85%+ (reduces DB queries by 85%)                │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │                   Storage Layer                              │  │
│  │                                                              │  │
│  │  Location: ~/.jarvis/learning/                              │  │
│  │                                                              │  │
│  │  Files:                                                      │  │
│  │  • jarvis_learning.db          (SQLite - main database)     │  │
│  │  • jarvis_learning.db-shm      (Shared memory)              │  │
│  │  • jarvis_learning.db-wal      (Write-ahead log)            │  │
│  │  • chroma_embeddings/          (ChromaDB directory)         │  │
│  │    └─ chroma.sqlite3           (Embeddings metadata)        │  │
│  │  • command_stats.json          (Legacy stats)               │  │
│  │  • success_patterns.json       (Legacy patterns)            │  │
│  └──────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

### Design Principles

1. **Separation of Concerns**
   - SQLite: Structured data, exact queries, time-series
   - ChromaDB: Semantic similarity, fuzzy matching, ML features

2. **Async-First**
   - All I/O operations are non-blocking
   - Uses `aiosqlite` for async SQLite access
   - No performance impact on main application

3. **Caching Strategy**
   - Multi-layer adaptive caching
   - LRU eviction policy
   - TTL-based expiration
   - Cache invalidation on updates

4. **Data Integrity**
   - ACID compliance via SQLite
   - Foreign key constraints
   - Automatic backups via WAL mode
   - Transaction support

5. **Performance Optimization**
   - Connection pooling
   - Batch inserts (100 records/batch)
   - Lazy loading
   - Auto-VACUUM
   - Indexed columns

---

## How It Works

### The Learning Cycle

```
┌──────────────────────────────────────────────────────────────────┐
│                      Complete Learning Flow                       │
└──────────────────────────────────────────────────────────────────┘

1️⃣  USER ACTION
    ↓
    User: "Connect to Living Room TV"
    Time: Wednesday, 8:00 PM

2️⃣  EXECUTION (UAE + SAI)
    ↓
    • UAE: Check for existing pattern
    • SAI: Detect UI position
    • Fusion: Combine context + detection
    • Execute: Click at position
    • Result: ✅ Success (0.5s)

3️⃣  PATTERN EXTRACTION
    ↓
    pattern_data = {
        'pattern_type': 'display_connection',
        'display_name': 'Living Room TV',
        'confidence': 0.85,
        'success': True,
        'execution_time': 0.5,
        'context': {
            'hour_of_day': 20,
            'day_of_week': 3,  # Wednesday
            'is_evening': True
        }
    }

4️⃣  DUAL STORAGE
    ↓
    A) SQLite Storage:
       ┌────────────────────────────────────┐
       │ INSERT INTO patterns (...)         │
       │ VALUES (                           │
       │   'pattern_001',                   │
       │   'display_connection',            │
       │   '{"display": "Living Room TV"}', │
       │   0.85,                            │
       │   1.0,                             │
       │   1,                               │
       │   '2025-10-23 20:00:00',          │
       │   ...                              │
       │ )                                  │
       └────────────────────────────────────┘

    B) ChromaDB Storage:
       ┌────────────────────────────────────┐
       │ collection.add(                    │
       │   ids=['pattern_001'],            │
       │   embeddings=[[0.23, -0.15, ...]], │
       │   metadatas=[{                     │
       │     'type': 'display_connection',  │
       │     'confidence': 0.85             │
       │   }]                               │
       │ )                                  │
       └────────────────────────────────────┘

    C) Cache Update:
       ┌────────────────────────────────────┐
       │ pattern_cache.set(                 │
       │   pattern_hash,                    │
       │   pattern_data                     │
       │ )                                  │
       └────────────────────────────────────┘

5️⃣  TEMPORAL PATTERN UPDATE
    ↓
    ┌────────────────────────────────────┐
    │ UPDATE temporal_patterns SET       │
    │   frequency = frequency + 1,       │
    │   avg_hour = 20,                   │
    │   day_pattern = day_pattern | 0x08 │
    │ WHERE                              │
    │   pattern_type = 'display' AND     │
    │   hour_of_day = 20 AND             │
    │   day_of_week = 3                  │
    └────────────────────────────────────┘

6️⃣  ACTION LOGGING
    ↓
    ┌────────────────────────────────────┐
    │ INSERT INTO actions (...)          │
    │ VALUES (                           │
    │   'action_001',                    │
    │   'click_element',                 │
    │   'Living Room TV',                │
    │   'goal_001',                      │
    │   0.85,                            │
    │   TRUE,                            │
    │   0.5,                             │
    │   '2025-10-23 20:00:00',          │
    │   ...                              │
    │ )                                  │
    └────────────────────────────────────┘

7️⃣  NEXT TIME (Pattern Recognition)
    ↓
    User: "Living Room TV" (next Wednesday, 8pm)

    A) Pattern Lookup (SQLite):
       ┌────────────────────────────────────┐
       │ SELECT * FROM patterns             │
       │ WHERE pattern_type = 'display'     │
       │   AND pattern_data LIKE '%Living%' │
       │ ORDER BY confidence DESC           │
       │ LIMIT 1                            │
       └────────────────────────────────────┘
       Result: pattern_001 (confidence: 0.85)

    B) Temporal Check (SQLite):
       ┌────────────────────────────────────┐
       │ SELECT frequency FROM temporal_... │
       │ WHERE hour_of_day = 20             │
       │   AND day_of_week = 3              │
       └────────────────────────────────────┘
       Result: frequency = 1

    C) Similarity Search (ChromaDB):
       ┌────────────────────────────────────┐
       │ collection.query(                  │
       │   query_embeddings=[embedding],    │
       │   n_results=5                      │
       │ )                                  │
       └────────────────────────────────────┘
       Result: pattern_001 (similarity: 0.95)

    D) Decision Fusion:
       ┌────────────────────────────────────┐
       │ Historical: 85% confidence         │
       │ Temporal: Match (Wed 8pm)          │
       │ Semantic: 95% similar              │
       │ → COMBINED: 92% confidence         │
       │ → USE CACHED POSITION             │
       └────────────────────────────────────┘

    E) Faster Execution:
       • No UI detection needed (cached)
       • Instant click (0.3s vs 0.5s)
       • 40% faster! ⚡

8️⃣  PATTERN REINFORCEMENT
    ↓
    ┌────────────────────────────────────┐
    │ UPDATE patterns SET                │
    │   occurrence_count = 2,            │
    │   confidence = 0.90,               │
    │   success_rate = 1.0,              │
    │   last_seen = NOW()                │
    │ WHERE pattern_id = 'pattern_001'   │
    └────────────────────────────────────┘

    Pattern gets stronger with each use! 📈

9️⃣  METRICS UPDATE
    ↓
    ┌────────────────────────────────────┐
    │ UPDATE learning_metrics SET        │
    │   total_patterns = total_patterns, │
    │   avg_confidence = 0.875,          │
    │   prediction_accuracy = 0.95,      │
    │   last_updated = NOW()             │
    └────────────────────────────────────┘

🔟  CONTINUOUS IMPROVEMENT
    ↓
    After 30 days:
    • Frequency: 30
    • Confidence: 98%
    • Temporal pattern: Strong (Wed 8pm)
    • Prediction: "User will connect to TV"
    • Proactive: Pre-validate position at 7:55pm
    • Result: Instant connection when user asks! ⚡
```

### Data Flow Diagram

```
┌─────────────┐
│ User Input  │
└──────┬──────┘
       │
       ↓
┌─────────────────────────────────────────┐
│ UAE (Unified Awareness Engine)          │
│                                         │
│ 1. Check Learning DB for patterns       │
│    ├─ Query SQLite (exact match)        │
│    └─ Query ChromaDB (semantic match)   │
│                                         │
│ 2. Get SAI real-time detection          │
│                                         │
│ 3. Fuse context + detection             │
│    ├─ Weight by confidence              │
│    ├─ Temporal validation               │
│    └─ Choose best source                │
└────────────┬────────────────────────────┘
             │
             ↓
┌─────────────────────────────────────────┐
│ Execute Action                          │
│ • Click UI element                      │
│ • Measure success/failure               │
│ • Record execution time                 │
└────────────┬────────────────────────────┘
             │
             ↓
┌─────────────────────────────────────────┐
│ Store Results (Learning Database)       │
│                                         │
│ SQLite:                                 │
│ ├─ patterns table (structured data)     │
│ ├─ actions table (execution log)        │
│ ├─ temporal_patterns (time-based)       │
│ └─ learning_metrics (aggregates)        │
│                                         │
│ ChromaDB:                               │
│ ├─ pattern_embeddings (vectors)         │
│ └─ context_embeddings (semantic)        │
│                                         │
│ Cache:                                  │
│ └─ pattern_cache (hot data)             │
└─────────────────────────────────────────┘
```

---

## Database Schema

### SQLite Tables

#### 1. **patterns** (Core Pattern Storage)

```sql
CREATE TABLE patterns (
    pattern_id TEXT PRIMARY KEY,           -- Unique pattern identifier
    pattern_type TEXT NOT NULL,            -- Type: display, workspace, temporal, etc.
    pattern_hash TEXT UNIQUE,              -- Hash for deduplication
    pattern_data JSON,                     -- Full pattern data (flexible schema)
    confidence REAL,                       -- Confidence score (0.0-1.0)
    success_rate REAL,                     -- Historical success rate
    occurrence_count INTEGER DEFAULT 1,     -- How many times seen
    first_seen TIMESTAMP,                  -- When first observed
    last_seen TIMESTAMP,                   -- When last used
    avg_execution_time REAL,               -- Average time to execute
    std_execution_time REAL,               -- Standard deviation
    decay_applied BOOLEAN DEFAULT 0,       -- Whether decay has been applied
    boost_count INTEGER DEFAULT 0,         -- Manual confidence boosts
    embedding_id TEXT,                     -- Link to ChromaDB embedding
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_patterns_type ON patterns(pattern_type);
CREATE INDEX idx_patterns_hash ON patterns(pattern_hash);
CREATE INDEX idx_patterns_confidence ON patterns(confidence);
CREATE INDEX idx_patterns_last_seen ON patterns(last_seen);
```

**Purpose:** Stores all learned patterns with ML metadata
**Size:** ~100-500 bytes per pattern
**Growth:** +5-20 patterns/day typical usage

#### 2. **actions** (Execution Log)

```sql
CREATE TABLE actions (
    action_id TEXT PRIMARY KEY,            -- Unique action identifier
    action_type TEXT NOT NULL,             -- Type: click, connect, execute, etc.
    target TEXT,                           -- Target of action (display name, app, etc.)
    goal_id TEXT,                          -- Associated goal (if any)
    confidence REAL,                       -- Confidence when executed
    success BOOLEAN,                       -- Whether action succeeded
    execution_time REAL,                   -- Time taken (seconds)
    timestamp TIMESTAMP,                   -- When executed
    retry_count INTEGER DEFAULT 0,         -- Number of retries
    error_message TEXT,                    -- Error if failed
    params JSON,                           -- Action parameters
    result JSON,                           -- Action result
    context_hash TEXT,                     -- Context when executed
    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
);

CREATE INDEX idx_actions_type ON actions(action_type);
CREATE INDEX idx_actions_timestamp ON actions(timestamp);
CREATE INDEX idx_actions_success ON actions(success);
CREATE INDEX idx_actions_context_hash ON actions(context_hash);
```

**Purpose:** Logs every action for analysis and learning
**Size:** ~200-400 bytes per action
**Growth:** +20-100 actions/day typical usage

#### 3. **goals** (Inferred User Goals)

```sql
CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY,              -- Unique goal identifier
    goal_type TEXT NOT NULL,               -- Type: connect_display, open_app, etc.
    goal_level TEXT NOT NULL,              -- Level: atomic, tactical, strategic
    description TEXT,                      -- Human-readable description
    confidence REAL,                       -- Confidence of inference
    progress REAL DEFAULT 0.0,             -- Completion progress (0.0-1.0)
    is_completed BOOLEAN DEFAULT 0,        -- Whether completed
    created_at TIMESTAMP,                  -- When goal inferred
    completed_at TIMESTAMP,                -- When completed
    predicted_duration REAL,               -- Predicted time to complete
    actual_duration REAL,                  -- Actual time taken
    evidence JSON,                         -- Evidence used for inference
    context_hash TEXT,                     -- Context when created
    embedding_id TEXT,                     -- Link to ChromaDB embedding
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_goals_type ON goals(goal_type);
CREATE INDEX idx_goals_created ON goals(created_at);
CREATE INDEX idx_goals_context_hash ON goals(context_hash);
```

**Purpose:** Stores inferred user goals for predictive behavior
**Size:** ~300-600 bytes per goal
**Growth:** +2-10 goals/day typical usage

#### 4. **display_patterns** (Display Connection History)

```sql
CREATE TABLE display_patterns (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,            -- Name of display
    context JSON,                          -- Context when connected
    context_hash TEXT,                     -- Hash of context
    connection_time TIME,                  -- Time of day
    day_of_week INTEGER,                   -- 0=Sunday, 6=Saturday
    hour_of_day INTEGER,                   -- 0-23
    frequency INTEGER DEFAULT 1,           -- Connection count
    auto_connect BOOLEAN DEFAULT 0,        -- Enable auto-connect?
    last_seen TIMESTAMP,                   -- Last connection
    consecutive_successes INTEGER DEFAULT 0, -- Success streak
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_display_patterns_name ON display_patterns(display_name);
CREATE INDEX idx_display_patterns_context ON display_patterns(context_hash);
CREATE INDEX idx_display_patterns_temporal ON display_patterns(hour_of_day, day_of_week);
```

**Purpose:** Tracks display connection patterns for proactive suggestions
**Size:** ~150-300 bytes per pattern
**Growth:** +1-5 patterns/week

#### 5. **workspace_usage** (macOS Space Usage Tracking)

```sql
CREATE TABLE workspace_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    space_id INTEGER NOT NULL,             -- macOS Space ID
    space_name TEXT,                       -- Space name (if labeled)
    app_name TEXT,                         -- Active app in space
    duration_seconds REAL,                 -- Time spent
    timestamp TIMESTAMP,                   -- When tracked
    window_count INTEGER,                  -- Number of windows
    is_fullscreen BOOLEAN,                 -- Fullscreen mode?
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_workspace_space ON workspace_usage(space_id);
CREATE INDEX idx_workspace_app ON workspace_usage(app_name);
CREATE INDEX idx_workspace_timestamp ON workspace_usage(timestamp);
```

**Purpose:** Learns workspace usage patterns via Yabai integration
**Size:** ~100-200 bytes per entry
**Growth:** +50-200 entries/day (if Yabai active)

#### 6. **app_usage_patterns** (Application Usage Analysis)

```sql
CREATE TABLE app_usage_patterns (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,                -- Application name
    space_id INTEGER,                      -- Preferred Space
    hour_of_day INTEGER,                   -- Typical time of use
    day_of_week INTEGER,                   -- Typical day
    frequency INTEGER DEFAULT 1,           -- Usage count
    avg_duration_minutes REAL,             -- Average session length
    last_used TIMESTAMP,                   -- Last used
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_app_usage_name ON app_usage_patterns(app_name);
CREATE INDEX idx_app_usage_temporal ON app_usage_patterns(hour_of_day, day_of_week);
```

**Purpose:** Learns which apps you use when/where
**Size:** ~120-250 bytes per pattern
**Growth:** +3-10 patterns/week

#### 7. **user_workflows** (Learned Action Sequences)

```sql
CREATE TABLE user_workflows (
    workflow_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name TEXT,                    -- Human-readable name
    action_sequence JSON,                  -- Ordered list of actions
    trigger_context JSON,                  -- What triggers this workflow
    confidence REAL,                       -- Confidence in sequence
    frequency INTEGER DEFAULT 1,           -- Times observed
    avg_duration_seconds REAL,             -- Average workflow duration
    last_seen TIMESTAMP,                   -- Last observed
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_workflows_name ON user_workflows(workflow_name);
CREATE INDEX idx_workflows_confidence ON user_workflows(confidence);
```

**Purpose:** Learns multi-step workflows (e.g., "Open Mail → Reply → Close")
**Size:** ~200-500 bytes per workflow
**Growth:** +1-3 workflows/week

#### 8. **space_transitions** (Space Switching Patterns)

```sql
CREATE TABLE space_transitions (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_space_id INTEGER,                 -- Source Space
    to_space_id INTEGER,                   -- Destination Space
    trigger_app TEXT,                      -- App that triggered switch
    frequency INTEGER DEFAULT 1,           -- Times observed
    avg_duration_seconds REAL,             -- How long in destination
    timestamp TIMESTAMP,                   -- When occurred
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_transitions_from ON space_transitions(from_space_id);
CREATE INDEX idx_transitions_to ON space_transitions(to_space_id);
```

**Purpose:** Learns Space navigation patterns
**Size:** ~80-150 bytes per transition
**Growth:** +30-100 transitions/day (if Yabai active)

#### 9. **behavioral_patterns** (ML-Detected Behavior Clusters)

```sql
CREATE TABLE behavioral_patterns (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_name TEXT,                     -- Auto-generated name
    cluster_id INTEGER,                    -- ML cluster ID
    feature_vector JSON,                   -- Features used for clustering
    confidence REAL,                       -- Cluster confidence
    occurrence_count INTEGER DEFAULT 1,    -- Times observed
    last_seen TIMESTAMP,                   -- Last occurrence
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_behavioral_cluster ON behavioral_patterns(cluster_id);
CREATE INDEX idx_behavioral_confidence ON behavioral_patterns(confidence);
```

**Purpose:** ML-discovered behavior patterns (unsupervised learning)
**Size:** ~150-400 bytes per pattern
**Growth:** +1-5 patterns/week (as ML learns)

#### 10. **temporal_patterns** (Time-Based Pattern Analysis)

```sql
CREATE TABLE temporal_patterns (
    pattern_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_type TEXT NOT NULL,            -- Type of temporal pattern
    hour_of_day INTEGER,                   -- Hour (0-23)
    day_of_week INTEGER,                   -- Day (0-6)
    week_of_month INTEGER,                 -- Week (1-5)
    month_of_year INTEGER,                 -- Month (1-12)
    frequency INTEGER DEFAULT 1,           -- Occurrence count
    avg_confidence REAL,                   -- Average confidence
    pattern_data JSON,                     -- Pattern specifics
    last_seen TIMESTAMP,                   -- Last occurrence
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_temporal_type ON temporal_patterns(pattern_type);
CREATE INDEX idx_temporal_time ON temporal_patterns(hour_of_day, day_of_week);
```

**Purpose:** Identifies time-based patterns (e.g., "Every Monday 9am")
**Size:** ~100-250 bytes per pattern
**Growth:** +5-15 patterns/week

#### 11. **proactive_suggestions** (AI-Generated Suggestions)

```sql
CREATE TABLE proactive_suggestions (
    suggestion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_type TEXT NOT NULL,         -- Type of suggestion
    suggestion_text TEXT,                  -- Human-readable suggestion
    trigger_context JSON,                  -- Context that triggered it
    confidence REAL,                       -- Confidence in suggestion
    is_accepted BOOLEAN,                   -- User accepted?
    is_dismissed BOOLEAN,                  -- User dismissed?
    created_at TIMESTAMP,                  -- When suggested
    responded_at TIMESTAMP,                -- When user responded
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_suggestions_type ON proactive_suggestions(suggestion_type);
CREATE INDEX idx_suggestions_created ON proactive_suggestions(created_at);
```

**Purpose:** Tracks proactive suggestions and user responses
**Size:** ~150-300 bytes per suggestion
**Growth:** +2-10 suggestions/week

#### 12. **user_preferences** (Learned Preferences)

```sql
CREATE TABLE user_preferences (
    preference_id TEXT PRIMARY KEY,        -- Unique identifier
    category TEXT NOT NULL,                -- Category (display, audio, etc.)
    key TEXT NOT NULL,                     -- Preference key
    value TEXT,                            -- Preference value
    confidence REAL,                       -- Confidence in preference
    learned_from TEXT,                     -- How it was learned
    update_count INTEGER DEFAULT 1,        -- Times updated
    created_at TIMESTAMP,                  -- When first learned
    updated_at TIMESTAMP,                  -- Last update
    UNIQUE(category, key)
);

CREATE INDEX idx_preferences_category ON user_preferences(category);
```

**Purpose:** Stores learned user preferences
**Size:** ~80-200 bytes per preference
**Growth:** +1-5 preferences/week

#### 13. **goal_action_mappings** (Goal → Action Relationships)

```sql
CREATE TABLE goal_action_mappings (
    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_type TEXT NOT NULL,               -- Type of goal
    action_type TEXT NOT NULL,             -- Action that achieves it
    success_count INTEGER DEFAULT 0,       -- Successful executions
    failure_count INTEGER DEFAULT 0,       -- Failed executions
    avg_execution_time REAL,               -- Average execution time
    std_execution_time REAL,               -- Standard deviation
    confidence REAL,                       -- Confidence in mapping
    last_updated TIMESTAMP,                -- Last update
    prediction_accuracy REAL,              -- How accurate predictions are
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_mappings_goal ON goal_action_mappings(goal_type);
CREATE INDEX idx_mappings_action ON goal_action_mappings(action_type);
```

**Purpose:** Maps goals to actions that achieve them
**Size:** ~120-250 bytes per mapping
**Growth:** +2-8 mappings/week

#### 14. **learning_metrics** (System Performance Tracking)

```sql
CREATE TABLE learning_metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_type TEXT NOT NULL,             -- Type of metric
    metric_value REAL,                     -- Value
    timestamp TIMESTAMP,                   -- When measured
    metadata JSON                          -- Additional context
);

CREATE INDEX idx_metrics_type ON learning_metrics(metric_type);
CREATE INDEX idx_metrics_timestamp ON learning_metrics(timestamp);
```

**Purpose:** Tracks learning system performance over time
**Size:** ~60-120 bytes per metric
**Growth:** +10-50 metrics/day

#### 15. **pattern_similarity_cache** (Pre-Computed Similarities)

```sql
CREATE TABLE pattern_similarity_cache (
    cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id_1 TEXT NOT NULL,            -- First pattern
    pattern_id_2 TEXT NOT NULL,            -- Second pattern
    similarity_score REAL,                 -- Similarity (0.0-1.0)
    computed_at TIMESTAMP,                 -- When computed
    UNIQUE(pattern_id_1, pattern_id_2)
);

CREATE INDEX idx_similarity_p1 ON pattern_similarity_cache(pattern_id_1);
CREATE INDEX idx_similarity_p2 ON pattern_similarity_cache(pattern_id_2);
```

**Purpose:** Caches expensive similarity computations
**Size:** ~60-100 bytes per entry
**Growth:** +10-30 entries/day

#### 16. **context_embeddings** (Context Vector Metadata)

```sql
CREATE TABLE context_embeddings (
    embedding_id TEXT PRIMARY KEY,         -- Unique embedding ID
    context_type TEXT,                     -- Type of context
    embedding_vector BLOB,                 -- Serialized numpy array
    created_at TIMESTAMP,                  -- When created
    metadata JSON                          -- Additional metadata
);

CREATE INDEX idx_embeddings_type ON context_embeddings(context_type);
```

**Purpose:** Stores context embeddings metadata (vectors in ChromaDB)
**Size:** ~300-1000 bytes per embedding (vector stored in ChromaDB)
**Growth:** +5-20 embeddings/day

### ChromaDB Collections

#### 1. **goal_embeddings**

```python
{
    "name": "goal_embeddings",
    "metadata": {
        "description": "Goal context embeddings for similarity search",
        "hnsw:space": "cosine"  # Cosine similarity
    }
}
```

**Stores:** Vector embeddings of goal contexts
**Dimension:** 384 (default sentence-transformer)
**Usage:** Find similar goals when user has new objective

#### 2. **pattern_embeddings**

```python
{
    "name": "pattern_embeddings",
    "metadata": {
        "description": "Pattern embeddings for matching",
        "hnsw:space": "cosine"
    }
}
```

**Stores:** Vector embeddings of learned patterns
**Dimension:** 384
**Usage:** Find similar patterns for new situations

#### 3. **context_embeddings**

```python
{
    "name": "context_embeddings",
    "metadata": {
        "description": "Context state embeddings for prediction",
        "hnsw:space": "cosine"
    }
}
```

**Stores:** Vector embeddings of system/user context
**Dimension:** 384
**Usage:** Predict actions based on current context

---

## Purpose & Role

### What Problem Does It Solve?

#### Before Learning Database:

```
User: "Connect to Living Room TV"
JARVIS:
  ├─ Searches for Control Center (2s)
  ├─ OCR detection (1.5s)
  ├─ Clicks wrong button (retry)
  ├─ Finally succeeds (5s total)
  └─ ❌ FORGETS EVERYTHING on restart

Next day:
  └─ Same slow process again (5s)
```

#### After Learning Database:

```
Day 1:
User: "Connect to Living Room TV"
JARVIS:
  ├─ Searches + detects (3s)
  ├─ Succeeds
  └─ ✅ STORES pattern to database

Day 2:
User: "Living Room TV"
JARVIS:
  ├─ Retrieves pattern from DB (0.1s)
  ├─ Knows exact position
  ├─ Instant click (1s total)
  └─ ✅ 80% faster!

Day 30:
Wednesday 7:55pm:
JARVIS:
  ├─ Predicts user will connect at 8pm
  ├─ Pre-validates position
  └─ Ready instantly when user asks (0.5s)
  └─ ✅ 90% faster + proactive!
```

### Core Capabilities

| Capability | Without Learning DB | With Learning DB |
|------------|-------------------|------------------|
| **Memory** | Session-only | Persistent forever |
| **Speed** | Slow every time | Gets faster with use |
| **Prediction** | Reactive only | Proactive suggestions |
| **Adaptation** | Manual tuning | Auto-learns patterns |
| **Intelligence** | Rule-based | ML-powered |
| **Generalization** | Exact match only | Semantic similarity |
| **Temporal Awareness** | No time patterns | Learns time-based behavior |
| **Cross-Session** | Starts from scratch | Continuous learning |

### Key Roles in JARVIS

#### 1. **Memory Layer**
- Remembers every interaction
- Persists across restarts
- Never forgets learned patterns

#### 2. **Learning Engine**
- Analyzes patterns automatically
- Discovers temporal correlations
- Clusters similar behaviors

#### 3. **Prediction Engine**
- Forecasts user actions
- Pre-validates UI positions
- Suggests proactive actions

#### 4. **Performance Optimizer**
- Caches frequently used patterns
- Reduces UI detection overhead
- Speeds up execution over time

#### 5. **Intelligence Foundation**
- Provides historical context to UAE
- Enables semantic search via ChromaDB
- Powers ML-based decision making

---

## Integration with UAE + SAI

### How They Work Together

```
┌──────────────────────────────────────────────────────────────┐
│                     Intelligence Stack                        │
│                                                               │
│  User Input: "Connect to Living Room TV"                     │
│                                                               │
│  Step 1: UAE Context Layer                                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ • Query Learning DB for "Living Room TV" pattern       │  │
│  │                                                        │  │
│  │ SQLite Query:                                          │  │
│  │   SELECT * FROM display_patterns                       │  │
│  │   WHERE display_name LIKE '%Living Room TV%'          │  │
│  │                                                        │  │
│  │ Result:                                                │  │
│  │   - pattern_id: display_001                           │  │
│  │   - frequency: 30                                     │  │
│  │   - confidence: 0.95                                  │  │
│  │   - hour_of_day: 20 (8pm)                            │  │
│  │   - day_of_week: 3 (Wednesday)                       │  │
│  │   - consecutive_successes: 30                         │  │
│  │                                                        │  │
│  │ ChromaDB Query:                                        │  │
│  │   collection.query(                                    │  │
│  │     query_text="Living Room TV",                      │  │
│  │     n_results=3                                       │  │
│  │   )                                                    │  │
│  │                                                        │  │
│  │ Result:                                                │  │
│  │   - "Living Room TV" (similarity: 1.0)               │  │
│  │   - "LG Monitor" (similarity: 0.72)                  │  │
│  │   - "Samsung Display" (similarity: 0.68)             │  │
│  │                                                        │  │
│  │ UAE Decision:                                          │  │
│  │   ✅ Strong historical pattern                        │  │
│  │   ✅ Temporal match (Wednesday 8pm)                   │  │
│  │   ✅ High confidence (95%)                            │  │
│  │   → Confidence: 95%                                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                           ↓                                   │
│  Step 2: SAI Situational Layer                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ • Real-time UI detection                               │  │
│  │                                                        │  │
│  │ Detection:                                             │  │
│  │   - Control Center location: (1235, 10)               │  │
│  │   - Display button visible: Yes                       │  │
│  │   - Confidence: 85%                                   │  │
│  │                                                        │  │
│  │ SAI Decision:                                          │  │
│  │   ✅ UI element detected                              │  │
│  │   ✅ Position validated                               │  │
│  │   → Confidence: 85%                                   │  │
│  └────────────────────────────────────────────────────────┘  │
│                           ↓                                   │
│  Step 3: UAE Decision Fusion                                 │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ • Combine Context (95%) + SAI (85%)                    │  │
│  │                                                        │  │
│  │ Fusion Logic:                                          │  │
│  │   context_weight = 0.6  (historical is reliable)      │  │
│  │   sai_weight = 0.4      (real-time validation)        │  │
│  │                                                        │  │
│  │   combined = (0.95 × 0.6) + (0.85 × 0.4)             │  │
│  │            = 0.57 + 0.34                              │  │
│  │            = 0.91 (91% confidence)                    │  │
│  │                                                        │  │
│  │ Decision:                                              │  │
│  │   ✅ Use cached position from Learning DB             │  │
│  │   ✅ Validated by SAI real-time check                 │  │
│  │   ✅ High confidence → Execute immediately            │  │
│  └────────────────────────────────────────────────────────┘  │
│                           ↓                                   │
│  Step 4: Execution                                           │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ • Click at (1235, 10)                                  │  │
│  │ • Success: ✅                                          │  │
│  │ • Execution time: 0.3s (vs 5s on Day 1)               │  │
│  └────────────────────────────────────────────────────────┘  │
│                           ↓                                   │
│  Step 5: Feedback to Learning DB                             │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Update pattern:                                         │  │
│  │   UPDATE display_patterns SET                           │  │
│  │     frequency = 31,                                     │  │
│  │     consecutive_successes = 31,                         │  │
│  │     confidence = 0.96,                                  │  │
│  │     last_seen = NOW()                                   │  │
│  │   WHERE pattern_id = 'display_001';                     │  │
│  │                                                         │  │
│  │ Log action:                                             │  │
│  │   INSERT INTO actions (...) VALUES (                    │  │
│  │     'action_031',                                       │  │
│  │     'click_element',                                    │  │
│  │     'Living Room TV',                                   │  │
│  │     0.91,                                               │  │
│  │     TRUE,                                               │  │
│  │     0.3,                                                │  │
│  │     NOW()                                               │  │
│  │   );                                                    │  │
│  │                                                         │  │
│  │ Pattern gets even stronger! 📈                         │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Integration Points

| Component | Reads From Learning DB | Writes To Learning DB |
|-----------|----------------------|---------------------|
| **UAE Context Layer** | ✅ Patterns, goals, temporal | ✅ New patterns, updates |
| **SAI** | ✅ Historical UI positions | ✅ UI change detections |
| **Memory Quantizer** | ✅ Memory patterns | ✅ Memory tier changes |
| **System Monitor** | ✅ Health patterns | ✅ Health changes, anomalies |
| **Yabai Integration** | ✅ Workspace patterns | ✅ Space transitions |
| **Goal Inference** | ✅ Goal history | ✅ New inferred goals |
| **Adaptive Clicker** | ✅ Element positions | ✅ Click results |

### Data Flow Example

```
┌─────────────────────────────────────────────────────────────┐
│ Complete Data Flow: User Connects to Display                │
└─────────────────────────────────────────────────────────────┘

Time: Wednesday, 8:00 PM
User: "Connect to Living Room TV"

┌───────────────────────────────────────────────────────────┐
│ 1. UAE Context Layer Queries Learning DB                  │
└───────────────────────────────────────────────────────────┘
   ↓
   SQLite: "SELECT * FROM display_patterns WHERE ..."
   Result: {
     pattern_id: "display_001",
     display_name: "Living Room TV",
     frequency: 30,
     confidence: 0.95,
     hour_of_day: 20,
     day_of_week: 3,
     consecutive_successes: 30
   }

   ChromaDB: "Query similar display patterns"
   Result: [
     {id: "display_001", similarity: 1.0},
     {id: "display_002", similarity: 0.72}  // "LG Monitor"
   ]

   UAE Prediction: "Strong pattern, high confidence"

┌───────────────────────────────────────────────────────────┐
│ 2. SAI Real-Time Detection                                │
└───────────────────────────────────────────────────────────┘
   ↓
   Vision: "Detect Control Center position"
   Result: (1235, 10) with 85% confidence

   SAI Validation: "Position matches historical pattern"

┌───────────────────────────────────────────────────────────┐
│ 3. UAE Decision Fusion                                     │
└───────────────────────────────────────────────────────────┘
   ↓
   Historical: 95% (Learning DB)
   Real-time: 85% (SAI)
   Combined: 91% confidence

   Decision: "Use cached position, execute immediately"

┌───────────────────────────────────────────────────────────┐
│ 4. Execution via Adaptive Clicker                         │
└───────────────────────────────────────────────────────────┘
   ↓
   Click: (1235, 10)
   Result: ✅ Success
   Time: 0.3s

┌───────────────────────────────────────────────────────────┐
│ 5. Feedback Loop → Learning DB                            │
└───────────────────────────────────────────────────────────┘
   ↓
   A) Update Pattern (SQLite):
      UPDATE display_patterns SET
        frequency = 31,
        consecutive_successes = 31,
        confidence = 0.96,
        last_seen = '2025-10-23 20:00:00'
      WHERE pattern_id = 'display_001';

   B) Log Action (SQLite):
      INSERT INTO actions VALUES (
        'action_031',
        'click_element',
        'Living Room TV',
        NULL,
        0.91,
        TRUE,
        0.3,
        '2025-10-23 20:00:00',
        ...
      );

   C) Update Temporal Pattern (SQLite):
      UPDATE temporal_patterns SET
        frequency = frequency + 1
      WHERE
        pattern_type = 'display_connection' AND
        hour_of_day = 20 AND
        day_of_week = 3;

   D) Update ChromaDB Embedding:
      collection.update(
        ids=['display_001'],
        metadatas=[{'confidence': 0.96}]
      );

   E) Update Cache:
      pattern_cache.set('display_001', updated_pattern)

   F) Update Metrics (SQLite):
      INSERT INTO learning_metrics VALUES (
        NULL,
        'execution_success',
        1.0,
        '2025-10-23 20:00:00',
        '{"execution_time": 0.3}'
      );

┌───────────────────────────────────────────────────────────┐
│ 6. Memory Quantizer Logs System State                     │
└───────────────────────────────────────────────────────────┘
   ↓
   Current Memory: 31.7% (macOS true pressure)
   Tier: abundant

   Store Pattern (SQLite):
   INSERT INTO patterns VALUES (
     'memory_pattern_001',
     'memory_usage',
     ...,
     '{"tier": "abundant", "pressure": 31.7}',
     ...
   );

┌───────────────────────────────────────────────────────────┐
│ 7. Goal Inference Updates                                 │
└───────────────────────────────────────────────────────────┘
   ↓
   Inferred Goal: "User wants to watch TV"
   Confidence: 0.88

   Store Goal (SQLite):
   INSERT INTO goals VALUES (
     'goal_031',
     'entertainment',
     'tactical',
     'Watch TV in living room',
     0.88,
     1.0,  // Completed
     TRUE,
     '2025-10-23 20:00:00',
     '2025-10-23 20:00:30',
     ...
   );

   Link Action to Goal:
   UPDATE actions SET
     goal_id = 'goal_031'
   WHERE action_id = 'action_031';

┌───────────────────────────────────────────────────────────┐
│ Result: Smarter Next Time! 🧠                             │
└───────────────────────────────────────────────────────────┘
   ↓
   Pattern now even stronger:
   • Frequency: 31
   • Confidence: 96%
   • Consecutive successes: 31
   • Temporal correlation: Strong (Wed 8pm)

   Next Wednesday at 7:55pm:
   → UAE will proactively suggest: "Connect to Living Room TV?"
   → Or auto-connect if user enabled that feature
```

---

## Test Scenarios

### 1. **Fresh Start (No Historical Data)**

**Scenario:** First time JARVIS runs after database is created

**Expected Behavior:**
```python
# Initial state
patterns_count = 0
actions_count = 0
confidence = 0.5 (default)

# User: "Connect to Living Room TV"
# Result: Slow (3-5s) - full UI detection needed
# After execution:
patterns_count = 1
actions_count = 1
display_patterns = 1
confidence = 0.6 (learned from first success)
```

**Validation:**
```bash
sqlite3 ~/.jarvis/learning/jarvis_learning.db << EOF
SELECT COUNT(*) FROM patterns;
SELECT COUNT(*) FROM actions;
SELECT COUNT(*) FROM display_patterns;
SELECT confidence FROM display_patterns WHERE display_name = 'Living Room TV';
EOF
```

**Expected Output:**
```
1     # patterns
1     # actions
1     # display_patterns
0.6   # confidence
```

### 2. **Pattern Recognition (After 5 Uses)**

**Scenario:** User has connected to same display 5 times

**Expected Behavior:**
```python
# State after 5 connections
patterns_count = 1
frequency = 5
confidence = 0.78
consecutive_successes = 5
avg_execution_time < initial_time  # Faster over time

# User: "Living Room TV" (6th time)
# Expected:
# - Pattern retrieved from cache (0.01s)
# - SAI validates position (0.5s)
# - Execution faster (1.5s total vs 3-5s initially)
# - Confidence increases to 0.82
```

**Validation:**
```python
async def test_pattern_recognition():
    from intelligence.learning_database import get_learning_database

    db = await get_learning_database()
    await db.initialize()

    # Query pattern
    patterns = await db.get_pattern_by_type('display_connection', limit=10)

    assert len(patterns) > 0, "No patterns found"

    pattern = patterns[0]
    assert pattern['occurrence_count'] >= 5, "Not enough occurrences"
    assert pattern['confidence'] > 0.7, "Confidence too low"
    assert pattern['success_rate'] == 1.0, "Should have 100% success rate"

    print(f"✅ Pattern recognition working:")
    print(f"   Occurrences: {pattern['occurrence_count']}")
    print(f"   Confidence: {pattern['confidence']:.2f}")
    print(f"   Success rate: {pattern['success_rate']:.2f}")
```

### 3. **Temporal Pattern Learning (Weekly Pattern)**

**Scenario:** User connects to TV every Wednesday at 8pm for 4 weeks

**Expected Behavior:**
```python
# After 4 weeks (4 connections)
temporal_pattern = {
    'hour_of_day': 20,
    'day_of_week': 3,
    'frequency': 4,
    'confidence': 0.85
}

# On 5th Wednesday at 7:55pm:
# UAE should predict: "User will likely connect to TV soon"
# Proactive: Pre-validate UI position
# Result: Instant execution when user asks at 8pm
```

**Validation:**
```bash
sqlite3 ~/.jarvis/learning/jarvis_learning.db << EOF
SELECT
    hour_of_day,
    day_of_week,
    frequency,
    AVG(frequency) OVER (PARTITION BY hour_of_day, day_of_week)
FROM temporal_patterns
WHERE pattern_type = 'display_connection'
  AND hour_of_day = 20
  AND day_of_week = 3;
EOF
```

### 4. **Semantic Similarity (New Display)**

**Scenario:** User says "Samsung Monitor" (never connected before) but has connected to "LG Monitor" many times

**Expected Behavior:**
```python
# ChromaDB finds semantic similarity
similar_patterns = [
    {'name': 'LG Monitor', 'similarity': 0.78},
    {'name': 'Dell Display', 'similarity': 0.72}
]

# UAE: "Never connected to Samsung, but LG Monitor is similar"
# Decision: Use similar pattern as starting point
# Result: Faster than fresh start (2s vs 5s)
```

**Test:**
```python
async def test_semantic_similarity():
    db = await get_learning_database()

    # Store LG Monitor pattern
    await db.learn_display_pattern('LG Monitor', {'type': 'external'})

    # Query for Samsung Monitor (doesn't exist)
    # ChromaDB should find LG Monitor as similar
    results = db.pattern_collection.query(
        query_texts=['Samsung Monitor'],
        n_results=3
    )

    assert len(results['ids'][0]) > 0, "No similar patterns found"
    assert 'LG Monitor' in str(results), "Expected LG Monitor in results"

    print(f"✅ Semantic similarity working:")
    print(f"   Found {len(results['ids'][0])} similar patterns")
```

### 5. **Cache Performance**

**Scenario:** Frequently accessed pattern should be cached

**Expected Behavior:**
```python
# First access: Cache miss (query DB)
# Subsequent 100 accesses: Cache hit (no DB query)
# Cache hit rate: >85%

metrics = {
    'cache_hits': 95,
    'cache_misses': 5,
    'hit_rate': 0.95
}
```

**Test:**
```python
async def test_cache_performance():
    db = await get_learning_database()

    # Store pattern
    pattern_id = await db.store_pattern({
        'pattern_type': 'test',
        'pattern_data': {'test': 'data'},
        'confidence': 0.8
    })

    # Access 100 times
    for _ in range(100):
        await db.get_pattern_by_type('test', limit=1)

    # Check cache stats
    cache_stats = db.pattern_cache.get_stats()
    hit_rate = cache_stats['hits'] / (cache_stats['hits'] + cache_stats['misses'])

    assert hit_rate > 0.85, f"Cache hit rate too low: {hit_rate:.2f}"

    print(f"✅ Cache performance:")
    print(f"   Hit rate: {hit_rate:.2%}")
    print(f"   Hits: {cache_stats['hits']}")
    print(f"   Misses: {cache_stats['misses']}")
```

### 6. **Pattern Merging (Duplicate Detection)**

**Scenario:** User connects to "Living Room TV" and "living room tv" (different casing)

**Expected Behavior:**
```python
# First: "Living Room TV" → pattern_001
# Second: "living room tv" → Detected as duplicate via hash
# Result: Merged into pattern_001, frequency = 2
# NOT: Two separate patterns

patterns_count = 1  # Not 2!
pattern_001.frequency = 2
```

**Test:**
```python
async def test_pattern_merging():
    db = await get_learning_database()

    # Store first pattern
    await db.learn_display_pattern('Living Room TV', {})

    # Store duplicate (different casing)
    await db.learn_display_pattern('living room tv', {})

    # Check that only 1 pattern exists
    patterns = await db.get_pattern_by_type('display_connection', limit=10)

    assert len(patterns) == 1, f"Expected 1 pattern, got {len(patterns)}"
    assert patterns[0]['occurrence_count'] == 2, "Frequency should be 2"

    print(f"✅ Pattern merging working:")
    print(f"   Patterns: {len(patterns)}")
    print(f"   Frequency: {patterns[0]['occurrence_count']}")
```

### 7. **Data Persistence (Restart Test)**

**Scenario:** Store pattern, restart JARVIS, verify pattern still exists

**Expected Behavior:**
```python
# Before restart
patterns_count = 10
display_patterns = 5

# Restart JARVIS
# (Database files remain on disk)

# After restart
patterns_count = 10  # Same!
display_patterns = 5  # Persisted!
```

**Manual Test:**
```bash
# Store data
python -c "
import asyncio
from backend.intelligence.learning_database import get_learning_database

async def main():
    db = await get_learning_database()
    await db.initialize()
    await db.learn_display_pattern('Test Display', {})
    await db.close()
    print('✅ Pattern stored')

asyncio.run(main())
"

# Check database
sqlite3 ~/.jarvis/learning/jarvis_learning.db \
  "SELECT COUNT(*) FROM display_patterns WHERE display_name = 'Test Display';"
# Should output: 1

# Restart Python (simulates JARVIS restart)
python -c "
import asyncio
from backend.intelligence.learning_database import get_learning_database

async def main():
    db = await get_learning_database()
    await db.initialize()

    # Query should find the pattern
    import aiosqlite
    async with db.db.execute(
        'SELECT * FROM display_patterns WHERE display_name = ?',
        ('Test Display',)
    ) as cursor:
        row = await cursor.fetchone()
        assert row is not None, 'Pattern not found after restart!'
        print('✅ Pattern persisted across restart')

    await db.close()

asyncio.run(main())
"
```

### 8. **Performance Under Load**

**Scenario:** Store 1000 patterns rapidly

**Expected Behavior:**
```python
# Batch insert mode activated
# Writes buffered and committed in batches of 100
# Total time: <2s for 1000 patterns
# Average: <2ms per pattern
```

**Test:**
```python
import time
import asyncio

async def test_bulk_insert():
    db = await get_learning_database()

    start = time.time()

    # Store 1000 patterns
    for i in range(1000):
        await db.store_pattern({
            'pattern_type': 'load_test',
            'pattern_data': {'index': i},
            'confidence': 0.5 + (i / 2000)  # Increasing confidence
        })

    elapsed = time.time() - start
    avg_time = (elapsed / 1000) * 1000  # ms per pattern

    assert elapsed < 5.0, f"Bulk insert too slow: {elapsed:.2f}s"

    print(f"✅ Bulk insert performance:")
    print(f"   Total time: {elapsed:.2f}s")
    print(f"   Avg per pattern: {avg_time:.2f}ms")
    print(f"   Patterns/sec: {1000/elapsed:.0f}")

asyncio.run(test_bulk_insert())
```

### 9. **Confidence Decay (Old Patterns)**

**Scenario:** Pattern not used for 30 days should have reduced confidence

**Expected Behavior:**
```python
# Day 1: confidence = 0.9
# Day 30: (if not used) confidence = 0.9 * 0.95 = 0.855
# Day 60: confidence = 0.855 * 0.95 = 0.812
# Decay factor: 0.95 (5% reduction if not used)
```

**Test:**
```python
async def test_confidence_decay():
    db = await get_learning_database()

    # Store pattern with high confidence
    pattern_id = await db.store_pattern({
        'pattern_type': 'test_decay',
        'pattern_data': {'test': 'data'},
        'confidence': 0.9
    })

    # Simulate 30 days passing (update last_seen manually)
    async with db.db.execute(
        "UPDATE patterns SET last_seen = datetime('now', '-30 days') WHERE pattern_id = ?",
        (pattern_id,)
    ) as cursor:
        await db.db.commit()

    # Run cleanup (applies decay)
    await db.cleanup_old_patterns(days=30)

    # Check confidence decreased
    patterns = await db.get_pattern_by_type('test_decay', limit=1)
    new_confidence = patterns[0]['confidence']

    assert new_confidence < 0.9, "Confidence should have decayed"
    expected_confidence = 0.9 * 0.95  # One decay cycle
    assert abs(new_confidence - expected_confidence) < 0.01, \
        f"Unexpected confidence: {new_confidence} (expected ~{expected_confidence})"

    print(f"✅ Confidence decay working:")
    print(f"   Original: 0.90")
    print(f"   After 30 days: {new_confidence:.3f}")
```

### 10. **Concurrent Access (Thread Safety)**

**Scenario:** Multiple components writing to DB simultaneously

**Expected Behavior:**
```python
# 10 concurrent writes
# All succeed without corruption
# No race conditions
# All data written correctly
```

**Test:**
```python
async def test_concurrent_access():
    db = await get_learning_database()

    async def write_pattern(index):
        await db.store_pattern({
            'pattern_type': 'concurrent_test',
            'pattern_data': {'index': index},
            'confidence': 0.5
        })

    # Run 10 concurrent writes
    await asyncio.gather(*[
        write_pattern(i) for i in range(10)
    ])

    # Verify all 10 patterns stored
    patterns = await db.get_pattern_by_type('concurrent_test', limit=20)

    assert len(patterns) == 10, f"Expected 10 patterns, got {len(patterns)}"

    # Verify all indices present (no data loss)
    indices = [p['pattern_data']['index'] for p in patterns]
    assert set(indices) == set(range(10)), "Missing or duplicate patterns"

    print(f"✅ Concurrent access working:")
    print(f"   Patterns stored: {len(patterns)}")
    print(f"   No data loss: ✅")
```

---

## Edge Cases

### 1. **Database Corruption**

**Scenario:** SQLite file gets corrupted (power loss, disk error)

**Problem:**
```
sqlite3.DatabaseError: database disk image is malformed
```

**Solution:**
```bash
# Automatic recovery via WAL mode
# WAL (Write-Ahead Logging) provides atomic commits

# If corruption detected, JARVIS should:
# 1. Backup corrupted DB
cp ~/.jarvis/learning/jarvis_learning.db \
   ~/.jarvis/learning/jarvis_learning.db.corrupted_$(date +%Y%m%d)

# 2. Attempt recovery
sqlite3 ~/.jarvis/learning/jarvis_learning.db ".recover" | \
  sqlite3 ~/.jarvis/learning/jarvis_learning.db.recovered

# 3. If recovery fails, create fresh DB
# (Lose historical data but JARVIS continues working)
```

**Prevention:**
```python
# In learning_database.py
async def _init_sqlite(self):
    # Enable WAL mode for crash recovery
    await self.db.execute("PRAGMA journal_mode=WAL")

    # Enable foreign keys for data integrity
    await self.db.execute("PRAGMA foreign_keys=ON")

    # Regular integrity checks
    await self.db.execute("PRAGMA integrity_check")
```

**Monitoring:**
```python
async def check_db_health(self):
    """Check database health and repair if needed"""
    try:
        result = await self.db.execute("PRAGMA integrity_check")
        row = await result.fetchone()

        if row[0] != 'ok':
            logger.error(f"Database corruption detected: {row[0]}")
            await self._attempt_recovery()
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
```

### 2. **Disk Full**

**Scenario:** Database tries to write but disk is full

**Problem:**
```
sqlite3.OperationalError: database or disk is full
```

**Solution:**
```python
async def store_pattern(self, pattern):
    try:
        # Attempt write
        await self._write_pattern(pattern)
    except sqlite3.OperationalError as e:
        if "disk is full" in str(e):
            logger.error("Disk full! Cleaning up old patterns...")

            # Emergency cleanup: Delete old patterns
            await self.cleanup_old_patterns(days=7)  # More aggressive

            # Try again
            await self._write_pattern(pattern)
        else:
            raise
```

**Prevention:**
```python
async def check_disk_space(self):
    """Check if enough disk space before writes"""
    import shutil

    stats = shutil.disk_usage(self.db_dir)
    free_gb = stats.free / (1024**3)

    if free_gb < 0.5:  # Less than 500MB free
        logger.warning(f"Low disk space: {free_gb:.2f}GB free")
        # Trigger cleanup
        await self.cleanup_old_patterns(days=15)
```

### 3. **ChromaDB Unavailable**

**Scenario:** ChromaDB fails to initialize (dependency issue, incompatible version)

**Problem:**
```
ImportError: cannot import name 'Settings' from 'chromadb.config'
```

**Solution:**
```python
# Graceful fallback to SQLite-only mode
try:
    import chromadb
    self.chroma_client = chromadb.PersistentClient(...)
    self.pattern_collection = self.chroma_client.get_or_create_collection(...)
    logger.info("ChromaDB initialized")
except ImportError:
    logger.warning("ChromaDB not available - using SQLite-only mode")
    self.chroma_client = None
    self.pattern_collection = None
except Exception as e:
    logger.error(f"ChromaDB init failed: {e} - fallback to SQLite-only")
    self.chroma_client = None
    self.pattern_collection = None

# All methods check if ChromaDB available
async def _find_similar_patterns(self, embedding, pattern_type):
    if not self.pattern_collection:
        logger.debug("ChromaDB not available, skipping similarity search")
        return []  # Fallback: no similar patterns

    # Normal ChromaDB logic
    ...
```

**Impact:**
- ✅ JARVIS still works (SQLite functional)
- ❌ No semantic similarity search
- ❌ No fuzzy matching
- ℹ️ Only exact pattern matches

### 4. **Extremely Large Database (>1GB)**

**Scenario:** After months of use, database grows very large

**Problem:**
```
# Performance degradation
# Slow queries
# High memory usage
```

**Solution:**
```python
# Automatic optimization
async def auto_optimize(self):
    """Optimize database when it gets large"""
    # Check size
    db_size_mb = os.path.getsize(self.sqlite_path) / (1024**2)

    if db_size_mb > 500:  # Over 500MB
        logger.info(f"Database large ({db_size_mb:.1f}MB), optimizing...")

        # 1. Delete very old patterns (>90 days)
        await self.cleanup_old_patterns(days=90)

        # 2. Vacuum to reclaim space
        await self.db.execute("VACUUM")

        # 3. Analyze for query optimization
        await self.db.execute("ANALYZE")

        # 4. Clear old action logs
        await self.db.execute("""
            DELETE FROM actions
            WHERE timestamp < datetime('now', '-60 days')
        """)

        new_size = os.path.getsize(self.sqlite_path) / (1024**2)
        logger.info(f"Optimization complete: {db_size_mb:.1f}MB → {new_size:.1f}MB")
```

**Archiving Strategy:**
```python
async def archive_old_data(self, archive_path):
    """Archive old data to separate database"""
    # Create archive DB
    archive_db = await aiosqlite.connect(archive_path)

    # Attach archive DB to main DB
    await self.db.execute(f"ATTACH DATABASE '{archive_path}' AS archive")

    # Move old patterns to archive
    await self.db.execute("""
        INSERT INTO archive.patterns
        SELECT * FROM main.patterns
        WHERE last_seen < datetime('now', '-180 days')
    """)

    # Delete from main DB
    await self.db.execute("""
        DELETE FROM main.patterns
        WHERE last_seen < datetime('now', '-180 days')
    """)

    await self.db.execute("DETACH DATABASE archive")

    logger.info(f"Old data archived to {archive_path}")
```

### 5. **Conflicting Patterns (Different Contexts)**

**Scenario:** Same display name but different connection methods

**Example:**
```
Context A: "Living Room TV" via AirPlay → Position (1235, 10)
Context B: "Living Room TV" via HDMI → Position (1240, 50)
```

**Problem:**
```
# Both patterns stored with same display_name
# UAE confused: Which position to use?
```

**Solution:**
```python
# Use context_hash to differentiate
async def learn_display_pattern(self, display_name, context):
    context_hash = self._hash_context(context)

    # Check for existing pattern with same context
    async with self.db.execute("""
        SELECT * FROM display_patterns
        WHERE display_name = ? AND context_hash = ?
    """, (display_name, context_hash)) as cursor:
        existing = await cursor.fetchone()

    if existing:
        # Update existing pattern
        await self._update_pattern(existing['pattern_id'])
    else:
        # New pattern (different context)
        await self._insert_pattern(display_name, context, context_hash)
```

**Context-Aware Retrieval:**
```python
async def get_display_pattern(self, display_name, current_context):
    # Calculate current context hash
    context_hash = self._hash_context(current_context)

    # Try exact match first (same context)
    pattern = await self._get_pattern_by_context(display_name, context_hash)

    if pattern:
        return pattern  # Exact match!

    # Fallback: Find most similar context
    all_patterns = await self._get_all_patterns(display_name)
    best_match = self._find_most_similar_context(all_patterns, current_context)

    return best_match
```

### 6. **Race Condition (Pattern Update)**

**Scenario:** Two components try to update same pattern simultaneously

**Problem:**
```
Thread 1: Read pattern (confidence: 0.8)
Thread 2: Read pattern (confidence: 0.8)
Thread 1: Update confidence to 0.85
Thread 2: Update confidence to 0.82  ← Overwrites Thread 1's update!
Result: confidence = 0.82 (should be 0.85)
```

**Solution:**
```python
# Use database locks and atomic operations
async def boost_pattern_confidence(self, pattern_id, boost=0.05):
    async with self._db_lock:  # Lock ensures atomic operation
        # Use SQL UPDATE with calculation (atomic)
        await self.db.execute("""
            UPDATE patterns
            SET
                confidence = MIN(1.0, confidence + ?),
                boost_count = boost_count + 1
            WHERE pattern_id = ?
        """, (boost, pattern_id))

        await self.db.commit()
```

**Alternative: Optimistic Locking:**
```python
async def update_pattern_optimistic(self, pattern_id, updates):
    # Read with version
    pattern = await self._get_pattern(pattern_id)
    version = pattern['version']

    # Update with version check
    result = await self.db.execute("""
        UPDATE patterns
        SET
            confidence = ?,
            version = version + 1
        WHERE
            pattern_id = ? AND
            version = ?  -- Only update if version matches
    """, (updates['confidence'], pattern_id, version))

    if result.rowcount == 0:
        # Version mismatch - someone else updated
        raise ConcurrentModificationError("Pattern was modified by another process")
```

### 7. **Embedding Dimension Mismatch**

**Scenario:** ChromaDB embeddings change dimension (model upgrade)

**Problem:**
```
# Old embeddings: 384 dimensions
# New model: 768 dimensions
# ChromaDB error: dimension mismatch
```

**Solution:**
```python
async def migrate_embeddings(self, new_dimension):
    """Migrate to new embedding dimension"""
    # Create new collection with new dimension
    new_collection = self.chroma_client.create_collection(
        name=f"pattern_embeddings_v2_{new_dimension}",
        metadata={"hnsw:space": "cosine"}
    )

    # Re-compute all embeddings
    patterns = await self._get_all_patterns()

    for pattern in patterns:
        # Generate new embedding
        new_embedding = await self._generate_embedding(
            pattern['pattern_data'],
            dimension=new_dimension
        )

        # Store in new collection
        new_collection.add(
            ids=[pattern['pattern_id']],
            embeddings=[new_embedding],
            metadatas=[{'pattern_type': pattern['pattern_type']}]
        )

    # Delete old collection
    self.chroma_client.delete_collection("pattern_embeddings")

    # Update reference
    self.pattern_collection = new_collection

    logger.info(f"Migrated {len(patterns)} embeddings to {new_dimension}D")
```

### 8. **Memory Leak (Cache Growth)**

**Scenario:** Cache grows unbounded in long-running process

**Problem:**
```
# After 7 days uptime:
# pattern_cache: 50,000 entries
# Memory usage: 2GB
# Performance: Degraded
```

**Solution:**
```python
class AdaptiveCache:
    def __init__(self, max_size=1000, ttl_seconds=3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.cache = {}
        self.access_times = {}

    def set(self, key, value):
        # Evict if at max size
        if len(self.cache) >= self.max_size:
            self._evict_lru()

        self.cache[key] = (value, time.time())
        self.access_times[key] = time.time()

    def _evict_lru(self):
        """Evict least recently used entry"""
        if not self.access_times:
            return

        # Find LRU key
        lru_key = min(self.access_times.items(), key=lambda x: x[1])[0]

        # Remove
        del self.cache[lru_key]
        del self.access_times[lru_key]

    def _evict_expired(self):
        """Remove expired entries"""
        now = time.time()
        expired = [
            key for key, (value, timestamp) in self.cache.items()
            if now - timestamp > self.ttl_seconds
        ]

        for key in expired:
            del self.cache[key]
            if key in self.access_times:
                del self.access_times[key]
```

**Monitoring:**
```python
async def monitor_cache_health(self):
    """Periodic cache health check"""
    cache_size = len(self.pattern_cache.cache)

    if cache_size > self.pattern_cache.max_size * 0.9:
        logger.warning(f"Cache nearly full: {cache_size}/{self.pattern_cache.max_size}")

        # Trigger cleanup
        self.pattern_cache._evict_expired()
```

### 9. **Schema Evolution (Breaking Change)**

**Scenario:** New JARVIS version needs to add/modify database columns

**Problem:**
```
# Old schema: patterns table has 10 columns
# New schema: patterns table needs 12 columns
# Existing database: Can't just add columns (data migration needed)
```

**Solution:**
```python
async def migrate_schema(self, from_version, to_version):
    """Migrate database schema"""
    logger.info(f"Migrating schema from v{from_version} to v{to_version}")

    if from_version == 1 and to_version == 2:
        # Add new columns to patterns table
        await self.db.execute("""
            ALTER TABLE patterns
            ADD COLUMN embedding_id TEXT
        """)

        await self.db.execute("""
            ALTER TABLE patterns
            ADD COLUMN metadata JSON
        """)

        # Update schema version
        await self.db.execute("""
            PRAGMA user_version = 2
        """)

        logger.info("Schema migrated to v2")
```

**Version Tracking:**
```python
async def check_schema_version(self):
    """Check current schema version"""
    cursor = await self.db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current_version = row[0]

    LATEST_VERSION = 3

    if current_version < LATEST_VERSION:
        logger.info(f"Schema outdated: v{current_version} (latest: v{LATEST_VERSION})")
        await self.migrate_schema(current_version, LATEST_VERSION)
    else:
        logger.debug(f"Schema up to date: v{current_version}")
```

### 10. **Unicode/Emoji in Pattern Names**

**Scenario:** User names display "Living Room TV 📺"

**Problem:**
```
# SQLite encoding issues
# ChromaDB embedding issues
# Hash collision
```

**Solution:**
```python
def _sanitize_input(self, text):
    """Sanitize user input for storage"""
    # Normalize Unicode
    import unicodedata
    normalized = unicodedata.normalize('NFKD', text)

    # Remove control characters
    sanitized = ''.join(c for c in normalized if not unicodedata.category(c).startswith('C'))

    # Trim whitespace
    sanitized = sanitized.strip()

    return sanitized

async def learn_display_pattern(self, display_name, context):
    # Sanitize input
    display_name = self._sanitize_input(display_name)

    # Store with sanitized name
    ...
```

**Emoji Preservation (Optional):**
```python
def _preserve_emoji(self, text):
    """Keep emoji but sanitize other characters"""
    import emoji

    # Extract emoji
    emoji_dict = emoji.emoji_list(text)

    # Sanitize non-emoji
    sanitized = self._sanitize_input(text)

    # Re-add emoji at original positions
    for em in emoji_dict:
        sanitized = sanitized[:em['match_start']] + em['emoji'] + sanitized[em['match_start']:]

    return sanitized
```

---

## Limitations

### Current Limitations

| Limitation | Description | Impact | Workaround |
|-----------|-------------|--------|------------|
| **1. No Distributed Sync** | Database is local-only | Can't sync across devices | Manual export/import |
| **2. Single-User** | No multi-user support | One user per JARVIS instance | User profiles (future) |
| **3. SQLite Concurrency** | Limited write concurrency | Bottleneck under heavy load | Connection pooling helps |
| **4. No Real-Time Replication** | Changes not replicated live | No HA (High Availability) | Periodic backups |
| **5. Embedding Generation Slow** | ChromaDB embedding takes 50-200ms | Slows pattern storage | Async + caching |
| **6. No Time-Series Optimization** | Temporal queries not optimized | Slow for time-range queries | Add temporal indices |
| **7. Limited Analytics** | Basic metrics only | Can't do complex analysis | Export to analytics tools |
| **8. No Versioning** | Pattern updates overwrite | Can't rollback changes | Add audit log |
| **9. No Encryption** | Data stored in plaintext | Privacy concern (local only) | Add encryption layer |
| **10. Fixed Schema** | Schema changes require migration | Breaking changes risky | Schema evolution system |

### Performance Limitations

| Operation | Current Performance | Bottleneck | Potential Improvement |
|-----------|-------------------|------------|----------------------|
| **Pattern Storage** | 2-5ms per pattern | SQLite write | Batch inserts (100x faster) |
| **Similarity Search** | 50-200ms | ChromaDB query | Index tuning |
| **Cache Lookup** | <0.1ms | N/A (already fast) | - |
| **Full Table Scan** | 100-500ms (1000 rows) | No index | Add composite indices |
| **Bulk Insert (1000)** | 1-2s | Transaction overhead | Use single transaction |
| **Database Vacuum** | 2-10s | Disk I/O | Run during low usage |
| **Embedding Generation** | 50-200ms | Model inference | Use smaller model |

### Scale Limitations

| Metric | Current Limit | When Reached | Solution |
|--------|--------------|--------------|----------|
| **Total Patterns** | 100,000 | 6-12 months | Archive old patterns |
| **Actions Logged** | 1,000,000 | 1-2 years | Rotate logs |
| **Database Size** | 1GB | 12-18 months | Optimize + archive |
| **ChromaDB Vectors** | 50,000 | 6 months | Index optimization |
| **Cache Entries** | 1,000 | N/A (LRU evicts) | Increase cache size |
| **Concurrent Writers** | 10 | Multi-agent scenarios | Connection pool |
| **Query Response Time** | <100ms | >100,000 patterns | Partitioning |

### Functional Limitations

1. **No Cross-Device Sync**
   - Can't sync patterns between Mac + iPhone
   - Workaround: Export/import JSON

2. **No Collaborative Learning**
   - Can't share patterns with other JARVIS instances
   - Workaround: Pattern export/import feature

3. **No Rollback**
   - Can't undo pattern updates
   - Workaround: Database backups

4. **No Conflict Resolution**
   - No merge strategy for conflicting patterns
   - Workaround: Context-aware differentiation

5. **No Real-Time Queries**
   - Can't stream pattern updates
   - Workaround: Polling

6. **No Geospatial Queries**
   - Can't query patterns by location
   - Workaround: Add location metadata

7. **No Graph Relationships**
   - Can't model complex relationships
   - Workaround: JSON metadata

8. **No Full-Text Search**
   - Basic LIKE queries only
   - Workaround: FTS5 extension (future)

---

## Potential Improvements

### Short-Term Improvements (1-3 months)

#### 1. **Batch Write Optimization**

**Current:**
```python
for pattern in patterns:
    await db.store_pattern(pattern)  # 1000 separate commits
```

**Improved:**
```python
async def store_patterns_batch(self, patterns):
    """Store multiple patterns in single transaction"""
    async with self._db_lock:
        async with self.db.execute("BEGIN TRANSACTION"):
            for pattern in patterns:
                await self._insert_pattern(pattern)
            await self.db.commit()

    # 100x faster for bulk inserts
```

**Benefit:** 100x faster bulk operations

#### 2. **Composite Indices**

**Current:**
```sql
CREATE INDEX idx_patterns_type ON patterns(pattern_type);
```

**Improved:**
```sql
-- Multi-column index for common queries
CREATE INDEX idx_patterns_type_confidence
ON patterns(pattern_type, confidence DESC, last_seen DESC);

-- Temporal queries
CREATE INDEX idx_temporal_lookup
ON temporal_patterns(hour_of_day, day_of_week, pattern_type);

-- Display patterns
CREATE INDEX idx_display_context
ON display_patterns(display_name, context_hash);
```

**Benefit:** 10-50x faster queries

#### 3. **Pattern Similarity Cache**

**Current:**
```python
# Recompute similarity every time
similarity = compute_similarity(pattern1, pattern2)  # 50ms
```

**Improved:**
```python
async def get_similarity_cached(self, pattern1_id, pattern2_id):
    """Get cached similarity or compute"""
    # Check cache
    cached = await self.db.execute("""
        SELECT similarity_score FROM pattern_similarity_cache
        WHERE (pattern_id_1 = ? AND pattern_id_2 = ?)
           OR (pattern_id_1 = ? AND pattern_id_2 = ?)
    """, (pattern1_id, pattern2_id, pattern2_id, pattern1_id))

    row = await cached.fetchone()
    if row:
        return row[0]  # Cache hit (instant)

    # Compute and cache
    similarity = await self._compute_similarity(pattern1_id, pattern2_id)

    await self.db.execute("""
        INSERT INTO pattern_similarity_cache VALUES (?, ?, ?, datetime('now'))
    """, (pattern1_id, pattern2_id, similarity))

    return similarity
```

**Benefit:** 100x faster similarity lookups

#### 4. **Async Embedding Generation**

**Current:**
```python
embedding = self._generate_embedding(text)  # Blocks for 100ms
await db.store_pattern(pattern, embedding)
```

**Improved:**
```python
async def store_pattern_async_embed(self, pattern):
    """Store pattern with async embedding"""
    # Store pattern immediately (no embedding)
    pattern_id = await self._store_pattern(pattern)

    # Generate embedding in background
    asyncio.create_task(self._generate_and_store_embedding(pattern_id, pattern))

    return pattern_id  # Returns immediately

async def _generate_and_store_embedding(self, pattern_id, pattern):
    """Background task for embedding"""
    embedding = await self._generate_embedding_async(pattern)

    # Store embedding
    self.pattern_collection.add(
        ids=[pattern_id],
        embeddings=[embedding]
    )
```

**Benefit:** No blocking on pattern storage

#### 5. **Query Result Caching**

**Current:**
```python
# Every query hits database
patterns = await db.get_pattern_by_type('display_connection')  # 10ms
```

**Improved:**
```python
class QueryCache:
    def __init__(self, ttl=300):  # 5-minute TTL
        self.cache = {}
        self.ttl = ttl

    async def get_or_query(self, query_key, query_func):
        # Check cache
        if query_key in self.cache:
            result, timestamp = self.cache[query_key]
            if time.time() - timestamp < self.ttl:
                return result  # Cache hit

        # Query database
        result = await query_func()

        # Cache result
        self.cache[query_key] = (result, time.time())

        return result

# Usage
patterns = await query_cache.get_or_query(
    'display_connection_patterns',
    lambda: db.get_pattern_by_type('display_connection')
)
```

**Benefit:** <0.1ms for cached queries

### Mid-Term Improvements (3-6 months)

#### 6. **Time-Series Partitioning**

**Current:**
```sql
-- All actions in one table
CREATE TABLE actions (...);  -- 1,000,000 rows (slow)
```

**Improved:**
```sql
-- Partition by month
CREATE TABLE actions_2025_10 (...);
CREATE TABLE actions_2025_11 (...);
CREATE TABLE actions_2025_12 (...);

-- View for unified access
CREATE VIEW actions AS
    SELECT * FROM actions_2025_10
    UNION ALL
    SELECT * FROM actions_2025_11
    UNION ALL
    SELECT * FROM actions_2025_12;
```

**Benefit:** 10x faster time-range queries

#### 7. **Pattern Versioning**

**Current:**
```python
# Update overwrites
await db.update_pattern(pattern_id, new_data)  # Old data lost
```

**Improved:**
```python
CREATE TABLE pattern_history (
    history_id INTEGER PRIMARY KEY,
    pattern_id TEXT,
    version INTEGER,
    pattern_data JSON,
    confidence REAL,
    timestamp TIMESTAMP,
    FOREIGN KEY (pattern_id) REFERENCES patterns(pattern_id)
);

async def update_pattern_versioned(self, pattern_id, new_data):
    # Get current version
    pattern = await self._get_pattern(pattern_id)

    # Archive current version
    await self.db.execute("""
        INSERT INTO pattern_history
        (pattern_id, version, pattern_data, confidence, timestamp)
        VALUES (?, ?, ?, ?, ?)
    """, (pattern_id, pattern['version'], pattern['data'],
          pattern['confidence'], datetime.now()))

    # Update pattern
    await self.db.execute("""
        UPDATE patterns
        SET pattern_data = ?, version = version + 1
        WHERE pattern_id = ?
    """, (new_data, pattern_id))
```

**Benefit:** Can rollback changes, audit history

#### 8. **Full-Text Search**

**Current:**
```sql
-- Slow LIKE queries
SELECT * FROM patterns WHERE pattern_data LIKE '%search term%';
```

**Improved:**
```sql
-- Enable FTS5 (SQLite Full-Text Search)
CREATE VIRTUAL TABLE patterns_fts USING fts5(
    pattern_id,
    pattern_type,
    pattern_data,
    content='patterns'
);

-- Fast text search
SELECT * FROM patterns_fts WHERE pattern_data MATCH 'search term';
```

**Benefit:** 100x faster text search

#### 9. **Automated Backup System**

**Current:**
```bash
# Manual backups
cp jarvis_learning.db jarvis_learning.db.backup
```

**Improved:**
```python
async def auto_backup(self, interval_hours=24):
    """Automatic periodic backups"""
    while True:
        await asyncio.sleep(interval_hours * 3600)

        # Create backup
        backup_path = f"{self.db_dir}/backups/jarvis_learning_{datetime.now():%Y%m%d_%H%M%S}.db"

        async with aiosqlite.connect(backup_path) as backup_db:
            await self.db.backup(backup_db)

        logger.info(f"Backup created: {backup_path}")

        # Rotate old backups (keep last 7 days)
        await self._rotate_backups(keep_days=7)
```

**Benefit:** Data safety, disaster recovery

#### 10. **Export/Import System**

**Current:**
```python
# No export feature
```

**Improved:**
```python
async def export_patterns(self, export_path, pattern_type=None):
    """Export patterns to JSON"""
    query = "SELECT * FROM patterns"
    params = []

    if pattern_type:
        query += " WHERE pattern_type = ?"
        params.append(pattern_type)

    async with self.db.execute(query, params) as cursor:
        rows = await cursor.fetchall()

    # Convert to JSON
    patterns = [dict(row) for row in rows]

    with open(export_path, 'w') as f:
        json.dump(patterns, f, indent=2, default=str)

    logger.info(f"Exported {len(patterns)} patterns to {export_path}")

async def import_patterns(self, import_path, merge=True):
    """Import patterns from JSON"""
    with open(import_path, 'r') as f:
        patterns = json.load(f)

    for pattern in patterns:
        if merge:
            # Merge with existing
            await self.store_pattern(pattern, auto_merge=True)
        else:
            # Replace existing
            await self._insert_pattern(pattern)

    logger.info(f"Imported {len(patterns)} patterns from {import_path}")
```

**Benefit:** Data portability, sharing patterns

### Long-Term Improvements (6-12 months)

#### 11. **Distributed Database (Multi-Device Sync)**

**Architecture:**
```
┌──────────────────────────────────────────────────────┐
│                  Cloud Sync Layer                     │
│                                                       │
│  ┌──────────┐       ┌──────────┐       ┌──────────┐ │
│  │  MacBook │◄─────►│  Server  │◄─────►│  iPhone  │ │
│  │  (SQLite)│       │ (Postgres│       │ (SQLite) │ │
│  └──────────┘       │  + Redis)│       └──────────┘ │
│                     └──────────┘                     │
│                                                       │
│  Features:                                           │
│  • Conflict resolution                               │
│  • Last-write-wins or CRDT                          │
│  • Incremental sync                                  │
│  • End-to-end encryption                            │
└──────────────────────────────────────────────────────┘
```

**Benefit:** Patterns sync across devices

#### 12. **Real-Time Analytics Dashboard**

**Features:**
```
┌────────────────────────────────────────┐
│     JARVIS Learning Analytics          │
├────────────────────────────────────────┤
│                                        │
│  📊 Patterns Over Time                 │
│     [Graph showing pattern growth]     │
│                                        │
│  🎯 Top Patterns                       │
│     1. Living Room TV (95% confidence) │
│     2. Work Monitor (92% confidence)   │
│                                        │
│  ⚡ Performance Metrics                │
│     Avg execution time: 0.8s           │
│     Cache hit rate: 87%                │
│                                        │
│  🧠 Learning Progress                  │
│     Total patterns: 1,247              │
│     This week: +43                     │
│                                        │
│  💡 Insights                           │
│     "You connect to TV every Wed 8pm"  │
│     "Consider auto-connect?"           │
└────────────────────────────────────────┘
```

**Implementation:**
```python
# Web dashboard (FastAPI + React)
@app.get("/api/analytics/patterns")
async def get_pattern_analytics():
    db = get_learning_database()

    # Pattern growth over time
    growth = await db.db.execute("""
        SELECT
            DATE(first_seen) as date,
            COUNT(*) as count
        FROM patterns
        GROUP BY DATE(first_seen)
        ORDER BY date
    """)

    # Top patterns
    top = await db.db.execute("""
        SELECT pattern_type, COUNT(*), AVG(confidence)
        FROM patterns
        GROUP BY pattern_type
        ORDER BY COUNT(*) DESC
        LIMIT 10
    """)

    return {
        'growth': [dict(row) for row in growth],
        'top_patterns': [dict(row) for row in top]
    }
```

**Benefit:** Visual insights into learning

#### 13. **Machine Learning Layer**

**Capabilities:**
```python
class MLPatternAnalyzer:
    """Advanced ML-powered pattern analysis"""

    async def detect_anomalies(self):
        """Detect unusual patterns using Isolation Forest"""
        from sklearn.ensemble import IsolationForest

        # Get all patterns
        patterns = await self.db.get_all_patterns()

        # Extract features
        features = [
            [p['confidence'], p['occurrence_count'], p['avg_execution_time']]
            for p in patterns
        ]

        # Train anomaly detector
        clf = IsolationForest(contamination=0.1)
        predictions = clf.fit_predict(features)

        # Return anomalies
        anomalies = [
            patterns[i] for i, pred in enumerate(predictions)
            if pred == -1
        ]

        return anomalies

    async def cluster_behaviors(self):
        """Cluster user behaviors using K-Means"""
        from sklearn.cluster import KMeans

        # Get temporal patterns
        patterns = await self.db.get_temporal_patterns()

        # Extract features (hour, day, frequency)
        features = [
            [p['hour_of_day'], p['day_of_week'], p['frequency']]
            for p in patterns
        ]

        # Cluster
        kmeans = KMeans(n_clusters=5)
        labels = kmeans.fit_predict(features)

        # Assign cluster names
        clusters = self._interpret_clusters(kmeans.cluster_centers_)

        return clusters

    async def predict_next_action(self, context):
        """Predict next user action using LSTM"""
        # (Requires TensorFlow/PyTorch)
        # Train on action sequences
        # Predict next most likely action
        ...
```

**Benefit:** Advanced predictive capabilities

#### 14. **Collaborative Filtering**

**Concept:**
```
If users A and B have similar patterns,
and user A uses pattern X,
then recommend pattern X to user B.
```

**Implementation:**
```python
async def get_pattern_recommendations(self, user_id):
    """Recommend patterns based on similar users"""
    # Get user's patterns
    user_patterns = await self._get_user_patterns(user_id)

    # Find similar users (cosine similarity)
    similar_users = await self._find_similar_users(user_patterns)

    # Get their patterns
    recommended = []
    for similar_user in similar_users:
        their_patterns = await self._get_user_patterns(similar_user['user_id'])

        # Patterns they have but user doesn't
        new_patterns = set(their_patterns) - set(user_patterns)
        recommended.extend(new_patterns)

    # Rank by popularity among similar users
    ranked = self._rank_by_popularity(recommended)

    return ranked[:10]  # Top 10 recommendations
```

**Benefit:** Discover new patterns from community

#### 15. **Natural Language Query**

**Feature:**
```python
# Natural language database queries
query = "Show me all display connections from last week that took longer than 2 seconds"

# Converts to SQL
sql = await nl_to_sql(query)
# Result:
# SELECT * FROM actions
# WHERE action_type = 'display_connection'
#   AND timestamp > datetime('now', '-7 days')
#   AND execution_time > 2.0

# Execute
results = await db.execute(sql)
```

**Implementation:**
```python
async def nl_to_sql(self, query):
    """Convert natural language to SQL using LLM"""
    prompt = f"""
    Given this database schema:
    {self.schema}

    Convert this query to SQL:
    {query}

    Return only the SQL query.
    """

    # Use Claude API
    sql = await claude_api.complete(prompt)

    # Validate SQL (prevent injection)
    validated_sql = self._validate_sql(sql)

    return validated_sql
```

**Benefit:** User-friendly data exploration

---

## Troubleshooting

### Common Issues

#### 1. **Database Won't Initialize**

**Symptoms:**
```
ERROR: Failed to initialize Learning Database: [Errno 13] Permission denied
```

**Diagnosis:**
```bash
# Check permissions
ls -la ~/.jarvis/learning/
```

**Solutions:**
```bash
# Fix permissions
chmod 755 ~/.jarvis/learning/
chmod 644 ~/.jarvis/learning/jarvis_learning.db

# Check disk space
df -h ~/.jarvis/

# Check if directory exists
mkdir -p ~/.jarvis/learning/

# Test database access
sqlite3 ~/.jarvis/learning/jarvis_learning.db "SELECT 1;"
```

#### 2. **Slow Query Performance**

**Symptoms:**
```
WARNING: Query took 2.5s (expected <100ms)
```

**Diagnosis:**
```sql
-- Check query plan
EXPLAIN QUERY PLAN
SELECT * FROM patterns WHERE pattern_type = 'display_connection';

-- Check if indices are being used
-- Expected: "SEARCH patterns USING INDEX idx_patterns_type"
-- Bad: "SCAN patterns"  (no index used)
```

**Solutions:**
```sql
-- Add missing indices
CREATE INDEX idx_patterns_type ON patterns(pattern_type);
CREATE INDEX idx_patterns_confidence ON patterns(confidence);

-- Analyze query optimizer
ANALYZE;

-- Vacuum to defragment
VACUUM;

-- Check table size
SELECT
    name,
    COUNT(*) as rows,
    (SELECT page_count * page_size FROM pragma_page_count(), pragma_page_size()) / 1024 / 1024 as size_mb
FROM patterns;
```

#### 3. **ChromaDB Errors**

**Symptoms:**
```
ERROR: Failed to initialize ChromaDB: No module named 'chromadb'
```

**Solution:**
```bash
# Install ChromaDB
pip install chromadb

# Check version
pip show chromadb

# If version incompatible, reinstall
pip uninstall chromadb
pip install chromadb==0.4.22
```

**Alternative Symptoms:**
```
ValueError: Embedding dimension mismatch: expected 384, got 768
```

**Solution:**
```python
# Clear ChromaDB and regenerate embeddings
rm -rf ~/.jarvis/learning/chroma_embeddings/
# Restart JARVIS (will recreate ChromaDB with correct dimensions)
```

#### 4. **Database Lock Errors**

**Symptoms:**
```
sqlite3.OperationalError: database is locked
```

**Diagnosis:**
```bash
# Check for stuck processes
lsof | grep jarvis_learning.db

# Check for WAL files
ls -la ~/.jarvis/learning/jarvis_learning.db*
```

**Solutions:**
```bash
# Kill stuck processes
pkill -f jarvis

# Reset WAL
sqlite3 ~/.jarvis/learning/jarvis_learning.db "PRAGMA wal_checkpoint(TRUNCATE);"

# If persistent, rebuild database
cd ~/.jarvis/learning/
mv jarvis_learning.db jarvis_learning.db.backup
sqlite3 jarvis_learning.db.backup ".dump" | sqlite3 jarvis_learning.db
```

#### 5. **High Memory Usage**

**Symptoms:**
```
WARNING: Learning Database using 2GB RAM
```

**Diagnosis:**
```python
import tracemalloc
tracemalloc.start()

# Use database
await db.store_pattern(...)

# Check memory
current, peak = tracemalloc.get_traced_memory()
print(f"Current: {current / 1024**2:.1f}MB, Peak: {peak / 1024**2:.1f}MB")
```

**Solutions:**
```python
# Reduce cache size
db.pattern_cache.max_size = 500  # Down from 1000

# Clear old cache entries
db.pattern_cache._evict_expired()

# Limit query results
patterns = await db.get_pattern_by_type('display', limit=100)  # Add limit

# Close database when not in use
await db.close()
```

#### 6. **Pattern Not Found**

**Symptoms:**
```
No pattern found for "Living Room TV" despite previous connections
```

**Diagnosis:**
```bash
# Check if pattern exists
sqlite3 ~/.jarvis/learning/jarvis_learning.db << EOF
SELECT * FROM display_patterns WHERE display_name LIKE '%Living Room%';
SELECT * FROM patterns WHERE pattern_data LIKE '%Living Room%';
EOF
```

**Possible Causes:**
```
1. Pattern stored with different name ("living room tv" vs "Living Room TV")
2. Context mismatch (different context_hash)
3. Pattern deleted during cleanup
4. Database corruption
```

**Solutions:**
```python
# Case-insensitive search
patterns = await db.db.execute("""
    SELECT * FROM display_patterns
    WHERE LOWER(display_name) = LOWER(?)
""", (display_name,))

# Ignore context for broader search
patterns = await db.db.execute("""
    SELECT * FROM display_patterns
    WHERE display_name LIKE ?
""", (f"%{display_name}%",))

# Check ChromaDB
results = db.pattern_collection.query(
    query_texts=[display_name],
    n_results=10
)
```

### Debugging Tools

#### Database Inspector

```python
async def inspect_database():
    """Inspect database health and contents"""
    db = await get_learning_database()
    await db.initialize()

    print("=" * 60)
    print("JARVIS Learning Database Inspector")
    print("=" * 60)

    # Table sizes
    print("\n📊 Table Sizes:")
    async with db.db.execute("""
        SELECT name FROM sqlite_master WHERE type='table'
    """) as cursor:
        tables = await cursor.fetchall()

    for table in tables:
        table_name = table[0]
        async with db.db.execute(f"SELECT COUNT(*) FROM {table_name}") as cursor:
            count = (await cursor.fetchone())[0]
        print(f"   {table_name}: {count} rows")

    # Database size
    import os
    size_mb = os.path.getsize(db.sqlite_path) / (1024**2)
    print(f"\n💾 Database Size: {size_mb:.2f}MB")

    # Cache stats
    print(f"\n🎯 Cache Performance:")
    cache_stats = db.pattern_cache.get_stats()
    print(f"   Pattern cache hit rate: {cache_stats.get('hit_rate', 0):.2%}")
    print(f"   Cache size: {len(db.pattern_cache.cache)} entries")

    # Recent patterns
    print(f"\n📝 Recent Patterns (last 5):")
    async with db.db.execute("""
        SELECT pattern_type, confidence, last_seen
        FROM patterns
        ORDER BY last_seen DESC
        LIMIT 5
    """) as cursor:
        rows = await cursor.fetchall()
        for row in rows:
            print(f"   {row[0]}: {row[1]:.2f} confidence ({row[2]})")

    await db.close()

# Run inspector
asyncio.run(inspect_database())
```

#### Performance Profiler

```python
import cProfile
import pstats

async def profile_database_operations():
    """Profile database performance"""
    profiler = cProfile.Profile()
    profiler.enable()

    db = await get_learning_database()
    await db.initialize()

    # Run test operations
    for i in range(100):
        await db.store_pattern({
            'pattern_type': 'test',
            'pattern_data': {'index': i},
            'confidence': 0.5
        })

    profiler.disable()

    # Print stats
    stats = pstats.Stats(profiler)
    stats.sort_stats('cumulative')
    stats.print_stats(20)  # Top 20 functions

asyncio.run(profile_database_operations())
```

---

## Performance Tuning

### Configuration Tuning

#### Optimal Settings for Different Use Cases

**Heavy Usage (Power User):**
```python
learning_db_config = {
    'cache_size': 5000,              # Large cache
    'cache_ttl_seconds': 14400,      # 4-hour TTL
    'batch_insert_size': 200,        # Large batches
    'enable_ml_features': True,
    'auto_optimize': True,
    'vacuum_interval_days': 7
}
```

**Light Usage (Casual User):**
```python
learning_db_config = {
    'cache_size': 500,               # Small cache
    'cache_ttl_seconds': 1800,       # 30-min TTL
    'batch_insert_size': 50,         # Small batches
    'enable_ml_features': False,     # Disable ChromaDB
    'auto_optimize': True,
    'vacuum_interval_days': 30
}
```

**Low Memory (8GB RAM):**
```python
learning_db_config = {
    'cache_size': 200,               # Minimal cache
    'cache_ttl_seconds': 600,        # 10-min TTL
    'batch_insert_size': 25,
    'enable_ml_features': False,
    'auto_optimize': True,
    'vacuum_interval_days': 14
}
```

### SQLite Optimizations

```sql
-- Performance pragmas
PRAGMA journal_mode = WAL;           -- Write-Ahead Logging (faster writes)
PRAGMA synchronous = NORMAL;         -- Balance safety vs speed
PRAGMA cache_size = -64000;          -- 64MB cache
PRAGMA temp_store = MEMORY;          -- Temp tables in RAM
PRAGMA mmap_size = 268435456;        -- 256MB memory-mapped I/O
PRAGMA page_size = 4096;             -- 4KB pages (SSD-optimized)
```

### Query Optimizations

```python
# Bad: Multiple round trips
for pattern_id in pattern_ids:
    pattern = await db.get_pattern(pattern_id)  # 100 queries!

# Good: Single batch query
patterns = await db.db.execute("""
    SELECT * FROM patterns
    WHERE pattern_id IN ({})
""".format(','.join('?' * len(pattern_ids))), pattern_ids)
```

---

## Summary

The JARVIS Learning Database is a sophisticated hybrid storage system that enables true persistent intelligence. Key takeaways:

### ✅ **What It Does**
- Stores all learned patterns across sessions
- Enables semantic similarity search
- Provides temporal pattern analysis
- Powers predictive intelligence
- Integrates seamlessly with UAE + SAI

### 🎯 **Why It Matters**
- JARVIS gets smarter over time
- Faster execution with usage
- Proactive suggestions
- Adapts to user behavior

### 🚀 **How to Use It**
- Automatically initialized on startup
- No manual intervention needed
- Just use JARVIS normally
- Data accumulates automatically

### 📊 **Monitoring**
- Check database size: `du -h ~/.jarvis/learning/`
- Query patterns: `sqlite3 ~/.jarvis/learning/jarvis_learning.db`
- View metrics: Built into JARVIS startup logs

### 🔧 **Maintenance**
- Auto-optimizes every 7 days
- Auto-cleans patterns >30 days old
- Backups recommended weekly
- Archive old data yearly

**The Learning Database is the foundation of JARVIS's intelligence - it transforms JARVIS from a reactive assistant into a proactive, adaptive AI that learns and improves with every interaction.** 🧠✨
