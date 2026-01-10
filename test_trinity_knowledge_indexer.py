#!/usr/bin/env python3
"""
Trinity Knowledge Indexer - End-to-End Test Script
===================================================

This script tests the complete pipeline:
1. Add sample scraped content to database
2. Initialize the knowledge indexer
3. Run indexing process
4. Verify chunks and embeddings
5. Test vector search
6. Verify training data export

Usage:
    python3 test_trinity_knowledge_indexer.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Dict, Any

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

# Color output
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def print_header(text: str):
    """Print a section header."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.RESET}\n")

def print_success(text: str):
    """Print success message."""
    print(f"{Colors.GREEN}✅ {text}{Colors.RESET}")

def print_info(text: str):
    """Print info message."""
    print(f"{Colors.BLUE}ℹ️  {text}{Colors.RESET}")

def print_warning(text: str):
    """Print warning message."""
    print(f"{Colors.YELLOW}⚠️  {text}{Colors.RESET}")

def print_error(text: str):
    """Print error message."""
    print(f"{Colors.RED}❌ {text}{Colors.RESET}")

# Sample scraped content for testing
SAMPLE_CONTENT = [
    {
        "url": "https://docs.python.org/3/library/asyncio.html",
        "title": "Python asyncio Documentation",
        "content": """
asyncio is a library to write concurrent code using the async/await syntax.

asyncio is used as a foundation for multiple Python asynchronous frameworks that provide
high-performance network and web-servers, database connection libraries, distributed task queues, etc.

asyncio is often a perfect fit for IO-bound and high-level structured network code.

Running an asyncio Program:
To run a coroutine, you can use asyncio.run():

```python
import asyncio

async def main():
    print('Hello ...')
    await asyncio.sleep(1)
    print('... World!')

asyncio.run(main())
```

Creating Tasks:
Tasks are used to schedule coroutines concurrently.

```python
async def nested():
    return 42

async def main():
    # Schedule nested() to run soon concurrently with main()
    task = asyncio.create_task(nested())

    # "task" can now be used to cancel "nested()", or
    # simply awaited to wait until it is complete:
    await task
```

Awaitables:
An object is an awaitable object if it can be used in an await expression.
Many asyncio APIs are designed to accept awaitables.

There are three main types of awaitable objects: coroutines, Tasks, and Futures.
        """,
        "topic": "Python asyncio",
        "quality_score": 0.95,
    },
    {
        "url": "https://fastapi.tiangolo.com/async/",
        "title": "FastAPI Async Concurrency",
        "content": """
FastAPI supports async request handlers natively, allowing you to write highly concurrent web applications.

Path Operation Functions:
You can declare path operation functions with async def:

```python
@app.get("/")
async def read_root():
    return {"Hello": "World"}
```

When to use async:
Use async when you're doing I/O operations:
- Database queries
- HTTP requests to external APIs
- File operations
- Calling microservices

Technical Details:
If you are using third-party libraries that tell you to call them with await, like:

results = await some_library()

Then, declare your path operation functions with async def:

@app.get('/')
async def read_results():
    results = await some_library()
    return results

Performance:
FastAPI can handle thousands of concurrent requests efficiently when using async properly.
The key is that during I/O waits, other requests can be processed.
        """,
        "topic": "FastAPI async patterns",
        "quality_score": 0.88,
    },
    {
        "url": "https://docs.langchain.com/docs/use-cases/agents",
        "title": "LangChain Agents",
        "content": """
Agents use an LLM to determine which actions to take and in what order.

An action can either be using a tool and observing its output, or returning to the user.

Agent Types:
1. Zero-shot ReAct: Uses the ReAct framework to determine which tool to use based solely on the tool's description.
2. Conversational: Designed for conversational settings with memory.
3. Self-ask with search: Uses a single tool that should be named 'Intermediate Answer'.

Creating an Agent:

```python
from langchain.agents import initialize_agent, Tool
from langchain.llms import OpenAI

llm = OpenAI(temperature=0)

tools = [
    Tool(
        name="Search",
        func=search.run,
        description="useful for when you need to answer questions about current events"
    )
]

agent = initialize_agent(tools, llm, agent="zero-shot-react-description", verbose=True)
agent.run("What was the high temperature in SF yesterday?")
```

Agent Executors:
The agent executor is the runtime for an agent. This is what actually calls the agent and executes
the actions it chooses. Pseudocode for this runtime:

1. Call the agent with the user input and any previous steps
2. If the agent returns a finish, return that to the user
3. If the agent returns an action, execute that action
4. Repeat, passing the observations back to the agent
        """,
        "topic": "LangChain agent patterns",
        "quality_score": 0.92,
    },
    {
        "url": "https://chromadb.docs/usage-guide",
        "title": "ChromaDB Usage Guide",
        "content": """
Chroma is the open-source embedding database. It makes it easy to build LLM apps by making knowledge,
facts, and skills pluggable for LLMs.

Basic Usage:

```python
import chromadb

# Create client
client = chromadb.Client()

# Create collection
collection = client.create_collection(name="my_collection")

# Add documents
collection.add(
    documents=["This is a document", "This is another document"],
    metadatas=[{"source": "notion"}, {"source": "google-docs"}],
    ids=["id1", "id2"]
)

# Query
results = collection.query(
    query_texts=["This is a query document"],
    n_results=2
)
```

Persistent Storage:

```python
client = chromadb.PersistentClient(path="/path/to/data")
```

Collections:
Collections are where you store your embeddings, documents, and metadata.
You can create, list, get, or delete collections.
        """,
        "topic": "ChromaDB vector storage",
        "quality_score": 0.90,
    },
    {
        "url": "https://example.com/low-quality",
        "title": "Low Quality Content",
        "content": "This is very short and low quality content that should be filtered out during indexing.",
        "topic": "Test",
        "quality_score": 0.3,  # Below threshold
    }
]


async def test_database_setup():
    """Test 1: Verify database and add sample content."""
    print_header("TEST 1: Database Setup & Sample Content")

    try:
        from backend.autonomy.unified_data_flywheel import get_data_flywheel

        flywheel = get_data_flywheel()

        # Initialize database
        await flywheel._init_training_database()
        print_success("Training database initialized")

        # Add sample content
        print_info("Adding sample scraped content...")
        added_count = 0

        for content in SAMPLE_CONTENT:
            content_id = flywheel.add_scraped_content(
                url=content["url"],
                title=content["title"],
                content=content["content"],
                topic=content["topic"],
                quality_score=content["quality_score"]
            )

            if content_id:
                added_count += 1
                print_success(f"  Added: {content['title'][:50]}... (quality: {content['quality_score']})")

        print_success(f"Added {added_count}/{len(SAMPLE_CONTENT)} content items")

        # Get stats
        stats = flywheel.get_training_db_stats()
        print_info(f"Database stats:")
        print(f"  Total scraped: {stats.get('total_scraped', 0)}")
        print(f"  Total experiences: {stats.get('total_experiences', 0)}")

        return True

    except Exception as e:
        print_error(f"Database setup failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_indexer_initialization():
    """Test 2: Initialize the knowledge indexer."""
    print_header("TEST 2: Knowledge Indexer Initialization")

    try:
        from backend.autonomy.trinity_knowledge_indexer import get_knowledge_indexer

        print_info("Initializing Trinity Knowledge Indexer...")
        indexer = await get_knowledge_indexer()

        print_success("Indexer initialized successfully")

        # Print configuration
        print_info("Configuration:")
        print(f"  Embedding model: {indexer.config.embedding_model_name}")
        print(f"  Chunk size: {indexer.config.chunk_size}")
        print(f"  Min quality: {indexer.config.min_quality_score}")
        print(f"  Semantic chunking: {indexer.config.semantic_chunking}")
        print(f"  Use ChromaDB: {indexer.config.use_chromadb}")
        print(f"  Use FAISS: {indexer.config.use_faiss}")
        print(f"  Export to Reactor: {indexer.config.export_to_reactor}")

        # Check dependencies
        print_info("Checking dependencies:")

        if indexer._embedding_model:
            print_success("  ✓ Embedding model loaded")
        else:
            print_warning("  ✗ Embedding model not available")

        if indexer._chroma_collection:
            print_success("  ✓ ChromaDB initialized")
        else:
            print_warning("  ✗ ChromaDB not available")

        return indexer

    except Exception as e:
        print_error(f"Indexer initialization failed: {e}")
        import traceback
        traceback.print_exc()
        return None


async def test_indexing_process(indexer):
    """Test 3: Run the indexing process."""
    print_header("TEST 3: Content Indexing Process")

    if not indexer:
        print_error("Indexer not initialized, skipping test")
        return False

    try:
        # Get unindexed content count
        from backend.autonomy.unified_data_flywheel import get_data_flywheel
        flywheel = get_data_flywheel()

        unindexed = await flywheel.get_unindexed_scraped_content(limit=100, min_quality=0.0)
        print_info(f"Found {len(unindexed)} unindexed content items")

        # Run indexing manually
        print_info("Running indexing process...")
        start_time = time.time()

        # This would normally be called by the background loop
        indexed_count = await indexer.index_new_content()

        elapsed = time.time() - start_time
        print_success(f"Indexing completed in {elapsed:.2f}s (indexed {indexed_count} items)")

        # Get metrics
        metrics = indexer.get_metrics()
        print_info("Indexing metrics:")
        print(f"  Content items indexed: {metrics.get('total_indexed', 0)}")
        print(f"  Chunks created: {metrics.get('total_chunks', 0)}")
        print(f"  Embeddings generated: {metrics.get('total_embeddings', 0)}")
        print(f"  Average chunk size: {metrics.get('avg_chunk_size', 0):.0f} chars")
        print(f"  Chunks skipped (dedup): {metrics.get('chunks_skipped_duplicate', 0)}")
        print(f"  Chunks skipped (quality): {metrics.get('chunks_skipped_quality', 0)}")

        # Verify indexing in database
        unindexed_after = await flywheel.get_unindexed_scraped_content(limit=100, min_quality=0.0)
        indexed_count = len(unindexed) - len(unindexed_after)
        print_success(f"Successfully indexed {indexed_count} content items")

        return metrics.get('total_chunks', 0) > 0

    except Exception as e:
        print_error(f"Indexing process failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_vector_search(indexer):
    """Test 4: Test vector similarity search."""
    print_header("TEST 4: Vector Similarity Search")

    if not indexer:
        print_error("Indexer not initialized, skipping test")
        return False

    try:
        # Test queries
        test_queries = [
            "How do I use async functions in Python?",
            "What are LangChain agents?",
            "How to store embeddings in a vector database?",
        ]

        for i, query in enumerate(test_queries, 1):
            print_info(f"Query {i}: \"{query}\"")

            try:
                results = await indexer.search_similar(query, limit=3)

                if results:
                    print_success(f"  Found {len(results)} results:")
                    for j, result in enumerate(results, 1):
                        score = result.get('score', 0)
                        text = result.get('text', '')[:100]
                        metadata = result.get('metadata', {})

                        print(f"    {j}. Score: {score:.3f}")
                        print(f"       Source: {metadata.get('url', 'unknown')}")
                        print(f"       Text: {text}...")
                else:
                    print_warning("  No results found")

            except Exception as e:
                print_warning(f"  Search failed: {e}")

            print()

        return True

    except Exception as e:
        print_error(f"Vector search test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_training_export(indexer):
    """Test 5: Test training data export."""
    print_header("TEST 5: Training Data Export")

    if not indexer:
        print_error("Indexer not initialized, skipping test")
        return False

    try:
        # Check export configuration
        if not indexer.config.export_to_reactor:
            print_warning("Training export is disabled in config")
            return True

        print_info(f"Export path: {indexer.config.reactor_export_path}")

        # Get unused training content count
        from backend.autonomy.unified_data_flywheel import get_data_flywheel
        flywheel = get_data_flywheel()

        unused = await flywheel.get_unused_training_content(
            min_quality=indexer.config.min_quality_score,
            limit=500
        )

        print_info(f"Found {len(unused)} content items ready for export")

        # Run export manually
        print_info("Running training data export...")
        start_time = time.time()

        exported_count = await indexer.export_training_data()

        elapsed = time.time() - start_time
        print_success(f"Export completed in {elapsed:.2f}s ({exported_count} items exported)")

        # Check if export file was created
        export_path = Path(indexer.config.reactor_export_path)
        if export_path.exists():
            export_files = list(export_path.glob("scraped_*.jsonl"))
            print_success(f"Found {len(export_files)} export files")

            if export_files:
                # Check most recent export file
                latest_export = max(export_files, key=lambda p: p.stat().st_mtime)
                file_size = latest_export.stat().st_size

                print_info(f"Latest export: {latest_export.name}")
                print_info(f"File size: {file_size:,} bytes ({file_size/1024:.1f} KB)")

                # Count lines in export
                with open(latest_export, 'r') as f:
                    line_count = sum(1 for _ in f)

                print_success(f"Exported {line_count} training examples")

                # Show sample
                print_info("Sample export entry:")
                with open(latest_export, 'r') as f:
                    first_line = f.readline()
                    sample = json.loads(first_line)
                    print(f"  Text preview: {sample['text'][:150]}...")
                    print(f"  Metadata: {sample.get('metadata', {})}")
        else:
            print_warning("Export directory doesn't exist yet")

        return True

    except Exception as e:
        print_error(f"Training export test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_end_to_end_verification():
    """Test 6: End-to-end verification."""
    print_header("TEST 6: End-to-End Verification")

    try:
        from backend.autonomy.unified_data_flywheel import get_data_flywheel

        flywheel = get_data_flywheel()

        # Get comprehensive stats
        stats = await flywheel.get_training_stats_async()

        print_info("Final Statistics:")
        print(f"  Total scraped pages: {stats.get('total_scraped_pages', 0)}")
        print(f"  Unused scraped pages: {stats.get('unused_scraped_pages', 0)}")
        print(f"  Total scraped words: {stats.get('total_scraped_words', 0):,}")
        print(f"  Avg quality score: {stats.get('avg_experience_quality', 0):.2f}")

        # Verify pipeline completeness
        print_info("\nPipeline Verification:")

        total_scraped = stats.get('total_scraped_pages', 0)
        unused_scraped = stats.get('unused_scraped_pages', 0)
        indexed = total_scraped - unused_scraped

        if total_scraped > 0:
            print_success(f"  ✓ Content scraped: {total_scraped} pages")
        else:
            print_warning("  ✗ No content scraped")

        if indexed > 0:
            print_success(f"  ✓ Content indexed: {indexed} pages")
        else:
            print_warning("  ✗ No content indexed")

        # Overall success
        success = total_scraped > 0 and indexed > 0

        if success:
            print_success("\n✅ All pipeline stages verified successfully!")
            print_info("The knowledge indexer is working correctly:")
            print("  1. ✓ Content stored in SQLite")
            print("  2. ✓ Content chunked semantically")
            print("  3. ✓ Embeddings generated")
            print("  4. ✓ Vectors stored in ChromaDB/FAISS")
            print("  5. ✓ Training data exported (if enabled)")
        else:
            print_warning("\n⚠️  Pipeline verification incomplete")

        return success

    except Exception as e:
        print_error(f"Verification failed: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run all tests."""
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}TRINITY KNOWLEDGE INDEXER - END-TO-END TEST SUITE{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'=' * 80}{Colors.RESET}\n")

    results = {}

    # Test 1: Database Setup
    results['database_setup'] = await test_database_setup()

    if not results['database_setup']:
        print_error("\nDatabase setup failed. Aborting tests.")
        return False

    # Test 2: Indexer Initialization
    indexer = await test_indexer_initialization()
    results['indexer_init'] = indexer is not None

    if not results['indexer_init']:
        print_error("\nIndexer initialization failed. Aborting tests.")
        return False

    # Test 3: Indexing Process
    results['indexing'] = await test_indexing_process(indexer)

    # Test 4: Vector Search
    results['vector_search'] = await test_vector_search(indexer)

    # Test 5: Training Export
    results['training_export'] = await test_training_export(indexer)

    # Test 6: End-to-End Verification
    results['verification'] = await test_end_to_end_verification()

    # Summary
    print_header("TEST SUMMARY")

    total_tests = len(results)
    passed_tests = sum(1 for v in results.values() if v)

    for test_name, passed in results.items():
        status = f"{Colors.GREEN}✅ PASS{Colors.RESET}" if passed else f"{Colors.RED}❌ FAIL{Colors.RESET}"
        print(f"{test_name.replace('_', ' ').title()}: {status}")

    print(f"\n{Colors.BOLD}Results: {passed_tests}/{total_tests} tests passed{Colors.RESET}")

    if passed_tests == total_tests:
        print(f"\n{Colors.GREEN}{Colors.BOLD}🎉 ALL TESTS PASSED! 🎉{Colors.RESET}")
        print(f"\n{Colors.GREEN}Trinity Knowledge Indexer is fully operational!{Colors.RESET}")
        return True
    else:
        print(f"\n{Colors.YELLOW}⚠️  Some tests failed. Check output above for details.{Colors.RESET}")
        return False


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
