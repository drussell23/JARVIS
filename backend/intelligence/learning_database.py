#!/usr/bin/env python3
"""
Advanced Learning Database System for JARVIS Goal Inference
Hybrid architecture: SQLite (structured) + ChromaDB (embeddings) + Async + ML-powered insights
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import aiosqlite

# Try to import Cloud Database Adapter
try:
    from intelligence.cloud_database_adapter import (
        get_database_adapter,
        CloudDatabaseAdapter,
        DatabaseConfig,
    )

    CLOUD_ADAPTER_AVAILABLE = True
except ImportError:
    CLOUD_ADAPTER_AVAILABLE = False
    CloudDatabaseAdapter = None  # Type hint compatibility
    DatabaseConfig = None
    logging.warning("Cloud Database Adapter not available - using SQLite only")

# Async and ML dependencies
try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False
    np = None

# ChromaDB for semantic search and embeddings
try:
    import chromadb
    from chromadb.config import Settings

    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    logging.warning("ChromaDB not available - install with: pip install chromadb")

logger = logging.getLogger(__name__)


# ============================================================================
# SQLITE ROW HELPER FUNCTIONS
# ============================================================================
# sqlite3.Row objects don't have .get() method like dicts, so we need helpers
# ============================================================================

def _row_to_dict(row) -> Dict[str, Any]:
    """
    Convert a sqlite3.Row or aiosqlite.Row to a dictionary.

    This enables .get() access with default values, which sqlite3.Row doesn't support.
    Works with any row-like object that has keys() method.

    Args:
        row: sqlite3.Row, aiosqlite.Row, or dict-like object

    Returns:
        Dictionary with all row values
    """
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    # Handle sqlite3.Row and aiosqlite.Row
    try:
        if hasattr(row, 'keys'):
            return {key: row[key] for key in row.keys()}
        # Fallback for tuple-like rows (shouldn't happen with row_factory)
        return dict(row)
    except Exception as e:
        logger.warning(f"Could not convert row to dict: {e}")
        return {}


def _safe_get(row, key: str, default: Any = None) -> Any:
    """
    Safely get a value from a sqlite3.Row or dict with a default.

    This is a robust replacement for row.get() which doesn't exist on sqlite3.Row.

    Args:
        row: sqlite3.Row, dict, or any object with key access
        key: The key to retrieve
        default: Default value if key doesn't exist or access fails

    Returns:
        The value at key, or default if not found
    """
    if row is None:
        return default
    try:
        # Try dict-like .get() first (works for dicts)
        if hasattr(row, 'get'):
            return row.get(key, default)
        # Try direct key access (works for sqlite3.Row)
        if hasattr(row, 'keys') and key in row.keys():
            value = row[key]
            return value if value is not None else default
        # Direct index access
        return row[key] if row[key] is not None else default
    except (KeyError, IndexError, TypeError):
        return default


# ============================================================================
# DATABASE WRAPPER CLASSES
# ============================================================================
# These classes provide a robust, async, dynamic abstraction layer that makes
# Cloud SQL (PostgreSQL) behave like aiosqlite, enabling seamless switching
# between local SQLite and cloud databases without code changes.
#
# Features:
# - Full DB-API 2.0 compatibility (execute, executemany, fetchall, fetchone, etc.)
# - Transaction support with savepoints for nested transactions
# - Connection pooling and resource management
# - Automatic query type detection (SELECT vs DML)
# - Comprehensive error handling and logging
# - Async iteration support
# - Context manager support for RAII pattern
# - No hardcoded values - fully dynamic configuration
# - DetachedCursor for safe result access after connection release
# ============================================================================


class AsyncContextCursor:
    """
    Awaitable async context manager wrapper for execute() operations.

    This class makes execute() both awaitable AND usable as an async context manager,
    supporting both patterns:
        cursor = await db.execute(...)
        async with db.execute(...) as cursor:

    NO HARDCODING - fully dynamic and robust.
    """

    def __init__(self, coro):
        """
        Initialize with coroutine that returns DetachedCursor.

        Args:
            coro: Coroutine from execute() method
        """
        self._coro = coro
        self._cursor = None

    def __await__(self):
        """Make awaitable - returns DetachedCursor."""
        return self._coro.__await__()

    async def __aenter__(self):
        """Async context manager entry - await and return cursor."""
        self._cursor = await self._coro
        return self._cursor

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._cursor:
            await self._cursor.close()
        return False


class UniversalRow:
    """
    Universal row wrapper supporting both numeric and column name access.

    Works with:
    - asyncpg.Record (PostgreSQL)
    - sqlite3.Row (SQLite)
    - dict (generic)

    Supports:
    - row[0], row[1] (numeric index)
    - row['column'] (column name)
    - row.column (attribute access)
    - dict(row) (conversion to dict)
    """

    def __init__(self, data, columns=None):
        """
        Initialize universal row.

        Args:
            data: Row data (dict, asyncpg.Record, or sqlite3.Row)
            columns: Optional column names (if data is tuple/list)
        """
        self._data = data
        self._columns = columns

        # Extract column names if not provided
        if self._columns is None:
            if hasattr(data, 'keys'):
                # dict or Record object
                self._columns = list(data.keys())
            elif isinstance(data, (tuple, list)):
                # tuple/list without column names
                self._columns = [f"col_{i}" for i in range(len(data))]
            else:
                self._columns = []

    def __getitem__(self, key):
        """Support both numeric and column name access."""
        if isinstance(key, int):
            # Numeric index - convert to column name
            if 0 <= key < len(self._columns):
                col_name = self._columns[key]
                return self._data[col_name] if hasattr(self._data, '__getitem__') else getattr(self._data, col_name)
            raise IndexError(f"Row index {key} out of range (0-{len(self._columns)-1})")
        else:
            # Column name access
            return self._data[key] if hasattr(self._data, '__getitem__') else getattr(self._data, key)

    def __getattr__(self, name):
        """Support attribute access (row.column)."""
        if name.startswith('_'):
            # Avoid infinite recursion with internal attributes
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
        return self[name]

    def __iter__(self):
        """Iterate over column values."""
        for col in self._columns:
            yield self[col]

    def __len__(self):
        """Return number of columns."""
        return len(self._columns)

    def __repr__(self):
        """String representation."""
        return f"UniversalRow({dict(self)})"

    def keys(self):
        """Return column names."""
        return self._columns

    def values(self):
        """Return column values."""
        return [self[col] for col in self._columns]

    def items(self):
        """Return (column, value) pairs."""
        return [(col, self[col]) for col in self._columns]

    def __dict__(self):
        """Convert to dict."""
        return {col: self[col] for col in self._columns}


class DetachedCursor:
    """
    A cursor that holds query results without maintaining a connection reference.

    This class is returned from execute() methods to allow safe access to results
    after the underlying connection has been released back to the pool.

    This fixes the "connection has been released back to the pool" error by
    ensuring no operations are attempted on a released connection.

    INTELLIGENT FEATURES:
    - UniversalRow wrapper for results (supports both row[0] and row['column'])
    - Works with both asyncpg.Record and dict results
    """

    def __init__(
        self,
        results: List[Any],
        row_count: int = -1,
        description: Optional[List[Tuple]] = None,
        lastrowid: Optional[int] = None,
        query: Optional[str] = None,
        params: Optional[Tuple] = None,
    ):
        """
        Initialize detached cursor with query results.

        Args:
            results: Query result rows (UniversalRow, dict, or Record objects)
            row_count: Number of affected rows
            description: Column descriptions
            lastrowid: Last inserted row ID
            query: The executed query
            params: Query parameters
        """
        # Wrap results in UniversalRow if not already wrapped
        self._results = []
        for row in (results or []):
            if isinstance(row, UniversalRow):
                self._results.append(row)
            elif hasattr(row, 'keys'):
                # dict or Record - wrap it
                self._results.append(UniversalRow(row))
            else:
                # Unknown type - wrap as-is
                self._results.append(row)

        self._row_count = row_count
        self._description = description
        self._lastrowid = lastrowid
        self._query = query
        self._params = params
        self._current_index = 0
        self._arraysize = 1

    @property
    def rowcount(self) -> int:
        """Return number of rows affected by last operation."""
        return self._row_count

    @property
    def description(self) -> Optional[List[Tuple]]:
        """Return column descriptions for last query result."""
        return self._description

    @property
    def lastrowid(self) -> Optional[int]:
        """Return last inserted row ID."""
        return self._lastrowid

    @property
    def arraysize(self) -> int:
        """Number of rows to fetch at a time with fetchmany()."""
        return self._arraysize

    @arraysize.setter
    def arraysize(self, size: int):
        """Set arraysize for fetchmany() operations."""
        if size < 1:
            raise ValueError("arraysize must be >= 1")
        self._arraysize = size

    @property
    def rownumber(self) -> Optional[int]:
        """Current 0-based index of cursor in result set."""
        if self._current_index == 0:
            return None
        return self._current_index - 1

    @property
    def query(self) -> Optional[str]:
        """Return last executed query."""
        return self._query

    async def fetchone(self) -> Optional[Dict[str, Any]]:
        """Fetch next row from results."""
        if self._current_index >= len(self._results):
            return None
        row = self._results[self._current_index]
        self._current_index += 1
        return row

    async def fetchall(self) -> List[Dict[str, Any]]:
        """Fetch all remaining rows from results."""
        remaining = self._results[self._current_index:]
        self._current_index = len(self._results)
        return remaining

    async def fetchmany(self, size: Optional[int] = None) -> List[Dict[str, Any]]:
        """Fetch next batch of rows."""
        if size is None:
            size = self._arraysize
        end_index = min(self._current_index + size, len(self._results))
        rows = self._results[self._current_index:end_index]
        self._current_index = end_index
        return rows

    def __aiter__(self):
        """Async iterator support."""
        return self

    async def __anext__(self) -> Dict[str, Any]:
        """Get next row for async iteration."""
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row

    async def close(self):
        """No-op for detached cursor (no connection to close)."""
        pass

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        return False


class DatabaseCursorWrapper:
    """
    Advanced wrapper that makes Cloud SQL adapter look like aiosqlite cursor.
    Provides robust, async, dynamic database operations with comprehensive error handling.
    """

    def __init__(self, adapter_conn, connection_wrapper=None):
        """
        Initialize cursor wrapper.

        Args:
            adapter_conn: Cloud SQL adapter connection instance
            connection_wrapper: Parent connection wrapper for context
        """
        self.adapter_conn = adapter_conn
        self.connection_wrapper = connection_wrapper
        self._last_results: List[Dict[str, Any]] = []
        self._last_query: Optional[str] = None
        self._last_params: Optional[Tuple] = None
        self._current_index: int = 0
        self._row_count: int = -1  # DB-API 2.0: -1 means "not available"
        self._description: Optional[List[Tuple]] = None
        self._lastrowid: Optional[int] = None
        self._arraysize: int = 1  # DB-API 2.0 default
        self._rownumber: Optional[int] = None  # Current row position
        self._column_metadata: Dict[str, Dict[str, Any]] = {}  # Extended metadata

    @property
    def rowcount(self) -> int:
        """
        Return number of rows affected by last operation.

        DB-API 2.0 compliant:
        - For SELECT: number of rows returned
        - For INSERT/UPDATE/DELETE: number of rows affected
        - Returns -1 if not available or not applicable

        Returns:
            int: Row count or -1 if unavailable
        """
        return self._row_count

    @property
    def description(self) -> Optional[List[Tuple]]:
        """
        Return column descriptions for last query result.

        DB-API 2.0 format - 7-item sequence per column:
        (name, type_code, display_size, internal_size, precision, scale, null_ok)

        Enhanced with dynamic type inference from result data:
        - Automatically detects Python types from results
        - Provides basic size estimates
        - Supports NULL detection

        Returns:
            Optional[List[Tuple]]: Column descriptions or None for non-SELECT queries
        """
        if not self._description and self._last_results:
            # Dynamically build description from results
            self._build_description_from_results()
        return self._description

    @property
    def lastrowid(self) -> Optional[int]:
        """
        Return last inserted row ID.

        Advanced implementation:
        - PostgreSQL: Uses RETURNING id clause (must be in query)
        - SQLite: Returns last inserted rowid
        - Auto-detects 'id' column from RETURNING results

        Note: For PostgreSQL, query must include RETURNING clause:
            INSERT INTO table (col) VALUES (?) RETURNING id

        Returns:
            Optional[int]: Last row ID or None if unavailable
        """
        if self._lastrowid is not None:
            return self._lastrowid

        # Try to extract from RETURNING results
        if self._last_results and self._last_query:
            query_upper = self._last_query.strip().upper()
            if "RETURNING" in query_upper and self._last_results:
                # Check for common ID column names
                first_row = self._last_results[0]
                for id_col in ["id", "ID", "Id", "rowid", "ROWID"]:
                    if id_col in first_row:
                        try:
                            self._lastrowid = int(first_row[id_col])
                            return self._lastrowid
                        except (ValueError, TypeError):
                            pass

                # If only one column returned, assume it's the ID
                if len(first_row) == 1:
                    try:
                        self._lastrowid = int(next(iter(first_row.values())))
                        return self._lastrowid
                    except (ValueError, TypeError):
                        pass

        return None

    @property
    def arraysize(self) -> int:
        """
        Number of rows to fetch at a time with fetchmany().
        DB-API 2.0 compliant (default: 1)
        """
        return self._arraysize

    @arraysize.setter
    def arraysize(self, size: int):
        """Set arraysize for fetchmany() operations"""
        if size < 1:
            raise ValueError("arraysize must be >= 1")
        self._arraysize = size

    @property
    def rownumber(self) -> Optional[int]:
        """
        Current 0-based index of cursor in result set.
        None if no query executed or before first row.
        """
        if self._current_index == 0:
            return None
        return self._current_index - 1

    @property
    def connection(self):
        """Return parent connection wrapper (DB-API 2.0 optional)"""
        return self.connection_wrapper

    @property
    def query(self) -> Optional[str]:
        """Return last executed query (extension)"""
        return self._last_query

    @property
    def query_parameters(self) -> Optional[Tuple]:
        """Return last query parameters (extension)"""
        return self._last_params

    def _build_description_from_results(self):
        """
        Dynamically build column description from result data.
        Infers types and sizes from actual values.
        """
        if not self._last_results:
            self._description = None
            return

        first_row = self._last_results[0]
        descriptions = []

        for col_name in first_row.keys():
            # Infer type and properties from all rows
            col_type = None
            max_size = 0
            has_null = False

            for row in self._last_results:
                value = row.get(col_name)

                if value is None:
                    has_null = True
                    continue

                # Detect Python type
                value_type = type(value)
                if col_type is None:
                    col_type = value_type

                # Estimate size
                if isinstance(value, str):
                    max_size = max(max_size, len(value))
                elif isinstance(value, (int, float)):
                    max_size = max(max_size, len(str(value)))
                elif isinstance(value, bytes):
                    max_size = max(max_size, len(value))

            # Build DB-API 2.0 description tuple
            # (name, type_code, display_size, internal_size, precision, scale, null_ok)
            descriptions.append(
                (
                    col_name,  # name
                    col_type,  # type_code (Python type)
                    max_size if max_size > 0 else None,  # display_size
                    None,  # internal_size (not available)
                    None,  # precision (not available)
                    None,  # scale (not available)
                    has_null,  # null_ok
                )
            )

            # Store extended metadata
            self._column_metadata[col_name] = {
                "python_type": col_type,
                "max_size": max_size,
                "nullable": has_null,
                "samples": min(len(self._last_results), 5),
            }

        self._description = descriptions

    async def execute(self, sql: str, parameters: Tuple = ()) -> "DatabaseCursorWrapper":
        """
        Execute SQL with parameters. Handles both SELECT and DML operations.

        Advanced features:
        - Automatic query type detection (SELECT, INSERT, UPDATE, DELETE, etc.)
        - RETURNING clause support for PostgreSQL
        - Rowcount tracking for affected rows
        - Lastrowid extraction from RETURNING results
        - Dynamic column description generation

        Args:
            sql: SQL query string (with %s or $1 style placeholders)
            parameters: Query parameters tuple

        Returns:
            Self for chaining

        Raises:
            Exception: Database errors with detailed logging
        """
        try:
            # Convert %s placeholders to $1, $2, etc. for PostgreSQL/asyncpg
            if "%s" in sql:
                # Simple sequential replacement
                i = 1
                while "%s" in sql:
                    sql = sql.replace("%s", f"${i}", 1)
                    i += 1

            # Reset state for new query
            self._last_query = sql
            self._last_params = parameters
            self._current_index = 0
            self._lastrowid = None
            self._column_metadata.clear()

            # Skip PRAGMA commands for PostgreSQL
            if sql.strip().upper().startswith("PRAGMA"):
                logger.debug(f"Skipping PRAGMA command: {sql}")
                self._last_results = []
                self._row_count = 0
                self._description = None
                return self

            # Detect query type dynamically
            query_upper = sql.strip().upper()
            query_type = self._detect_query_type(query_upper)

            if query_type in ("SELECT", "RETURNING"):
                # Query that returns rows
                results = await self.adapter_conn.fetch(sql, *parameters)
                self._last_results = results if results else []
                self._row_count = len(self._last_results)

                # Build dynamic column descriptions
                if self._last_results:
                    self._build_description_from_results()
                else:
                    self._description = None

                # Extract lastrowid from RETURNING results
                if query_type == "RETURNING" and self._last_results:
                    self._extract_lastrowid()

            elif query_type in ("INSERT", "UPDATE", "DELETE"):
                # DML operation - try to get affected rows count
                try:
                    # For PostgreSQL with asyncpg, we can use execute and get status
                    result = await self.adapter_conn.execute(sql, *parameters)

                    # Try to extract row count from result status
                    # PostgreSQL returns status like "INSERT 0 1" or "UPDATE 5"
                    if hasattr(result, "decode"):
                        result = result.decode("utf-8")

                    if isinstance(result, str):
                        self._row_count = self._parse_rowcount_from_status(result)
                    else:
                        self._row_count = -1  # Not available

                except Exception as e:
                    logger.debug(f"Could not determine rowcount: {e}")
                    await self.adapter_conn.execute(sql, *parameters)
                    self._row_count = -1  # Not available

                self._last_results = []
                self._description = None

            else:
                # Other operations (CREATE, DROP, ALTER, etc.)
                await self.adapter_conn.execute(sql, *parameters)
                self._last_results = []
                self._row_count = -1  # Not applicable
                self._description = None

            return self

        except Exception as e:
            logger.error(f"Error executing query: {sql[:100]}... with params {parameters}: {e}")
            # Reset state on error
            self._row_count = -1
            self._description = None
            self._last_results = []
            raise

    def _detect_query_type(self, query_upper: str) -> str:
        """
        Dynamically detect SQL query type.

        Args:
            query_upper: Uppercase SQL query

        Returns:
            str: Query type ('SELECT', 'INSERT', 'UPDATE', 'DELETE', 'RETURNING', etc.)
        """
        # Check for RETURNING clause (takes precedence)
        if "RETURNING" in query_upper:
            return "RETURNING"

        # Check for standard DML/DDL commands
        query_start = query_upper.split()[0] if query_upper.split() else ""

        if query_start in ("SELECT", "WITH"):  # WITH for CTEs
            return "SELECT"
        elif query_start == "INSERT":
            return "INSERT"
        elif query_start == "UPDATE":
            return "UPDATE"
        elif query_start == "DELETE":
            return "DELETE"
        elif query_start in ("CREATE", "DROP", "ALTER", "TRUNCATE"):
            return "DDL"
        else:
            return "OTHER"

    def _parse_rowcount_from_status(self, status: str) -> int:
        """
        Parse rowcount from PostgreSQL status string.

        Examples:
            "INSERT 0 1" -> 1
            "UPDATE 5" -> 5
            "DELETE 10" -> 10

        Args:
            status: PostgreSQL command status string

        Returns:
            int: Number of affected rows or -1 if unavailable
        """
        try:
            parts = status.split()
            if len(parts) >= 2:
                # Last part is usually the row count
                return int(parts[-1])
        except (ValueError, IndexError):
            pass
        return -1

    def _extract_lastrowid(self):
        """
        Extract last inserted row ID from RETURNING results.
        Checks common ID column names and patterns.
        """
        if not self._last_results:
            return

        first_row = self._last_results[0]

        # Priority order for ID column detection
        id_candidates = ["id", "ID", "Id", "rowid", "ROWID", "RowId", "_id", "_ID", "pk", "PK"]

        # Try known column names first
        for col_name in id_candidates:
            if col_name in first_row:
                try:
                    self._lastrowid = int(first_row[col_name])
                    logger.debug(f"Extracted lastrowid={self._lastrowid} from column '{col_name}'")
                    return
                except (ValueError, TypeError) as e:
                    logger.debug(f"Could not convert {col_name} to int: {e}")

        # If RETURNING has exactly one column, assume it's the ID
        if len(first_row) == 1:
            try:
                value = next(iter(first_row.values()))
                self._lastrowid = int(value)
                logger.debug(f"Extracted lastrowid={self._lastrowid} from single RETURNING column")
            except (ValueError, TypeError, StopIteration) as e:
                logger.debug(f"Could not extract lastrowid from single column: {e}")

    async def upsert(
        self, table: str, unique_cols: List[str], data: Dict[str, Any]
    ) -> "DatabaseCursorWrapper":
        """
        Database-agnostic UPSERT with robust fallback implementation.

        Tries to delegate to adapter connection's upsert method first.
        Falls back to manual UPSERT SQL if adapter doesn't have upsert.

        Args:
            table: Table name
            unique_cols: List of columns that form the unique constraint
            data: Dictionary of column_name: value to insert/update

        Returns:
            Self for method chaining
        """
        # Try to use adapter's upsert if available
        if hasattr(self.adapter_conn, 'upsert') and callable(getattr(self.adapter_conn, 'upsert')):
            try:
                await self.adapter_conn.upsert(table, unique_cols, data)
                self._row_count = 1  # UPSERT affects 1 row
                return self
            except Exception as e:
                logger.debug(f"adapter_conn.upsert failed, falling back to manual UPSERT: {e}")

        # Fallback: Construct UPSERT SQL manually
        logger.debug(f"Using fallback UPSERT for table {table}")

        cols = list(data.keys())
        values = tuple(data.values())

        # Detect database type by checking for asyncpg/psycopg methods
        is_postgresql = hasattr(self.adapter_conn, 'fetch') or hasattr(self.adapter_conn, 'fetchrow')

        if is_postgresql:
            # PostgreSQL: INSERT ... ON CONFLICT DO UPDATE
            placeholders = ",".join([f"${i+1}" for i in range(len(cols))])
            col_names = ",".join(cols)
            conflict_target = ",".join(unique_cols)
            update_cols = [col for col in cols if col not in unique_cols]

            if update_cols:
                update_set = ",".join([f"{col} = EXCLUDED.{col}" for col in update_cols])
                query = f"""
                    INSERT INTO {table} ({col_names})
                    VALUES ({placeholders})
                    ON CONFLICT ({conflict_target})
                    DO UPDATE SET {update_set}
                """
            else:
                # No non-unique columns, just ignore conflicts
                query = f"""
                    INSERT INTO {table} ({col_names})
                    VALUES ({placeholders})
                    ON CONFLICT ({conflict_target}) DO NOTHING
                """
        else:
            # SQLite: INSERT OR REPLACE
            placeholders = ",".join(["?" for _ in cols])
            col_names = ",".join(cols)
            query = f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})"

        # Execute the UPSERT
        try:
            if is_postgresql:
                # PostgreSQL uses $1, $2, etc
                await self.adapter_conn.execute(query, *values)
            else:
                # SQLite uses ?
                await self.execute(query, values)

            self._row_count = 1  # UPSERT affects 1 row
            return self
        except Exception as e:
            logger.error(f"Fallback UPSERT failed for table {table}: {e}")
            logger.error(f"Query: {query}")
            logger.error(f"Values: {values}")
            raise

    async def executemany(self, sql: str, parameters_list: List[Tuple]) -> "DatabaseCursorWrapper":
        """
        Execute SQL with multiple parameter sets.

        Args:
            sql: SQL query string
            parameters_list: List of parameter tuples

        Returns:
            Self for chaining
        """
        try:
            total_affected = 0
            for parameters in parameters_list:
                await self.execute(sql, parameters)
                total_affected += self._row_count

            self._row_count = total_affected
            return self

        except Exception as e:
            logger.error(f"Error in executemany: {e}")
            raise

    async def fetchall(self) -> List[Dict[str, Any]]:
        """
        Fetch all remaining results from last query.

        Returns:
            List of result dictionaries
        """
        try:
            results = self._last_results[self._current_index :]
            self._current_index = len(self._last_results)
            return results

        except Exception as e:
            logger.error(f"Error in fetchall: {e}")
            return []

    async def fetchone(self) -> Optional[Dict[str, Any]]:
        """
        Fetch next result from last query.

        Returns:
            Single result dictionary or None
        """
        try:
            if self._current_index < len(self._last_results):
                result = self._last_results[self._current_index]
                self._current_index += 1
                return result
            return None

        except Exception as e:
            logger.error(f"Error in fetchone: {e}")
            return None

    async def fetchmany(self, size: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Fetch multiple results from last query.

        DB-API 2.0 compliant:
        - If size is None, uses cursor.arraysize (default: 1)
        - Returns up to size rows
        - Returns empty list when no more rows

        Args:
            size: Number of rows to fetch (None = use arraysize)

        Returns:
            List of result dictionaries (up to size items)
        """
        try:
            # Use arraysize if size not specified (DB-API 2.0 behavior)
            fetch_size = size if size is not None else self._arraysize

            if fetch_size < 1:
                raise ValueError("fetchmany size must be >= 1")

            results = self._last_results[self._current_index : self._current_index + fetch_size]
            self._current_index += len(results)
            return results

        except Exception as e:
            logger.error(f"Error in fetchmany: {e}")
            return []

    async def fetchval(self) -> Optional[Any]:
        """
        Fetch first column of next result.

        Returns:
            Single value or None
        """
        try:
            row = await self.fetchone()
            if row:
                # Get first value from dict
                return next(iter(row.values())) if row else None
            return None

        except Exception as e:
            logger.error(f"Error in fetchval: {e}")
            return None

    async def scroll(self, value: int, mode: str = "relative"):
        """
        Scroll cursor position (DB-API 2.0 optional extension).

        Args:
            value: Number of rows to move
            mode: 'relative' (default) or 'absolute'

        Raises:
            IndexError: If scrolling beyond result bounds
        """
        if mode == "relative":
            new_index = self._current_index + value
        elif mode == "absolute":
            new_index = value
        else:
            raise ValueError(f"Invalid scroll mode: {mode}. Use 'relative' or 'absolute'")

        if new_index < 0 or new_index > len(self._last_results):
            raise IndexError(f"Scroll out of range: {new_index}")

        self._current_index = new_index

    def setinputsizes(self, sizes):
        """DB-API 2.0 required method (no-op for our implementation)"""

    def setoutputsize(self, size, column=None):
        """DB-API 2.0 required method (no-op for our implementation)"""

    def get_column_metadata(self, column_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get extended column metadata (custom extension).

        Args:
            column_name: Specific column or None for all columns

        Returns:
            Dict with column metadata including types, sizes, nullability
        """
        if column_name:
            return self._column_metadata.get(column_name, {})
        return self._column_metadata.copy()

    async def close(self):
        """Close cursor and release resources"""
        self._last_results.clear()
        self._current_index = 0
        self._row_count = -1
        self._description = None
        self._lastrowid = None
        self._column_metadata.clear()

    def detach(self) -> "DetachedCursor":
        """
        Create a detached cursor with current results.

        This allows safe access to query results after the underlying
        connection has been released back to the pool.

        Returns:
            DetachedCursor: A cursor that holds results without connection reference
        """
        return DetachedCursor(
            results=list(self._last_results),  # Copy the results
            row_count=self._row_count,
            description=self._description,
            lastrowid=self._lastrowid,
            query=self._last_query,
            params=self._last_params,
        )

    def __aiter__(self):
        """Make cursor iterable"""
        return self

    async def __anext__(self) -> Dict[str, Any]:
        """Async iteration support"""
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        return False

    def __repr__(self) -> str:
        """String representation for debugging"""
        status = "closed" if self._row_count == -1 and not self._last_results else "open"
        return (
            f"<DatabaseCursorWrapper status={status} "
            f"rowcount={self._row_count} "
            f"rownumber={self.rownumber}>"
        )

    def __str__(self) -> str:
        """User-friendly string representation"""
        if self._last_query:
            query_preview = (
                self._last_query[:50] + "..." if len(self._last_query) > 50 else self._last_query
            )
            return f"Cursor(query='{query_preview}', rowcount={self._row_count})"
        return "Cursor(no query)"


class DatabaseConnectionWrapper:
    """
    Advanced wrapper that makes Cloud SQL adapter look like aiosqlite connection.
    Provides transaction support, connection pooling, and comprehensive error handling.

    INTELLIGENT FEATURES:
    - Automatic type conversion (datetime strings -> datetime objects for PostgreSQL)
    - SQL dialect translation (SQLite -> PostgreSQL syntax)
    - Result normalization (asyncpg Records -> dict-like with numeric indices)
    """

    def __init__(self, adapter):
        """
        Initialize connection wrapper.

        Args:
            adapter: Cloud SQL database adapter instance
        """
        self.adapter = adapter
        self.row_factory = None  # For aiosqlite compatibility
        self._in_transaction: bool = False
        self._transaction_savepoint: Optional[str] = None
        self._current_connection = None
        self._connection_lock = asyncio.Lock()
        self._closed: bool = False

    @property
    def is_cloud(self) -> bool:
        """Check if using cloud database backend"""
        return self.adapter.is_cloud if hasattr(self.adapter, "is_cloud") else False

    @property
    def in_transaction(self) -> bool:
        """Check if currently in a transaction"""
        return self._in_transaction

    def _convert_parameters_for_db(self, parameters: Tuple) -> Tuple:
        """
        Intelligently convert parameters based on database type.

        For PostgreSQL (asyncpg):
        - Convert ISO datetime strings to datetime objects
        - Keep everything else as-is

        For SQLite:
        - Keep datetime strings as strings
        - Keep everything else as-is

        Args:
            parameters: Tuple of query parameters

        Returns:
            Converted tuple suitable for target database
        """
        if not self.is_cloud:
            # SQLite - no conversion needed, handles strings fine
            return parameters

        # PostgreSQL - convert datetime strings to datetime objects
        from datetime import datetime
        import re

        converted = []
        # ISO format pattern: YYYY-MM-DDTHH:MM:SS.ffffff or YYYY-MM-DDTHH:MM:SS
        iso_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?$')

        for param in parameters:
            if isinstance(param, str) and iso_pattern.match(param):
                # Convert ISO string to datetime object
                try:
                    converted.append(datetime.fromisoformat(param))
                except (ValueError, TypeError):
                    # If conversion fails, keep as string
                    converted.append(param)
            else:
                # Keep as-is
                converted.append(param)

        return tuple(converted)

    def _translate_sql_dialect(self, sql: str) -> str:
        """
        Translate SQL from SQLite dialect to PostgreSQL dialect.

        Translations:
        - ? placeholders -> $1, $2, $3... (PostgreSQL positional)
        - json_extract(column, '$.path') -> column->>'path' (PostgreSQL jsonb)
        - Other SQLite-specific functions -> PostgreSQL equivalents

        Args:
            sql: SQL query string (SQLite dialect)

        Returns:
            Translated SQL for target database
        """
        if not self.is_cloud:
            # SQLite - no translation needed
            return sql

        # PostgreSQL translation
        translated = sql

        # 1. Convert ? placeholders to $1, $2, $3...
        param_count = 0
        result = []
        i = 0
        in_string = False
        string_char = None

        while i < len(translated):
            char = translated[i]

            # Track string literals to avoid replacing ? inside them
            if char in ('"', "'") and (i == 0 or translated[i-1] != '\\'):
                if not in_string:
                    in_string = True
                    string_char = char
                elif char == string_char:
                    in_string = False
                    string_char = None

            # Replace ? with $N outside of strings
            if char == '?' and not in_string:
                param_count += 1
                result.append(f'${param_count}')
            else:
                result.append(char)

            i += 1

        translated = ''.join(result)

        # 2. Convert SQLite json_extract() to PostgreSQL jsonb operators
        # json_extract(metadata, '$.key') -> metadata->>'key'
        import re
        json_extract_pattern = re.compile(
            r"json_extract\s*\(\s*(\w+)\s*,\s*['\"]?\$\.(\w+)['\"]?\s*\)",
            re.IGNORECASE
        )
        translated = json_extract_pattern.sub(r"\1->>'\2'", translated)

        # 3. Handle json_extract with nested paths ($.path)
        # json_extract(metadata, '$.path') -> metadata->>'path' for text extraction
        json_extract_nested = re.compile(
            r"json_extract\s*\(\s*(\w+)\s*,\s*['\"]([^'\"]+)['\"]\s*\)",
            re.IGNORECASE
        )

        def replace_nested(match):
            column = match.group(1)
            path = match.group(2).replace('$.', '')
            # Use ->> for text extraction (not >> which returns jsonb)
            # This avoids "operator does not exist: jsonb >> unknown" errors
            return f"{column}->>'{path}'"

        translated = json_extract_nested.sub(replace_nested, translated)

        return translated

    def _convert_insert_to_upsert(self, sql: str, table_primary_keys: Dict[str, List[str]] = None) -> str:
        """
        Intelligently convert INSERT to UPSERT (INSERT ON CONFLICT).

        This method is fully dynamic - automatically detects table name,
        columns, and generates appropriate UPSERT for PostgreSQL or SQLite.

        For PostgreSQL:
            INSERT INTO table (col1, col2) VALUES ($1, $2)
            -> INSERT INTO table (col1, col2) VALUES ($1, $2)
               ON CONFLICT (pk_col) DO UPDATE SET col1=$1, col2=$2

        For SQLite:
            INSERT INTO table (col1, col2) VALUES (?, ?)
            -> INSERT INTO table (col1, col2) VALUES (?, ?)
               ON CONFLICT (pk_col) DO UPDATE SET col1=excluded.col1, col2=excluded.col2

        Args:
            sql: Original INSERT statement
            table_primary_keys: Optional dict mapping table names to primary key columns
                               If None, will use intelligent defaults

        Returns:
            UPSERT statement for target database
        """
        import re

        # Don't convert if already UPSERT
        if 'ON CONFLICT' in sql.upper() or 'ON DUPLICATE' in sql.upper():
            return sql

        # Don't convert if not INSERT
        if not sql.strip().upper().startswith('INSERT'):
            return sql

        # Extract table name and columns
        insert_pattern = re.compile(
            r'INSERT\s+INTO\s+(\w+)\s*\(([^)]+)\)\s*VALUES',
            re.IGNORECASE
        )
        match = insert_pattern.search(sql)

        if not match:
            # Can't parse - return unchanged
            return sql

        table_name = match.group(1)
        columns_str = match.group(2)
        columns = [col.strip() for col in columns_str.split(',')]

        # Define primary keys for known tables (fully dynamic - easy to extend)
        if table_primary_keys is None:
            table_primary_keys = {
                'workspace_usage': ['usage_id'],
                'app_usage_patterns': ['pattern_id'],
                'user_workflows': ['workflow_id'],
                'space_transitions': ['transition_id'],
                'behavioral_patterns': ['behavior_id'],
                'temporal_patterns': ['temporal_id'],
                'proactive_suggestions': ['suggestion_id'],
                'voice_samples': ['sample_id'],
                'speaker_profiles': ['speaker_id'],
                'voice_transcriptions': ['transcription_id'],
                'misheard_queries': ['misheard_id'],
                'query_retries': ['retry_id'],
                'acoustic_adaptations': ['adaptation_id'],
                'display_patterns': ['pattern_id'],
                'user_preferences': ['preference_id'],
                'actions': ['action_id'],
                'goal_action_mappings': ['mapping_id'],
                'conversation_history': ['interaction_id'],
                'interaction_corrections': ['correction_id'],
                'conversation_embeddings': ['embedding_id'],
                'ml_training_history': ['training_id'],
            }

        # Get primary key for this table
        pk_columns = table_primary_keys.get(table_name)

        if not pk_columns:
            # Unknown table - use intelligent default: assume first column is PK
            # or look for columns with '_id' suffix
            id_columns = [col for col in columns if col.endswith('_id')]
            if id_columns:
                pk_columns = [id_columns[0]]
            else:
                # Use first column as PK
                pk_columns = [columns[0]] if columns else None

        if not pk_columns:
            # Can't determine PK - return unchanged
            return sql

        # Build UPSERT statement based on database type
        if self.is_cloud:
            # PostgreSQL: INSERT ... ON CONFLICT ... DO UPDATE
            pk_clause = ', '.join(pk_columns)

            # Build UPDATE SET clause (all columns except auto-increment PKs)
            update_pairs = []
            for i, col in enumerate(columns, start=1):
                # Don't update auto-increment primary keys
                if col not in pk_columns or not col.endswith('_id'):
                    update_pairs.append(f"{col}=${i}")

            update_clause = ', '.join(update_pairs)

            # Append ON CONFLICT clause
            upsert_sql = sql.rstrip(';').rstrip()
            upsert_sql += f"\n    ON CONFLICT ({pk_clause}) DO UPDATE SET {update_clause}"

            return upsert_sql
        else:
            # SQLite: INSERT ... ON CONFLICT ... DO UPDATE with excluded
            pk_clause = ', '.join(pk_columns)

            # Build UPDATE SET clause using excluded
            update_pairs = []
            for col in columns:
                # Don't update auto-increment primary keys
                if col not in pk_columns or not col.endswith('_id'):
                    update_pairs.append(f"{col}=excluded.{col}")

            update_clause = ', '.join(update_pairs)

            # Append ON CONFLICT clause
            upsert_sql = sql.rstrip(';').rstrip()
            upsert_sql += f"\n    ON CONFLICT ({pk_clause}) DO UPDATE SET {update_clause}"

            return upsert_sql

    @asynccontextmanager
    async def cursor(self):
        """
        Return cursor-like object using adapter connection.
        Reuses connection if in transaction, otherwise creates new one.

        Yields:
            DatabaseCursorWrapper: Cursor for executing queries
        """
        if self._closed:
            raise RuntimeError("Connection is closed")

        async with self._connection_lock:
            if self._in_transaction and self._current_connection:
                # Reuse existing transaction connection
                yield DatabaseCursorWrapper(self._current_connection, connection_wrapper=self)
            else:
                # Create new connection for non-transaction queries
                async with self.adapter.connection() as conn:
                    yield DatabaseCursorWrapper(conn, connection_wrapper=self)

    def execute(self, sql: str, parameters: Tuple = ()) -> "AsyncContextCursor":
        """
        Execute SQL directly - returns awaitable AND async context manager.

        This method is fully async, parallel, intelligent, and dynamic.
        Supports both usage patterns:
            cursor = await db.execute(...)          # await pattern
            async with db.execute(...) as cursor:   # context manager pattern

        INTELLIGENT FEATURES:
        - Automatic datetime string -> datetime object conversion for PostgreSQL
        - SQL dialect translation (SQLite ? -> PostgreSQL $1, $2...)
        - json_extract() -> jsonb operators translation
        - INSERT -> UPSERT conversion (prevents duplicate key violations)

        Args:
            sql: SQL query string (SQLite dialect)
            parameters: Query parameters (datetime strings OK)

        Returns:
            AsyncContextCursor: Awaitable context manager wrapping DetachedCursor
        """
        async def _execute_impl():
            # Intelligently convert INSERT to UPSERT
            upsert_sql = self._convert_insert_to_upsert(sql)
            # Intelligently translate SQL dialect
            translated_sql = self._translate_sql_dialect(upsert_sql)
            # Intelligently convert parameters
            converted_params = self._convert_parameters_for_db(parameters)

            async with self.cursor() as cur:
                await cur.execute(translated_sql, converted_params)
                # Return detached cursor to prevent "connection released" errors
                return cur.detach()

        return AsyncContextCursor(_execute_impl())

    def executemany(self, sql: str, parameters_list: List[Tuple]) -> "AsyncContextCursor":
        """
        Execute SQL with multiple parameter sets.

        Fully async and dynamic - supports both await and context manager patterns.

        INTELLIGENT FEATURES:
        - Automatic datetime string -> datetime object conversion for PostgreSQL
        - SQL dialect translation (SQLite ? -> PostgreSQL $1, $2...)
        - INSERT -> UPSERT conversion (prevents duplicate key violations)
        - Batch parameter conversion for all parameter sets

        Args:
            sql: SQL query string (SQLite dialect)
            parameters_list: List of parameter tuples (datetime strings OK)

        Returns:
            AsyncContextCursor: Awaitable context manager wrapping DetachedCursor
        """
        async def _executemany_impl():
            # Intelligently convert INSERT to UPSERT
            upsert_sql = self._convert_insert_to_upsert(sql)
            # Intelligently translate SQL dialect
            translated_sql = self._translate_sql_dialect(upsert_sql)
            # Intelligently convert all parameter sets
            converted_params_list = [
                self._convert_parameters_for_db(params)
                for params in parameters_list
            ]

            async with self.cursor() as cur:
                await cur.executemany(translated_sql, converted_params_list)
                # Return detached cursor to prevent "connection released" errors
                return cur.detach()

        return AsyncContextCursor(_executemany_impl())

    async def executescript(self, sql_script: str):
        """
        Execute multiple SQL statements (compatibility with aiosqlite).

        Args:
            sql_script: Multiple SQL statements separated by semicolons
        """
        # Split on semicolons and execute each statement
        statements = [s.strip() for s in sql_script.split(";") if s.strip()]
        async with self.cursor() as cur:
            for statement in statements:
                await cur.execute(statement)

    async def begin(self):
        """Start a transaction explicitly"""
        if self._in_transaction:
            logger.warning("Already in transaction, creating savepoint")
            await self._create_savepoint()
            return

        async with self._connection_lock:
            if not self._current_connection:
                # Acquire connection for transaction
                self._current_connection = await self.adapter.connection().__aenter__()

            if self.is_cloud:
                # PostgreSQL: Start transaction
                await self._current_connection.execute("BEGIN")

            self._in_transaction = True
            logger.debug("Transaction started")

    async def commit(self):
        """
        Commit current transaction.
        For Cloud SQL (PostgreSQL): Explicit COMMIT
        For SQLite: Auto-commit mode, this is a no-op
        """
        if not self._in_transaction:
            # Not in explicit transaction, likely auto-commit mode
            return

        async with self._connection_lock:
            try:
                if self.is_cloud and self._current_connection:
                    # PostgreSQL: Explicit commit
                    await self._current_connection.execute("COMMIT")
                    logger.debug("Transaction committed")

                self._in_transaction = False
                self._transaction_savepoint = None

                # Release connection back to pool
                if self._current_connection:
                    await self._release_connection()

            except Exception as e:
                logger.error(f"Error committing transaction: {e}")
                await self.rollback()
                raise

    async def rollback(self):
        """
        Rollback current transaction.
        """
        if not self._in_transaction:
            logger.warning("No active transaction to rollback")
            return

        async with self._connection_lock:
            try:
                if self.is_cloud and self._current_connection:
                    if self._transaction_savepoint:
                        # Rollback to savepoint
                        await self._current_connection.execute(
                            f"ROLLBACK TO SAVEPOINT {self._transaction_savepoint}"
                        )
                        logger.debug(f"Rolled back to savepoint {self._transaction_savepoint}")
                        self._transaction_savepoint = None
                    else:
                        # Full rollback
                        await self._current_connection.execute("ROLLBACK")
                        logger.debug("Transaction rolled back")
                        self._in_transaction = False

                # Release connection
                if not self._transaction_savepoint and self._current_connection:
                    await self._release_connection()

            except Exception as e:
                logger.error(f"Error rolling back transaction: {e}")
                # Force release connection
                await self._release_connection()
                raise

    async def _create_savepoint(self):
        """Create a savepoint for nested transactions"""
        if not self.is_cloud:
            return  # SQLite handles this differently

        savepoint_name = f"sp_{int(time.time() * 1000000)}"
        if self._current_connection:
            await self._current_connection.execute(f"SAVEPOINT {savepoint_name}")
            self._transaction_savepoint = savepoint_name
            logger.debug(f"Created savepoint {savepoint_name}")

    async def _release_connection(self):
        """Release transaction connection back to pool"""
        if self._current_connection:
            try:
                # Exit the context manager for the connection
                await self._current_connection.__aexit__(None, None, None)
            except Exception as e:
                logger.error(f"Error releasing connection: {e}")
            finally:
                self._current_connection = None
                self._in_transaction = False

    @asynccontextmanager
    async def transaction(self):
        """
        Context manager for automatic transaction handling.

        Usage:
            async with conn.transaction():
                await conn.execute("INSERT ...")
                await conn.execute("UPDATE ...")
            # Auto-commits on success, rolls back on exception
        """
        await self.begin()
        try:
            yield self
            await self.commit()
        except Exception:
            await self.rollback()
            raise

    async def close(self):
        """Close connection and release all resources"""
        if self._closed:
            return

        async with self._connection_lock:
            # Rollback any pending transaction
            if self._in_transaction:
                await self.rollback()

            # Release connection
            if self._current_connection:
                await self._release_connection()

            self._closed = True
            logger.debug("Connection wrapper closed")

    async def __aenter__(self):
        """Async context manager entry"""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        if exc_type is not None and self._in_transaction:
            # Exception occurred, rollback
            await self.rollback()
        await self.close()
        return False


class PatternType(Enum):
    """Dynamic pattern types - extensible"""

    TEMPORAL = "temporal"  # Time-based patterns
    SEQUENTIAL = "sequential"  # Action sequences
    CONTEXTUAL = "contextual"  # Context-driven patterns
    HYBRID = "hybrid"  # Multi-factor patterns


class ConfidenceBoostStrategy(Enum):
    """Strategies for boosting pattern confidence"""

    LINEAR = "linear"
    EXPONENTIAL = "exponential"
    LOGARITHMIC = "logarithmic"
    ADAPTIVE = "adaptive"  # Learns optimal strategy


@dataclass
class GoalPattern:
    """Represents a learned goal pattern with ML metadata"""

    pattern_id: str
    goal_type: str
    context_embedding: Optional[List[float]]
    action_sequence: List[str]
    confidence: float
    success_rate: float
    occurrence_count: int
    last_seen: datetime
    avg_execution_time: float = 0.0
    std_execution_time: float = 0.0
    decay_factor: float = 0.95  # For time-based decay
    boost_history: List[float] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LearningMetrics:
    """Real-time learning performance metrics"""

    total_patterns: int
    active_patterns: int
    avg_confidence: float
    prediction_accuracy: float
    cache_hit_rate: float
    avg_inference_time_ms: float
    memory_usage_mb: float
    last_updated: datetime


class AdaptiveCache:
    """Smart LRU cache with TTL and size management"""

    def __init__(self, max_size: int = 1000, ttl_seconds: int = 3600):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.cache: Dict[str, Tuple[Any, float]] = {}
        self.access_count: Dict[str, int] = defaultdict(int)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache"""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.time() - timestamp < self.ttl_seconds:
                self.access_count[key] += 1
                self.hits += 1
                return value
            else:
                del self.cache[key]

        self.misses += 1
        return None

    def set(self, key: str, value: Any):
        """Set value in cache with eviction"""
        # Evict if full
        if len(self.cache) >= self.max_size:
            # Remove least recently used
            lru_key = min(self.access_count.keys(), key=lambda k: self.access_count[k])
            del self.cache[lru_key]
            del self.access_count[lru_key]

        self.cache[key] = (value, time.time())
        self.access_count[key] = 0

    def invalidate(self, key: str):
        """Remove from cache"""
        if key in self.cache:
            del self.cache[key]
            del self.access_count[key]

    def hit_rate(self) -> float:
        """Calculate cache hit rate"""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0


class PatternMatcher:
    """Advanced pattern matching with fuzzy matching and ML"""

    def __init__(self):
        self.embeddings_cache = AdaptiveCache(max_size=500, ttl_seconds=7200)

    async def compute_similarity(self, pattern1: Dict, pattern2: Dict) -> float:
        """Compute similarity between patterns using multiple signals"""
        similarity_scores = []

        # Context similarity
        if "context" in pattern1 and "context" in pattern2:
            context_sim = self._jaccard_similarity(
                set(pattern1["context"].keys()), set(pattern2["context"].keys())
            )
            similarity_scores.append(context_sim * 0.3)

        # Action sequence similarity
        if "actions" in pattern1 and "actions" in pattern2:
            action_sim = self._sequence_similarity(pattern1["actions"], pattern2["actions"])
            similarity_scores.append(action_sim * 0.4)

        # Temporal similarity
        if "timestamp" in pattern1 and "timestamp" in pattern2:
            time_sim = self._temporal_similarity(pattern1["timestamp"], pattern2["timestamp"])
            similarity_scores.append(time_sim * 0.3)

        return sum(similarity_scores) if similarity_scores else 0.0

    @staticmethod
    def _jaccard_similarity(set1: Set, set2: Set) -> float:
        """Jaccard similarity coefficient"""
        if not set1 and not set2:
            return 1.0
        intersection = len(set1.intersection(set2))
        union = len(set1.union(set2))
        return intersection / union if union > 0 else 0.0

    @staticmethod
    def _sequence_similarity(seq1: List, seq2: List) -> float:
        """Levenshtein-based sequence similarity"""
        if not seq1 and not seq2:
            return 1.0

        # Simple edit distance
        m, n = len(seq1), len(seq2)
        dp = [[0] * (n + 1) for _ in range(m + 1)]

        for i in range(m + 1):
            dp[i][0] = i
        for j in range(n + 1):
            dp[0][j] = j

        for i in range(1, m + 1):
            for j in range(1, n + 1):
                if seq1[i - 1] == seq2[j - 1]:
                    dp[i][j] = dp[i - 1][j - 1]
                else:
                    dp[i][j] = 1 + min(dp[i - 1][j], dp[i][j - 1], dp[i - 1][j - 1])

        max_len = max(m, n)
        return 1.0 - (dp[m][n] / max_len) if max_len > 0 else 0.0

    @staticmethod
    def _temporal_similarity(time1: datetime, time2: datetime) -> float:
        """Temporal similarity based on time difference"""
        diff_seconds = abs((time1 - time2).total_seconds())

        # Decay similarity over time (1 hour window)
        if diff_seconds < 3600:
            return 1.0 - (diff_seconds / 3600)
        elif diff_seconds < 86400:  # 24 hours
            return 0.5 * (1.0 - (diff_seconds / 86400))
        else:
            return 0.0


class JARVISLearningDatabase:
    """
    Advanced hybrid database system for JARVIS learning
    - Async SQLite: Structured data with connection pooling
    - ChromaDB: Embeddings and semantic search
    - Adaptive caching: Smart LRU with TTL
    - ML-powered pattern matching and insights
    - Dynamic schema evolution
    - Real-time metrics and analytics
    """

    def __init__(self, db_path: Optional[Path] = None, config: Optional[Dict] = None):
        """Initialize the advanced learning database"""
        # Configuration with defaults
        self.config = config or {}
        self.cache_size = self.config.get("cache_size", 1000)
        self.cache_ttl = self.config.get("cache_ttl_seconds", 3600)
        self.enable_ml = self.config.get("enable_ml_features", True)
        self.auto_optimize = self.config.get("auto_optimize", True)
        self.batch_size = self.config.get("batch_insert_size", 100)

        # Set up paths
        self.db_dir = db_path or Path.home() / ".jarvis" / "learning"
        self.db_dir.mkdir(parents=True, exist_ok=True)

        self.sqlite_path = self.db_dir / "jarvis_learning.db"
        self.chroma_path = self.db_dir / "chroma_embeddings"

        # Async SQLite connection (will be initialized in async context)
        # Type is Optional during __init__ but guaranteed non-None after initialize()
        self.db: Union[aiosqlite.Connection, "DatabaseConnectionWrapper", None] = None  # type: ignore[assignment]
        self._db_lock = asyncio.Lock()

        # Adaptive caching
        self.pattern_cache = AdaptiveCache(self.cache_size, self.cache_ttl)
        self.goal_cache = AdaptiveCache(self.cache_size, self.cache_ttl)
        self.query_cache = AdaptiveCache(self.cache_size // 2, self.cache_ttl)

        # Pattern matching engine
        self.pattern_matcher = PatternMatcher()

        # Batch processing queues
        self.pending_goals: deque = deque(maxlen=self.batch_size)
        self.pending_actions: deque = deque(maxlen=self.batch_size)
        self.pending_patterns: deque = deque(maxlen=self.batch_size)

        # Initialization flag to prevent multiple initializations
        self._initialized = False

        # Background task management for clean shutdown
        self._background_tasks: List[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        # Performance metrics
        self.metrics = LearningMetrics(
            total_patterns=0,
            active_patterns=0,
            avg_confidence=0.0,
            prediction_accuracy=0.0,
            cache_hit_rate=0.0,
            avg_inference_time_ms=0.0,
            memory_usage_mb=0.0,
            last_updated=datetime.now(),
        )

        # Initialize ChromaDB if available
        self.chroma_client = None
        self.goal_collection = None
        self.pattern_collection = None
        self.context_collection = None

        # Hybrid Sync System for voice biometrics
        self.hybrid_sync = None
        self._sync_enabled = self.config.get("enable_hybrid_sync", True)

        # Cloud Database Adapter for redundant Cloud SQL storage (v10.6)
        # Initialized lazily in initialize() for parallel voice sample storage
        self.cloud_adapter: Optional["CloudDatabaseAdapter"] = None
        self._cloud_adapter_enabled = self.config.get("enable_cloud_adapter", True)

        logger.info(f"Advanced JARVIS Learning Database initializing at {self.db_dir}")

    async def initialize(self):
        """Async initialization - call this after creating instance"""
        # Prevent multiple initializations
        if self._initialized:
            logger.debug("Database already initialized, skipping re-initialization")
            return

        # Initialize async SQLite
        await self._init_sqlite()

        # Initialize Cloud Database Adapter for redundant Cloud SQL storage (v10.6)
        # This enables parallel writes to both local SQLite and Cloud SQL for voice samples
        if self._cloud_adapter_enabled and CLOUD_ADAPTER_AVAILABLE:
            await self._init_cloud_adapter()

        # Initialize ChromaDB
        if CHROMADB_AVAILABLE:
            await self._init_chromadb()

        # Load metrics
        await self._load_metrics()

        # Initialize hybrid sync system for voice biometrics
        if self._sync_enabled:
            await self._init_hybrid_sync()

        # Start background tasks and track them for clean shutdown
        flush_task = asyncio.create_task(self._auto_flush_batches())
        optimize_task = asyncio.create_task(self._auto_optimize_task())
        self._background_tasks.extend([flush_task, optimize_task])

        self._initialized = True
        logger.info(f"✅ Advanced Learning Database initialized")
        logger.info(f"   Cache: {self.cache_size} entries, {self.cache_ttl}s TTL")
        logger.info(f"   ML Features: {self.enable_ml}")
        logger.info(f"   Auto-optimize: {self.auto_optimize}")
        logger.info(f"   Hybrid Sync: {self._sync_enabled}")
        logger.info(f"   Background tasks: {len(self._background_tasks)} started")

    def _ensure_db_initialized(self) -> Union[aiosqlite.Connection, "DatabaseConnectionWrapper"]:
        """Assert that database is initialized and return it with proper type."""
        assert self.db is not None, "Database not initialized. Call initialize() first."
        return self.db

    async def _init_sqlite(self):
        """Initialize async database (SQLite or Cloud SQL) with enhanced schema and timeout protection"""
        # Try to use Cloud SQL if available (with timeout protection)
        if CLOUD_ADAPTER_AVAILABLE:
            try:
                # CRITICAL FIX: Add timeout protection to prevent infinite hangs
                # If Cloud SQL proxy isn't running, this will timeout and fallback to SQLite
                adapter = await asyncio.wait_for(
                    get_database_adapter(),
                    timeout=15.0  # 15 second timeout for database adapter initialization
                )

                if adapter.is_cloud:
                    logger.info("☁️  Using Cloud SQL (PostgreSQL)")
                    self.db = DatabaseConnectionWrapper(adapter)
                else:
                    logger.info("📂 Using SQLite (Cloud SQL not configured)")
                    self.db = await aiosqlite.connect(str(self.sqlite_path))
                    self.db.row_factory = aiosqlite.Row

            except asyncio.TimeoutError:
                logger.warning(
                    "⏱️  Cloud SQL adapter initialization timeout (15s exceeded)\n"
                    "   → Falling back to local SQLite"
                )
                self.db = await aiosqlite.connect(str(self.sqlite_path))
                self.db.row_factory = aiosqlite.Row

            except Exception as e:
                logger.warning(f"⚠️  Cloud SQL adapter failed: {e}")
                logger.info("📂 Falling back to SQLite")
                self.db = await aiosqlite.connect(str(self.sqlite_path))
                self.db.row_factory = aiosqlite.Row
        else:
            logger.info("📂 Using SQLite (Cloud adapter not available)")
            self.db = await aiosqlite.connect(str(self.sqlite_path))
            self.db.row_factory = aiosqlite.Row

        # Check if using Cloud SQL (PostgreSQL) or SQLite
        is_cloud = isinstance(self.db, DatabaseConnectionWrapper) and self.db.is_cloud

        # Helper function to generate auto-increment syntax
        def auto_increment(column_name: str, col_type: str = "INTEGER") -> str:
            """Generate auto-increment primary key syntax for current database"""
            if is_cloud:
                # PostgreSQL uses SERIAL or BIGSERIAL
                serial_type = "BIGSERIAL" if col_type == "BIGINT" else "SERIAL"
                return f"{column_name} {serial_type} PRIMARY KEY"
            else:
                # SQLite uses AUTOINCREMENT
                return f"{column_name} {col_type} PRIMARY KEY AUTOINCREMENT"

        # Helper function to generate boolean default syntax
        def bool_default(value: int) -> str:
            """Generate boolean default syntax for current database"""
            if is_cloud:
                # PostgreSQL uses TRUE/FALSE
                return "TRUE" if value else "FALSE"
            else:
                # SQLite uses 1/0
                return str(value)

        # Helper function to generate binary data type
        def blob_type() -> str:
            """Generate binary data type for current database"""
            if is_cloud:
                # PostgreSQL uses BYTEA
                return "BYTEA"
            else:
                # SQLite uses BLOB
                return "BLOB"

        db = self._ensure_db_initialized()
        async with db.cursor() as cursor:
            # Enable WAL mode for better concurrency (SQLite only)
            if not is_cloud:
                await cursor.execute("PRAGMA journal_mode=WAL")
                await cursor.execute("PRAGMA synchronous=NORMAL")
                await cursor.execute("PRAGMA cache_size=10000")
                await cursor.execute("PRAGMA temp_store=MEMORY")

            # Goals table with enhanced tracking
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    goal_type TEXT NOT NULL,
                    goal_level TEXT NOT NULL,
                    description TEXT,
                    confidence REAL,
                    progress REAL DEFAULT 0.0,
                    is_completed BOOLEAN DEFAULT {bool_default(0)},
                    created_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    predicted_duration REAL,
                    actual_duration REAL,
                    evidence JSON,
                    context_hash TEXT,
                    embedding_id TEXT,
                    metadata JSON
                )
            """
            )

            # Actions table with performance tracking
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS actions (
                    action_id TEXT PRIMARY KEY,
                    action_type TEXT NOT NULL,
                    target TEXT,
                    goal_id TEXT,
                    confidence REAL,
                    success BOOLEAN,
                    execution_time REAL,
                    timestamp TIMESTAMP,
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    params JSON,
                    result JSON,
                    context_hash TEXT,
                    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
                )
            """
            )

            # Enhanced patterns table with ML metadata
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS patterns (
                    pattern_id TEXT PRIMARY KEY,
                    pattern_type TEXT NOT NULL,
                    pattern_hash TEXT UNIQUE,
                    pattern_data JSON,
                    confidence REAL,
                    success_rate REAL,
                    occurrence_count INTEGER DEFAULT 1,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    avg_execution_time REAL,
                    std_execution_time REAL,
                    decay_applied BOOLEAN DEFAULT {bool_default(0)},
                    boost_count INTEGER DEFAULT 0,
                    embedding_id TEXT,
                    metadata JSON
                )
            """
            )

            # User preferences with confidence tracking
            await cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS user_preferences (
                    preference_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT,
                    confidence REAL,
                    learned_from TEXT,
                    update_count INTEGER DEFAULT 1,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    UNIQUE(category, key)
                )
            """
            )

            # Goal-Action mappings with performance metrics
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS goal_action_mappings (
                    {auto_increment('mapping_id')},
                    goal_type TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    avg_execution_time REAL,
                    std_execution_time REAL,
                    confidence REAL,
                    last_updated TIMESTAMP,
                    prediction_accuracy REAL,
                    UNIQUE(goal_type, action_type)
                )
            """
            )

            # Display patterns with temporal analysis
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS display_patterns (
                    {auto_increment('pattern_id')},
                    display_name TEXT NOT NULL,
                    context JSON,
                    context_hash TEXT,
                    connection_time TIME,
                    day_of_week INTEGER,
                    hour_of_day INTEGER,
                    frequency INTEGER DEFAULT 1,
                    auto_connect BOOLEAN DEFAULT {bool_default(0)},
                    last_seen TIMESTAMP,
                    consecutive_successes INTEGER DEFAULT 0,
                    metadata JSON
                )
            """
            )

            # Learning metrics tracking
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS learning_metrics (
                    {auto_increment('metric_id')},
                    metric_name TEXT NOT NULL,
                    metric_value REAL,
                    timestamp TIMESTAMP,
                    context JSON
                )
            """
            )

            # Pattern similarity cache
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS pattern_similarity_cache (
                    {auto_increment('cache_id')},
                    pattern1_id TEXT NOT NULL,
                    pattern2_id TEXT NOT NULL,
                    similarity_score REAL,
                    computed_at TIMESTAMP,
                    UNIQUE(pattern1_id, pattern2_id)
                )
            """
            )

            # Context embeddings metadata
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS context_embeddings (
                    embedding_id TEXT PRIMARY KEY,
                    context_hash TEXT UNIQUE,
                    embedding_vector {blob_type()},
                    dimension INTEGER,
                    created_at TIMESTAMP,
                    access_count INTEGER DEFAULT 0,
                    last_accessed TIMESTAMP
                )
            """
            )

            # ============================================================================
            # 24/7 BEHAVIORAL LEARNING TABLES - Enhanced Workspace Tracking
            # ============================================================================

            # Workspace/Space tracking (Yabai integration)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS workspace_usage (
                    {auto_increment('usage_id')},
                    space_id INTEGER NOT NULL,
                    space_label TEXT,
                    app_name TEXT NOT NULL,
                    window_title TEXT,
                    window_position JSON,
                    focus_duration_seconds REAL,
                    timestamp TIMESTAMP,
                    day_of_week INTEGER,
                    hour_of_day INTEGER,
                    is_fullscreen BOOLEAN DEFAULT {bool_default(0)},
                    metadata JSON
                )
            """
            )

            # App usage patterns (24/7 tracking)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS app_usage_patterns (
                    {auto_increment('pattern_id')},
                    app_name TEXT NOT NULL,
                    space_id INTEGER,
                    usage_frequency INTEGER DEFAULT 1,
                    avg_session_duration REAL,
                    total_usage_time REAL,
                    typical_time_of_day INTEGER,
                    typical_day_of_week INTEGER,
                    last_used TIMESTAMP,
                    confidence REAL DEFAULT 0.5,
                    metadata JSON,
                    UNIQUE(app_name, space_id, typical_time_of_day, typical_day_of_week)
                )
            """
            )

            # User workflows (sequential action patterns)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS user_workflows (
                    {auto_increment('workflow_id')},
                    workflow_name TEXT,
                    action_sequence JSON NOT NULL,
                    space_sequence JSON,
                    app_sequence JSON,
                    frequency INTEGER DEFAULT 1,
                    avg_duration REAL,
                    success_rate REAL DEFAULT 1.0,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    time_of_day_pattern JSON,
                    confidence REAL DEFAULT 0.5,
                    metadata JSON
                )
            """
            )

            # Space transitions (movement between Spaces)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS space_transitions (
                    {auto_increment('transition_id')},
                    from_space_id INTEGER NOT NULL,
                    to_space_id INTEGER NOT NULL,
                    trigger_app TEXT,
                    trigger_action TEXT,
                    frequency INTEGER DEFAULT 1,
                    avg_time_between_seconds REAL,
                    timestamp TIMESTAMP,
                    hour_of_day INTEGER,
                    day_of_week INTEGER,
                    metadata JSON
                )
            """
            )

            # Behavioral patterns (high-level user habits)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS behavioral_patterns (
                    {auto_increment('behavior_id')},
                    behavior_type TEXT NOT NULL,
                    behavior_description TEXT,
                    pattern_data JSON NOT NULL,
                    frequency INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.5,
                    temporal_pattern JSON,
                    contextual_triggers JSON,
                    first_observed TIMESTAMP,
                    last_observed TIMESTAMP,
                    prediction_accuracy REAL,
                    metadata JSON
                )
            """
            )

            # Temporal patterns (time-based behaviors for leap years too!)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS temporal_patterns (
                    {auto_increment('temporal_id')},
                    pattern_type TEXT NOT NULL,
                    time_of_day INTEGER,
                    day_of_week INTEGER,
                    day_of_month INTEGER,
                    month_of_year INTEGER,
                    is_leap_year BOOLEAN DEFAULT {bool_default(0)},
                    action_type TEXT NOT NULL,
                    target TEXT,
                    frequency INTEGER DEFAULT 1,
                    confidence REAL DEFAULT 0.5,
                    last_occurrence TIMESTAMP,
                    metadata JSON
                )
            """
            )

            # Proactive suggestions (what JARVIS should suggest)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS proactive_suggestions (
                    {auto_increment('suggestion_id')},
                    suggestion_type TEXT NOT NULL,
                    suggestion_text TEXT NOT NULL,
                    trigger_pattern_id TEXT,
                    confidence REAL NOT NULL,
                    times_suggested INTEGER DEFAULT 0,
                    times_accepted INTEGER DEFAULT 0,
                    times_rejected INTEGER DEFAULT 0,
                    acceptance_rate REAL,
                    created_at TIMESTAMP,
                    last_suggested TIMESTAMP,
                    metadata JSON
                )
            """
            )

            # Conversation history (ALL user-JARVIS interactions for learning)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversation_history (
                    {auto_increment('interaction_id')},
                    timestamp TIMESTAMP NOT NULL,
                    session_id TEXT,
                    user_query TEXT NOT NULL,
                    jarvis_response TEXT NOT NULL,
                    response_type TEXT,
                    confidence_score REAL,
                    execution_time_ms REAL,
                    success BOOLEAN DEFAULT {bool_default(1)},
                    user_feedback TEXT,
                    feedback_score INTEGER,
                    was_corrected BOOLEAN DEFAULT {bool_default(0)},
                    correction_text TEXT,
                    context_snapshot JSON,
                    active_apps JSON,
                    current_space TEXT,
                    system_state JSON,
                    embedding_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

            # Create index for conversation_history
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_timestamp ON conversation_history(timestamp)
            """
            )
            # Create index for conversation_history
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_session ON conversation_history(session_id)
            """
            )
            # Create index for conversation_history
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_response_type ON conversation_history(response_type)
            """
            )
            # Create index for conversation_history
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_success ON conversation_history(success)
            """
            )
            # Create index for conversation_history
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_feedback_score ON conversation_history(feedback_score)
            """
            )

            # Learning from corrections (when user corrects JARVIS)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS interaction_corrections (
                    {auto_increment('correction_id')},
                    interaction_id INTEGER NOT NULL,
                    original_response TEXT NOT NULL,
                    corrected_response TEXT NOT NULL,
                    correction_type TEXT NOT NULL,
                    user_explanation TEXT,
                    applied_at TIMESTAMP NOT NULL,
                    learned BOOLEAN DEFAULT {bool_default(0)},
                    pattern_extracted TEXT,
                    metadata JSON,
                    FOREIGN KEY (interaction_id) REFERENCES conversation_history(interaction_id)
                )
            """
            )

            # Create index for interaction_corrections
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_correction_type ON interaction_corrections(correction_type)
            """
            )
            # Create index for interaction_corrections
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_learned ON interaction_corrections(learned)
            """
            )

            # Semantic embeddings for conversation search
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS conversation_embeddings (
                    embedding_id TEXT PRIMARY KEY,
                    interaction_id INTEGER NOT NULL,
                    query_embedding {blob_type()},
                    response_embedding {blob_type()},
                    combined_embedding {blob_type()},
                    embedding_model TEXT DEFAULT 'all-MiniLM-L6-v2',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (interaction_id) REFERENCES conversation_history(interaction_id)
                )
            """
            )

            # Create index for conversation_embeddings
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_interaction ON conversation_embeddings(interaction_id)
            """
            )

            # Voice transcription accuracy tracking
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS voice_transcriptions (
                    {auto_increment('transcription_id')},
                    interaction_id INTEGER,
                    raw_audio_hash TEXT NOT NULL,
                    transcribed_text TEXT NOT NULL,
                    corrected_text TEXT,
                    confidence_score REAL,
                    audio_duration_ms REAL,
                    language_code TEXT DEFAULT 'en-US',
                    accent_detected TEXT,
                    background_noise_level REAL,
                    retry_count INTEGER DEFAULT 0,
                    was_misheard BOOLEAN DEFAULT {bool_default(0)},
                    transcription_engine TEXT DEFAULT 'browser_api',
                    audio_quality_score REAL,
                    timestamp TIMESTAMP NOT NULL,
                    FOREIGN KEY (interaction_id) REFERENCES conversation_history(interaction_id)
                )
            """
            )

            # Create index for voice_transcriptions
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_was_misheard ON voice_transcriptions(was_misheard)
            """
            )
            # Create index for voice_transcriptions
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_confidence ON voice_transcriptions(confidence_score)
            """
            )
            # Create index for voice_transcriptions
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retry_count ON voice_transcriptions(retry_count)
            """
            )

            # Speaker/Voiceprint recognition (Derek J. Russell)
            # 🔬 BEAST MODE: Comprehensive biometric features for multi-modal verification
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS speaker_profiles (
                    {auto_increment('speaker_id')},
                    speaker_name TEXT NOT NULL UNIQUE,
                    voiceprint_embedding {blob_type()},
                    embedding_dimension INTEGER,
                    total_samples INTEGER DEFAULT 0,

                    -- Legacy features (kept for backward compatibility)
                    average_pitch_hz REAL,
                    speech_rate_wpm REAL,
                    accent_profile TEXT,
                    common_phrases JSON,
                    vocabulary_preferences JSON,
                    pronunciation_patterns JSON,

                    -- 🎵 Advanced Pitch Features (fundamental frequency)
                    pitch_mean_hz REAL,
                    pitch_std_hz REAL,
                    pitch_range_hz REAL,
                    pitch_min_hz REAL,
                    pitch_max_hz REAL,

                    -- 🎼 Formant Features (vocal tract resonances)
                    formant_f1_hz REAL,
                    formant_f1_std REAL,
                    formant_f2_hz REAL,
                    formant_f2_std REAL,
                    formant_f3_hz REAL,
                    formant_f3_std REAL,
                    formant_f4_hz REAL,
                    formant_f4_std REAL,

                    -- 📊 Spectral Features (frequency distribution)
                    spectral_centroid_hz REAL,
                    spectral_centroid_std REAL,
                    spectral_rolloff_hz REAL,
                    spectral_rolloff_std REAL,
                    spectral_flux REAL,
                    spectral_flux_std REAL,
                    spectral_entropy REAL,
                    spectral_entropy_std REAL,
                    spectral_flatness REAL,
                    spectral_bandwidth_hz REAL,

                    -- ⏱️ Temporal Features (speaking patterns)
                    speaking_rate_wpm REAL,
                    speaking_rate_std REAL,
                    pause_ratio REAL,
                    pause_ratio_std REAL,
                    syllable_rate REAL,
                    articulation_rate REAL,

                    -- 🎚️ Energy Features (loudness patterns)
                    energy_mean REAL,
                    energy_std REAL,
                    energy_dynamic_range_db REAL,

                    -- 🔊 Voice Quality Features (vocal cord characteristics)
                    jitter_percent REAL,
                    jitter_std REAL,
                    shimmer_percent REAL,
                    shimmer_std REAL,
                    harmonic_to_noise_ratio_db REAL,
                    hnr_std REAL,

                    -- 📈 Statistical Features (variance and covariance)
                    feature_covariance_matrix {blob_type()},
                    feature_statistics JSON,

                    -- 🎯 Quality Metrics
                    enrollment_quality_score REAL,
                    feature_extraction_version TEXT DEFAULT 'v1.0',
                    recognition_confidence REAL DEFAULT 0.0,

                    -- 🔐 Security
                    is_primary_user BOOLEAN DEFAULT {bool_default(0)},
                    security_level TEXT DEFAULT 'standard',
                    verification_count INTEGER DEFAULT 0,
                    successful_verifications INTEGER DEFAULT 0,
                    failed_verifications INTEGER DEFAULT 0,

                    -- 📅 Timestamps
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_verified TIMESTAMP
                )
            """
            )

            # Create index for speaker_profiles
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_speaker_name ON speaker_profiles(speaker_name)
            """
            )

            # Voice samples for speaker training (Derek's voice patterns)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS voice_samples (
                    {auto_increment('sample_id')},
                    speaker_id INTEGER NOT NULL,
                    audio_hash TEXT NOT NULL,
                    audio_data {blob_type()},
                    audio_fingerprint {blob_type()},
                    mfcc_features {blob_type()},
                    duration_ms REAL NOT NULL,
                    transcription TEXT,
                    pitch_mean REAL,
                    pitch_std REAL,
                    energy_mean REAL,
                    recording_timestamp TIMESTAMP NOT NULL,
                    quality_score REAL,
                    background_noise REAL,
                    used_for_training BOOLEAN DEFAULT {bool_default(1)},
                    FOREIGN KEY (speaker_id) REFERENCES speaker_profiles(speaker_id)
                )
            """
            )

            # Create indexes for voice_samples
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_speaker ON voice_samples(speaker_id)
            """
            )
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_quality ON voice_samples(quality_score)
            """
            )

            # Acoustic adaptation (learning Derek's accent/pronunciation)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS acoustic_adaptations (
                    {auto_increment('adaptation_id')},
                    speaker_id INTEGER NOT NULL,
                    phoneme_pattern TEXT NOT NULL,
                    expected_pronunciation TEXT NOT NULL,
                    actual_pronunciation TEXT NOT NULL,
                    frequency_count INTEGER DEFAULT 1,
                    confidence REAL,
                    context_words JSON,
                    learned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_observed TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (speaker_id) REFERENCES speaker_profiles(speaker_id)
                )
            """
            )

            # Create index for acoustic_adaptations
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_speaker_phoneme ON acoustic_adaptations(speaker_id, phoneme_pattern)
            """
            )

            # Misheard queries (for learning what went wrong)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS misheard_queries (
                    {auto_increment('misheard_id')},
                    transcription_id INTEGER NOT NULL,
                    what_jarvis_heard TEXT NOT NULL,
                    what_user_meant TEXT NOT NULL,
                    correction_method TEXT,
                    acoustic_similarity_score REAL,
                    phonetic_distance INTEGER,
                    context_clues JSON,
                    learned_pattern TEXT,
                    occurred_at TIMESTAMP NOT NULL,
                    FOREIGN KEY (transcription_id) REFERENCES voice_transcriptions(transcription_id)
                )
            """
            )

            # Create index for misheard_queries
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_similarity ON misheard_queries(acoustic_similarity_score)
            """
            )
            # Create index for misheard_queries
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_occurred ON misheard_queries(occurred_at)
            """
            )

            # Retry patterns (when user has to repeat)
            await cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS query_retries (
                    {auto_increment('retry_id')},
                    original_transcription_id INTEGER NOT NULL,
                    retry_transcription_id INTEGER NOT NULL,
                    retry_number INTEGER NOT NULL,
                    time_between_retries_ms REAL,
                    confidence_improved REAL,
                    retry_reason TEXT,
                    eventually_succeeded BOOLEAN,
                    timestamp TIMESTAMP NOT NULL,
                    FOREIGN KEY (original_transcription_id) REFERENCES voice_transcriptions(transcription_id),
                    FOREIGN KEY (retry_transcription_id) REFERENCES voice_transcriptions(transcription_id)
                )
            """
            )

            # Create index for query_retries
            await cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_retry_number ON query_retries(retry_number)
            """
            )

            # Performance indexes
            await cursor.execute("CREATE INDEX IF NOT EXISTS idx_goals_type ON goals(goal_type)")
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_created ON goals(created_at)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_context_hash ON goals(context_hash)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_type ON actions(action_type)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_timestamp ON actions(timestamp)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_context_hash ON actions(context_hash)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_patterns_type ON patterns(pattern_type)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_patterns_hash ON patterns(pattern_hash)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_display_patterns_context ON display_patterns(context_hash)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_display_patterns_time ON display_patterns(connection_time, day_of_week)"
            )

            # Indexes for 24/7 behavioral learning tables
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_usage_space ON workspace_usage(space_id, timestamp)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_usage_app ON workspace_usage(app_name, timestamp)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workspace_usage_time ON workspace_usage(hour_of_day, day_of_week)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_app_usage_app ON app_usage_patterns(app_name)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_app_usage_space ON app_usage_patterns(space_id)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_app_usage_time ON app_usage_patterns(typical_time_of_day, typical_day_of_week)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_workflows_frequency ON user_workflows(frequency DESC)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_space_transitions_from ON space_transitions(from_space_id, timestamp)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_space_transitions_to ON space_transitions(to_space_id, timestamp)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_behavioral_patterns_type ON behavioral_patterns(behavior_type)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_temporal_patterns_time ON temporal_patterns(time_of_day, day_of_week)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_temporal_patterns_leap ON temporal_patterns(is_leap_year)"
            )
            await cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_suggestions_confidence ON proactive_suggestions(confidence DESC)"
            )

        await self.db.commit()
        logger.info("SQLite database initialized with enhanced async schema")

    async def _init_hybrid_sync(self):
        """Initialize hybrid database sync for voice biometrics"""
        try:
            from intelligence.hybrid_database_sync import HybridDatabaseSync
            import json

            # Load CloudSQL config
            config_path = Path.home() / ".jarvis" / "gcp" / "database_config.json"
            if not config_path.exists():
                logger.warning("⚠️  CloudSQL config not found - hybrid sync disabled")
                self._sync_enabled = False
                return

            with open(config_path, 'r') as f:
                gcp_config = json.load(f)

            cloudsql_config = gcp_config.get("cloud_sql", {})

            # Initialize ADVANCED hybrid sync V2.0 with Phase 2 features
            sqlite_sync_path = self.db_dir / "voice_biometrics_sync.db"
            self.hybrid_sync = HybridDatabaseSync.get_instance(
                sqlite_path=sqlite_sync_path,
                cloudsql_config=cloudsql_config,
                sync_interval_seconds=30,
                max_retry_attempts=5,
                batch_size=50,
                max_connections=3,  # 🚀 Reduced from 10 to prevent exhaustion
                enable_faiss_cache=True,  # 🚀 Sub-millisecond FAISS cache
                enable_prometheus=True,  # 🚀 Phase 2: Prometheus metrics on port 9090
                enable_redis=True,  # 🚀 Phase 2: Redis distributed metrics
                enable_ml_prefetch=True,  # 🚀 Phase 2: ML-based predictive cache warming
                prometheus_port=9090,
                redis_url="redis://localhost:6379"
            )

            await self.hybrid_sync.initialize()
            logger.info("✅ Advanced hybrid sync V2.0 enabled - zero live queries mode")
            logger.info(f"   Local: {sqlite_sync_path}")
            logger.info(f"   Cloud: {cloudsql_config.get('instance_name', 'unknown')}")
            logger.info(f"   📊 Phase 2 Features:")
            logger.info(f"      Connection Orchestrator: 3 max connections")
            logger.info(f"      FAISS Cache: Enabled")
            logger.info(f"      Prometheus Metrics: http://localhost:9090/metrics")
            logger.info(f"      Redis Metrics: Enabled")
            logger.info(f"      ML Prefetcher: Enabled")

        except Exception as e:
            logger.warning(f"⚠️  Hybrid sync initialization failed: {e}")
            logger.info("   Continuing with standard database access")
            self.hybrid_sync = None
            self._sync_enabled = False

    async def _init_chromadb(self):
        """Initialize ChromaDB for embeddings and semantic search"""
        try:
            # Disable ChromaDB telemetry via environment variable (belt and suspenders)
            import os

            os.environ["ANONYMIZED_TELEMETRY"] = "False"

            # Suppress ChromaDB telemetry errors and logging
            import logging as stdlib_logging

            stdlib_logging.getLogger("chromadb.telemetry").setLevel(stdlib_logging.CRITICAL)

            # Monkey-patch ChromaDB telemetry to prevent errors in older versions
            try:
                from chromadb.telemetry.product import posthog

                # Replace capture method with no-op to prevent API mismatch errors
                if hasattr(posthog, "Posthog"):
                    original_capture = (
                        posthog.Posthog.capture if hasattr(posthog.Posthog, "capture") else None
                    )
                    if original_capture:

                        def noop_capture(self, *args, **kwargs):
                            pass

                        posthog.Posthog.capture = noop_capture
            except (ImportError, AttributeError):
                pass  # Telemetry module not available or already disabled

            # Initialize ChromaDB client with persistent storage
            try:
                self.chroma_client = chromadb.PersistentClient(
                    path=str(self.chroma_path),
                    settings=Settings(anonymized_telemetry=False, allow_reset=True),
                )

                # Create or get collections with metadata
                self.goal_collection = self.chroma_client.get_or_create_collection(
                    name="goal_embeddings",
                    metadata={
                        "description": "Goal context embeddings for similarity search",
                        "created": datetime.now().isoformat(),
                    },
                )

                self.pattern_collection = self.chroma_client.get_or_create_collection(
                    name="pattern_embeddings",
                    metadata={
                        "description": "Pattern embeddings for matching",
                        "created": datetime.now().isoformat(),
                    },
                )

                self.context_collection = self.chroma_client.get_or_create_collection(
                    name="context_embeddings",
                    metadata={
                        "description": "Context state embeddings for prediction",
                        "created": datetime.now().isoformat(),
                    },
                )

                logger.info("ChromaDB initialized for semantic search")

            except Exception as chroma_error:
                # Check if it's a schema error
                if "no such column" in str(chroma_error).lower():
                    logger.warning(f"ChromaDB schema mismatch detected: {chroma_error}")
                    logger.info("Resetting ChromaDB to fix schema issue...")

                    # Reset and recreate the client
                    try:
                        import shutil
                        if self.chroma_path.exists():
                            # Backup old database
                            backup_path = self.chroma_path.parent / f"chroma_embeddings_backup_{int(time.time())}"
                            shutil.move(str(self.chroma_path), str(backup_path))
                            logger.info(f"Backed up old ChromaDB to: {backup_path}")

                        # Create fresh ChromaDB instance
                        self.chroma_client = chromadb.PersistentClient(
                            path=str(self.chroma_path),
                            settings=Settings(anonymized_telemetry=False, allow_reset=True),
                        )

                        # Create collections
                        self.goal_collection = self.chroma_client.get_or_create_collection(
                            name="goal_embeddings",
                            metadata={
                                "description": "Goal context embeddings for similarity search",
                                "created": datetime.now().isoformat(),
                            },
                        )

                        self.pattern_collection = self.chroma_client.get_or_create_collection(
                            name="pattern_embeddings",
                            metadata={
                                "description": "Pattern embeddings for matching",
                                "created": datetime.now().isoformat(),
                            },
                        )

                        self.context_collection = self.chroma_client.get_or_create_collection(
                            name="context_embeddings",
                            metadata={
                                "description": "Context state embeddings for prediction",
                                "created": datetime.now().isoformat(),
                            },
                        )

                        logger.info("ChromaDB reset complete - fresh database created")
                    except Exception as reset_error:
                        logger.error(f"Failed to reset ChromaDB: {reset_error}")
                        self.chroma_client = None
                else:
                    raise chroma_error

        except Exception as e:
            logger.error(f"Failed to initialize ChromaDB: {e}")
            self.chroma_client = None

    async def _init_cloud_adapter(self):
        """
        Initialize Cloud Database Adapter for redundant Cloud SQL storage (v10.6).

        This enables parallel writes to both local SQLite and Cloud SQL for voice samples,
        providing redundancy and enabling multi-device access to voice biometric data.

        Features:
        - Timeout protection (15s) to prevent hangs if Cloud SQL proxy isn't running
        - Graceful fallback to SQLite-only if Cloud SQL unavailable
        - Smart detection of Cloud SQL vs SQLite configuration
        - Configuration-driven behavior (no hardcoding)
        - Comprehensive error handling with detailed logging
        - Async initialization for non-blocking startup

        If initialization fails, self.cloud_adapter remains None and all voice sample
        writes will fall back to local SQLite only (existing behavior).
        """
        try:
            logger.info("🔧 Initializing Cloud Database Adapter for redundant voice storage...")

            # STEP 1: Create CloudDatabaseAdapter instance
            # This reads configuration from environment variables and config files
            try:
                cloud_config = DatabaseConfig()

                # Log configuration status (for debugging)
                logger.info(
                    f"   Database config: type={cloud_config.db_type}, "
                    f"cloud_sql={cloud_config.use_cloud_sql}"
                )

                # Create adapter instance
                self.cloud_adapter = CloudDatabaseAdapter(config=cloud_config)

            except Exception as config_error:
                logger.warning(
                    f"⚠️  Failed to create Cloud Database Adapter config: {config_error}\n"
                    f"   → Falling back to SQLite-only storage"
                )
                self.cloud_adapter = None
                return

            # STEP 2: Initialize the adapter with timeout protection
            # This prevents infinite hangs if Cloud SQL proxy isn't running
            try:
                logger.info("   Initializing adapter (15s timeout)...")
                await asyncio.wait_for(
                    self.cloud_adapter.initialize(),
                    timeout=15.0  # 15 second timeout
                )

            except asyncio.TimeoutError:
                logger.warning(
                    "⏱️  Cloud Database Adapter initialization timeout (15s exceeded)\n"
                    "   This usually means Cloud SQL proxy isn't running.\n"
                    "   → Falling back to SQLite-only storage"
                )
                self.cloud_adapter = None
                return

            except Exception as init_error:
                logger.warning(
                    f"⚠️  Cloud Database Adapter initialization failed: {init_error}\n"
                    f"   → Falling back to SQLite-only storage"
                )
                self.cloud_adapter = None
                return

            # STEP 3: Verify the adapter is actually using Cloud SQL
            # If it fell back to SQLite, we don't need it (we already have self.db)
            if not self.cloud_adapter.is_cloud:
                logger.info(
                    "📂 Cloud Database Adapter is using SQLite (not Cloud SQL)\n"
                    "   → Using local self.db for all storage (no redundancy needed)"
                )
                self.cloud_adapter = None
                return

            # STEP 4: Success! We have a working Cloud SQL adapter
            logger.info(
                "✅ Cloud Database Adapter initialized successfully\n"
                "   → Voice samples will be written to BOTH:\n"
                "      • Local SQLite (fast, always available)\n"
                "      • Cloud SQL (redundant, multi-device accessible)"
            )

        except Exception as e:
            # Catch-all: Any unexpected error should not crash initialization
            logger.error(
                f"❌ Unexpected error in Cloud Database Adapter initialization: {e}\n"
                f"   → Falling back to SQLite-only storage"
            )
            self.cloud_adapter = None

    # ==================== Goal Management (Async + Cached) ====================

    async def store_goal(self, goal: Dict[str, Any], batch: bool = False) -> str:
        """Store an inferred goal with batching support"""
        goal_id = goal.get("goal_id", self._generate_id("goal"))

        if batch:
            self.pending_goals.append((goal_id, goal))
            if len(self.pending_goals) >= self.batch_size:
                await self._flush_goal_batch()
            return goal_id

        # Compute context hash for deduplication
        context_hash = self._hash_context(goal.get("evidence", {}))

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT OR REPLACE INTO goals
                    (goal_id, goal_type, goal_level, description, confidence,
                     progress, is_completed, created_at, evidence, context_hash, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        goal_id,
                        goal["goal_type"],
                        goal.get("goal_level", "UNKNOWN"),
                        goal.get("description", ""),
                        goal.get("confidence", 0.0),
                        goal.get("progress", 0.0),
                        goal.get("is_completed", False),
                        goal.get("created_at", datetime.now()),
                        json.dumps(goal.get("evidence", [])),
                        context_hash,
                        json.dumps(goal.get("metadata", {})),
                    ),
                )

            await self.db.commit()

        # Store embedding in ChromaDB if available
        if self.goal_collection and "embedding" in goal:
            await self._store_embedding_async(
                self.goal_collection,
                goal_id,
                goal["embedding"],
                {
                    "goal_type": goal["goal_type"],
                    "confidence": goal.get("confidence", 0.0),
                    "timestamp": datetime.now().isoformat(),
                },
            )

        # Update cache
        self.goal_cache.set(goal_id, goal)

        return goal_id

    async def get_similar_goals(
        self, embedding: List[float], top_k: int = 5, min_confidence: float = 0.5
    ) -> List[Dict]:
        """Find similar goals using semantic search with caching"""
        cache_key = f"similar_goals_{hashlib.md5(str(embedding).encode(), usedforsecurity=False).hexdigest()}_{top_k}"

        cached = self.query_cache.get(cache_key)
        if cached:
            return cached

        if not self.goal_collection:
            return []

        try:
            results = self.goal_collection.query(
                query_embeddings=[embedding],
                n_results=top_k,
                where={"confidence": {"$gte": min_confidence}} if min_confidence > 0 else None,
            )

            similar_goals = []
            async with self.db.cursor() as cursor:
                for i, goal_id in enumerate(results["ids"][0]):
                    await cursor.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,))
                    row = await cursor.fetchone()

                    if row:
                        goal = dict(row)
                        goal["evidence"] = json.loads(goal["evidence"]) if goal["evidence"] else []
                        goal["metadata"] = json.loads(goal["metadata"]) if goal["metadata"] else {}
                        goal["similarity"] = 1.0 - results["distances"][0][i]
                        similar_goals.append(goal)

            self.query_cache.set(cache_key, similar_goals)
            return similar_goals

        except Exception as e:
            logger.error(f"Error finding similar goals: {e}")
            return []

    # ==================== Action Tracking (Async + Metrics) ====================

    async def store_action(self, action: Dict[str, Any], batch: bool = False) -> str:
        """Store an executed action with performance tracking"""
        action_id = action.get("action_id", self._generate_id("action"))

        if batch:
            self.pending_actions.append((action_id, action))
            if len(self.pending_actions) >= self.batch_size:
                await self._flush_action_batch()
            return action_id

        context_hash = self._hash_context(action.get("params", {}))

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO actions
                    (action_id, action_type, target, goal_id, confidence,
                     success, execution_time, timestamp, retry_count, params, result, context_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        action_id,
                        action["action_type"],
                        action.get("target", ""),
                        action.get("goal_id"),
                        action.get("confidence", 0.0),
                        action.get("success", False),
                        action.get("execution_time", 0.0),
                        action.get("timestamp", datetime.now()),
                        action.get("retry_count", 0),
                        json.dumps(action.get("params", {})),
                        json.dumps(action.get("result", {})),
                        context_hash,
                    ),
                )

            await self.db.commit()

        # Update goal-action mapping
        if action.get("goal_id"):
            await self._update_goal_action_mapping(
                action["action_type"],
                action.get("goal_id"),
                action.get("success", False),
                action.get("execution_time", 0.0),
            )

        return action_id

    async def _update_goal_action_mapping(
        self, action_type: str, goal_id: Optional[str], success: bool, execution_time: float
    ):
        """Update goal-action mapping with statistical tracking"""
        if not goal_id:
            return

        async with self.db.cursor() as cursor:
            # Get goal type
            await cursor.execute("SELECT goal_type FROM goals WHERE goal_id = ?", (goal_id,))
            row = await cursor.fetchone()

            if row:
                goal_type = row["goal_type"]

                # Calculate new statistics
                success_inc = 1 if success else 0
                failure_inc = 0 if success else 1

                await cursor.execute(
                    """
                    INSERT INTO goal_action_mappings
                    (goal_type, action_type, success_count, failure_count,
                     avg_execution_time, confidence, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(goal_type, action_type) DO UPDATE SET
                        success_count = success_count + ?,
                        failure_count = failure_count + ?,
                        avg_execution_time = (avg_execution_time * (success_count + failure_count) + ?)
                                            / (success_count + failure_count + 1),
                        confidence = CAST(success_count + ? AS REAL) / (success_count + failure_count + 1),
                        last_updated = ?
                """,
                    (
                        goal_type,
                        action_type,
                        success_inc,
                        failure_inc,
                        execution_time,
                        0.5,
                        datetime.now(),
                        success_inc,
                        failure_inc,
                        execution_time,
                        success_inc,
                        datetime.now(),
                    ),
                )

        await self.db.commit()

    # ==================== Conversation History & Learning ====================

    async def record_interaction(
        self,
        user_query: str,
        jarvis_response: str,
        response_type: Optional[str] = None,
        confidence_score: Optional[float] = None,
        execution_time_ms: Optional[float] = None,
        success: bool = True,
        session_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Record every user-JARVIS interaction for learning and improvement.

        Args:
            user_query: What the user said/typed
            jarvis_response: How JARVIS responded
            response_type: Type of response (command, conversation, error, etc.)
            confidence_score: JARVIS's confidence in the response (0-1)
            execution_time_ms: How long it took to respond
            success: Whether the response was successful
            session_id: Unique session identifier
            context: Full context snapshot (apps, spaces, system state, etc.)

        Returns:
            int: interaction_id for reference
        """
        try:
            import uuid

            embedding_id = str(uuid.uuid4())

            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO conversation_history
                    (timestamp, session_id, user_query, jarvis_response, response_type,
                     confidence_score, execution_time_ms, success, context_snapshot,
                     active_apps, current_space, system_state, embedding_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        datetime.now(),
                        session_id,
                        user_query,
                        jarvis_response,
                        response_type,
                        confidence_score,
                        execution_time_ms,
                        1 if success else 0,
                        json.dumps(context or {}),
                        json.dumps(context.get("active_apps", []) if context else []),
                        context.get("current_space") if context else None,
                        json.dumps(context.get("system_state", {}) if context else {}),
                        embedding_id,
                    ),
                )

                await self.db.commit()

                # Get the interaction_id
                interaction_id = cursor.lastrowid

                # Generate embeddings asynchronously (non-blocking)
                if self.enable_ml:
                    asyncio.create_task(
                        self._generate_conversation_embeddings(
                            interaction_id, embedding_id, user_query, jarvis_response
                        )
                    )

                logger.debug(
                    f"Recorded interaction {interaction_id}: '{user_query[:50]}...' -> '{jarvis_response[:50]}...'"
                )

                return interaction_id

        except Exception as e:
            logger.error(f"Failed to record interaction: {e}")
            return -1

    async def record_correction(
        self,
        interaction_id: int,
        corrected_response: str,
        correction_type: str,
        user_explanation: Optional[str] = None,
    ) -> int:
        """
        Record when user corrects JARVIS's response (critical for learning!).

        Args:
            interaction_id: ID of the original interaction
            corrected_response: What the correct response should have been
            correction_type: Type of correction (misunderstanding, wrong_action, etc.)
            user_explanation: User's explanation of what went wrong

        Returns:
            int: correction_id
        """
        try:
            async with self.db.cursor() as cursor:
                # Get original response
                await cursor.execute(
                    "SELECT jarvis_response FROM conversation_history WHERE interaction_id = ?",
                    (interaction_id,),
                )
                row = await cursor.fetchone()

                if not row:
                    logger.error(f"Interaction {interaction_id} not found for correction")
                    return -1

                original_response = row["jarvis_response"]

                # Insert correction
                await cursor.execute(
                    """
                    INSERT INTO interaction_corrections
                    (interaction_id, original_response, corrected_response,
                     correction_type, user_explanation, applied_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        interaction_id,
                        original_response,
                        corrected_response,
                        correction_type,
                        user_explanation,
                        datetime.now(),
                    ),
                )

                # Mark the original interaction as corrected
                await cursor.execute(
                    """
                    UPDATE conversation_history
                    SET was_corrected = 1, correction_text = ?
                    WHERE interaction_id = ?
                """,
                    (corrected_response, interaction_id),
                )

                await self.db.commit()

                correction_id = cursor.lastrowid

                logger.info(
                    f"Recorded correction {correction_id} for interaction {interaction_id}: "
                    f"{correction_type}"
                )

                # Extract learning pattern from correction (ML task)
                asyncio.create_task(self._extract_correction_pattern(correction_id))

                return correction_id

        except Exception as e:
            logger.error(f"Failed to record correction: {e}")
            return -1

    async def add_user_feedback(
        self,
        interaction_id: int,
        feedback_text: Optional[str] = None,
        feedback_score: Optional[int] = None,
    ) -> bool:
        """
        Record user feedback on JARVIS's response (thumbs up/down, rating, comment).

        Args:
            interaction_id: ID of the interaction
            feedback_text: User's feedback comment
            feedback_score: Numeric score (-1, 0, 1 or 1-5 scale)

        Returns:
            bool: Success
        """
        try:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE conversation_history
                    SET user_feedback = ?, feedback_score = ?
                    WHERE interaction_id = ?
                """,
                    (feedback_text, feedback_score, interaction_id),
                )

                await self.db.commit()

                logger.debug(
                    f"Added feedback to interaction {interaction_id}: score={feedback_score}"
                )

                return True

        except Exception as e:
            logger.error(f"Failed to add feedback: {e}")
            return False

    async def _generate_conversation_embeddings(
        self, interaction_id: int, embedding_id: str, query: str, response: str
    ):
        """Generate semantic embeddings for conversation search (async background task)"""
        try:
            # Import embedding model (lazy load)
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer("all-MiniLM-L6-v2")

            # Generate embeddings
            query_embedding = model.encode(query)
            response_embedding = model.encode(response)
            combined_embedding = model.encode(f"{query} {response}")

            # Store embeddings
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO conversation_embeddings
                    (embedding_id, interaction_id, query_embedding, response_embedding, combined_embedding)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        embedding_id,
                        interaction_id,
                        query_embedding.tobytes(),
                        response_embedding.tobytes(),
                        combined_embedding.tobytes(),
                    ),
                )

                await self.db.commit()

        except ImportError:
            logger.debug("sentence-transformers not installed - skipping embeddings")
        except Exception as e:
            logger.error(f"Failed to generate conversation embeddings: {e}")

    async def _extract_correction_pattern(self, correction_id: int):
        """Extract learning pattern from user correction (ML-powered)"""
        try:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT ic.*, ch.user_query, ch.context_snapshot
                    FROM interaction_corrections ic
                    JOIN conversation_history ch ON ic.interaction_id = ch.interaction_id
                    WHERE ic.correction_id = ?
                """,
                    (correction_id,),
                )

                row = await cursor.fetchone()
                if not row:
                    return

                # Extract pattern from correction
                # This is where ML/LLM could analyze what went wrong and create a rule
                # For now, store basic pattern
                pattern_text = f"When user says '{row['user_query']}', respond with '{row['corrected_response']}' not '{row['original_response']}'"

                await cursor.execute(
                    """
                    UPDATE interaction_corrections
                    SET pattern_extracted = ?, learned = 1
                    WHERE correction_id = ?
                """,
                    (pattern_text, correction_id),
                )

                await self.db.commit()

                logger.info(f"Extracted pattern from correction {correction_id}")

        except Exception as e:
            logger.error(f"Failed to extract correction pattern: {e}")

    async def search_similar_conversations(
        self, query: str, top_k: int = 5, min_confidence: float = 0.5
    ) -> List[Dict[str, Any]]:
        """
        Search past conversations semantically using embeddings.

        Args:
            query: Search query
            top_k: Number of results to return
            min_confidence: Minimum similarity score

        Returns:
            List of similar past interactions with scores
        """
        try:
            import numpy as np
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer("all-MiniLM-L6-v2")
            query_embedding = model.encode(query)

            # Get all conversation embeddings
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT ce.*, ch.user_query, ch.jarvis_response, ch.success, ch.timestamp
                    FROM conversation_embeddings ce
                    JOIN conversation_history ch ON ce.interaction_id = ch.interaction_id
                """
                )

                rows = await cursor.fetchall()

            # Calculate similarity scores
            results = []
            for row in rows:
                combined_embedding = np.frombuffer(row["combined_embedding"], dtype=np.float32)

                # Cosine similarity
                similarity = np.dot(query_embedding, combined_embedding) / (
                    np.linalg.norm(query_embedding) * np.linalg.norm(combined_embedding)
                )

                if similarity >= min_confidence:
                    results.append(
                        {
                            "interaction_id": row["interaction_id"],
                            "user_query": row["user_query"],
                            "jarvis_response": row["jarvis_response"],
                            "success": row["success"],
                            "timestamp": row["timestamp"],
                            "similarity_score": float(similarity),
                        }
                    )

            # Sort by similarity and return top_k
            results.sort(key=lambda x: x["similarity_score"], reverse=True)
            return results[:top_k]

        except ImportError:
            logger.warning("sentence-transformers not installed - semantic search unavailable")
            return []
        except Exception as e:
            logger.error(f"Failed to search conversations: {e}")
            return []

    # ==================== Voice Recognition & Speaker Learning ====================

    async def record_voice_transcription(
        self,
        audio_data: bytes,
        transcribed_text: str,
        confidence_score: float,
        audio_duration_ms: float,
        interaction_id: Optional[int] = None,
    ) -> int:
        """
        Record voice transcription for accuracy tracking and learning.
        Tracks misheards, retries, and acoustic patterns for continuous improvement.
        """
        try:
            import hashlib

            audio_hash = hashlib.sha256(audio_data).hexdigest()[:16]

            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO voice_transcriptions
                    (interaction_id, raw_audio_hash, transcribed_text, confidence_score,
                     audio_duration_ms, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        interaction_id,
                        audio_hash,
                        transcribed_text,
                        confidence_score,
                        audio_duration_ms,
                        datetime.now(),
                    ),
                )

                await self.db.commit()
                transcription_id = cursor.lastrowid

                logger.debug(
                    f"🎤 Recorded voice transcription {transcription_id}: '{transcribed_text[:50]}...'"
                )

                return transcription_id

        except Exception as e:
            logger.error(f"Failed to record voice transcription: {e}")
            return -1

    async def record_misheard_query(
        self,
        transcription_id: int,
        what_jarvis_heard: str,
        what_user_meant: str,
        correction_method: str = "user_correction",
    ) -> int:
        """
        Record when JARVIS mishears - critical for learning!
        This trains the acoustic model to handle Derek's accent better.
        """
        try:
            async with self.db.cursor() as cursor:
                # Mark original transcription as misheard
                await cursor.execute(
                    """
                    UPDATE voice_transcriptions
                    SET was_misheard = 1, corrected_text = ?
                    WHERE transcription_id = ?
                """,
                    (what_user_meant, transcription_id),
                )

                # Calculate phonetic distance
                phonetic_distance = self._calculate_phonetic_distance(
                    what_jarvis_heard, what_user_meant
                )

                # Insert misheard record
                await cursor.execute(
                    """
                    INSERT INTO misheard_queries
                    (transcription_id, what_jarvis_heard, what_user_meant,
                     correction_method, phonetic_distance, occurred_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """,
                    (
                        transcription_id,
                        what_jarvis_heard,
                        what_user_meant,
                        correction_method,
                        phonetic_distance,
                        datetime.now(),
                    ),
                )

                await self.db.commit()
                misheard_id = cursor.lastrowid

                logger.info(
                    f"🎤 MISHEARD: Heard '{what_jarvis_heard}' but user meant '{what_user_meant}' (distance={phonetic_distance})"
                )

                # Trigger acoustic adaptation learning
                asyncio.create_task(self._learn_acoustic_pattern(misheard_id))

                return misheard_id

        except Exception as e:
            logger.error(f"Failed to record misheard query: {e}")
            return -1

    # ==========================================================================
    # INVALID SPEAKER NAMES - These should NEVER create profiles
    # ==========================================================================
    INVALID_SPEAKER_NAMES = frozenset({
        'unknown', 'test', 'placeholder', 'anonymous', 'guest',
        'none', 'null', 'undefined', '', ' ',
    })

    async def get_or_create_speaker_profile(self, speaker_name: str = "Derek J. Russell") -> int:
        """
        Get or create speaker profile for voice recognition.
        Derek J. Russell is the primary user.

        VALIDATION: Rejects invalid/placeholder speaker names to prevent
        garbage profiles from being created in the database.
        """
        # =======================================================================
        # VALIDATION: Prevent invalid speaker names from creating profiles
        # =======================================================================
        if not speaker_name or not speaker_name.strip():
            logger.warning(f"⚠️ Rejected empty speaker name - not creating profile")
            return -1

        speaker_name_lower = speaker_name.strip().lower()
        if speaker_name_lower in self.INVALID_SPEAKER_NAMES:
            logger.debug(
                f"🚫 Rejected invalid speaker name '{speaker_name}' - "
                "not creating profile for placeholder names"
            )
            return -1

        # Additional validation: must be at least 2 characters
        if len(speaker_name.strip()) < 2:
            logger.warning(f"⚠️ Rejected speaker name '{speaker_name}' - too short")
            return -1

        try:
            db = self._ensure_db_initialized()
            # Check if using Cloud SQL (PostgreSQL) or SQLite
            is_cloud = isinstance(db, DatabaseConnectionWrapper) and db.is_cloud

            async with db.cursor() as cursor:
                # Check if profile exists
                if is_cloud:
                    await cursor.execute(
                        "SELECT speaker_id FROM speaker_profiles WHERE speaker_name = %s",
                        (speaker_name,),
                    )
                else:
                    await cursor.execute(
                        "SELECT speaker_id FROM speaker_profiles WHERE speaker_name = ?",
                        (speaker_name,),
                    )
                row = await cursor.fetchone()

                if row:
                    return row["speaker_id"] if isinstance(row, dict) else row[0]

                # Create new profile
                if is_cloud:
                    await cursor.execute(
                        "INSERT INTO speaker_profiles (speaker_name) VALUES (%s) RETURNING speaker_id",
                        (speaker_name,),
                    )
                    row = await cursor.fetchone()
                    speaker_id = row["speaker_id"] if isinstance(row, dict) else row[0]
                else:
                    await cursor.execute(
                        "INSERT INTO speaker_profiles (speaker_name) VALUES (?)",
                        (speaker_name,),
                    )
                    speaker_id = cursor.lastrowid

                await db.commit()

                logger.info(f"👤 Created speaker profile for {speaker_name} (ID: {speaker_id})")

                return speaker_id

        except Exception as e:
            logger.error(f"Failed to get/create speaker profile: {e}")
            return -1

    async def cleanup_invalid_speaker_profiles(self) -> Dict[str, Any]:
        """
        Remove invalid/placeholder speaker profiles from the database.

        This cleans up profiles like 'unknown', 'test', etc. that were
        accidentally created and have no valid embedding.

        Returns:
            Dict with cleanup results
        """
        result = {
            'cleaned_profiles': [],
            'errors': [],
            'total_removed': 0,
        }

        try:
            db = self._ensure_db_initialized()
            is_cloud = isinstance(db, DatabaseConnectionWrapper) and db.is_cloud

            async with db.cursor() as cursor:
                # Find invalid profiles (no embedding or invalid name)
                invalid_names_list = list(self.INVALID_SPEAKER_NAMES)

                if is_cloud:
                    # PostgreSQL: Find profiles with invalid names OR missing embeddings
                    placeholders = ', '.join(['%s'] * len(invalid_names_list))
                    await cursor.execute(f"""
                        SELECT speaker_id, speaker_name, voiceprint_embedding IS NULL as missing_embedding
                        FROM speaker_profiles
                        WHERE LOWER(speaker_name) IN ({placeholders})
                           OR voiceprint_embedding IS NULL
                    """, invalid_names_list)
                else:
                    # SQLite
                    placeholders = ', '.join(['?'] * len(invalid_names_list))
                    await cursor.execute(f"""
                        SELECT speaker_id, speaker_name, voiceprint_embedding IS NULL as missing_embedding
                        FROM speaker_profiles
                        WHERE LOWER(speaker_name) IN ({placeholders})
                           OR voiceprint_embedding IS NULL
                    """, invalid_names_list)

                invalid_profiles = await cursor.fetchall()

                for profile in invalid_profiles:
                    speaker_id = profile[0] if isinstance(profile, (list, tuple)) else profile['speaker_id']
                    speaker_name = profile[1] if isinstance(profile, (list, tuple)) else profile['speaker_name']
                    missing_embedding = profile[2] if isinstance(profile, (list, tuple)) else profile['missing_embedding']

                    # Don't delete primary users even if embedding is temporarily missing
                    # Check is_primary_user flag dynamically
                    if is_cloud:
                        await cursor.execute(
                            "SELECT is_primary_user FROM speaker_profiles WHERE speaker_id = %s",
                            (speaker_id,)
                        )
                    else:
                        await cursor.execute(
                            "SELECT is_primary_user FROM speaker_profiles WHERE speaker_id = ?",
                            (speaker_id,)
                        )
                    primary_check = await cursor.fetchone()
                    is_primary = primary_check and (
                        primary_check.get('is_primary_user', False) if isinstance(primary_check, dict)
                        else primary_check[0]
                    )

                    if is_primary:
                        logger.info(f"⚠️ Skipping cleanup of primary user profile: {speaker_name}")
                        continue

                    try:
                        # Delete associated voice samples first
                        if is_cloud:
                            await cursor.execute(
                                "DELETE FROM voice_samples WHERE speaker_id = %s",
                                (speaker_id,)
                            )
                            await cursor.execute(
                                "DELETE FROM speaker_profiles WHERE speaker_id = %s",
                                (speaker_id,)
                            )
                        else:
                            await cursor.execute(
                                "DELETE FROM voice_samples WHERE speaker_id = ?",
                                (speaker_id,)
                            )
                            await cursor.execute(
                                "DELETE FROM speaker_profiles WHERE speaker_id = ?",
                                (speaker_id,)
                            )

                        reason = "invalid name" if speaker_name.lower() in self.INVALID_SPEAKER_NAMES else "missing embedding"
                        result['cleaned_profiles'].append({
                            'speaker_id': speaker_id,
                            'speaker_name': speaker_name,
                            'reason': reason,
                        })
                        result['total_removed'] += 1

                        logger.info(f"🧹 Cleaned up invalid profile: {speaker_name} ({reason})")

                    except Exception as e:
                        result['errors'].append(f"Failed to delete {speaker_name}: {e}")
                        logger.error(f"Failed to cleanup profile {speaker_name}: {e}")

                await db.commit()

            logger.info(f"✅ Profile cleanup complete: {result['total_removed']} removed")
            return result

        except Exception as e:
            logger.error(f"Failed to cleanup invalid profiles: {e}")
            result['errors'].append(str(e))
            return result

    async def record_voice_sample(
        self,
        speaker_name: str,
        audio_data: bytes,
        transcription: str,
        audio_duration_ms: float,
        quality_score: float = 1.0,
    ) -> int:
        """
        Record voice sample for speaker training (Derek's voiceprint).
        Collects acoustic features for voice recognition and adaptation.
        """
        try:
            import hashlib

            speaker_id = await self.get_or_create_speaker_profile(speaker_name)

            if speaker_id == -1:
                logger.error(f"Failed to get/create speaker profile for {speaker_name}")
                return -1

            audio_hash = hashlib.sha256(audio_data).hexdigest()[:16]

            # Extract acoustic features (if librosa available)
            # Run in thread pool to avoid blocking event loop
            mfcc_features = None
            pitch_mean = None
            pitch_std = None
            energy_mean = None

            def _extract_acoustic_features_sync():
                """Extract acoustic features synchronously - runs in thread pool."""
                import io
                import librosa
                import numpy as np

                # Convert bytes to audio
                audio_array, sr = librosa.load(io.BytesIO(audio_data), sr=16000)

                # Extract MFCC features (13 coefficients)
                mfccs = librosa.feature.mfcc(y=audio_array, sr=sr, n_mfcc=13)
                mfcc_features = np.mean(mfccs, axis=1).tobytes()

                # Extract pitch
                pitches, magnitudes = librosa.piptrack(y=audio_array, sr=sr)
                pitch_values = pitches[pitches > 0]
                pitch_mean = float(np.mean(pitch_values)) if len(pitch_values) > 0 else None
                pitch_std = float(np.std(pitch_values)) if len(pitch_values) > 0 else None

                # Extract energy
                energy = librosa.feature.rms(y=audio_array)
                energy_mean = float(np.mean(energy))

                return mfcc_features, pitch_mean, pitch_std, energy_mean

            try:
                # Run CPU-intensive librosa operations in thread pool
                import asyncio
                mfcc_features, pitch_mean, pitch_std, energy_mean = await asyncio.to_thread(
                    _extract_acoustic_features_sync
                )
                if pitch_mean:
                    logger.debug(
                        f"🎵 Extracted acoustic features: pitch={pitch_mean:.1f}Hz, energy={energy_mean:.3f}"
                    )

            except ImportError:
                logger.debug("librosa not available - storing audio hash only")
            except Exception as e:
                logger.warning(f"Failed to extract acoustic features: {e}")

            # Store to BOTH SQLite and Cloud SQL for synchronization

            # 1. Store to database (SQLite or Cloud SQL)
            async with self.db.cursor() as cursor:

                # Check if we're using PostgreSQL (Cloud SQL) or SQLite
                if isinstance(self.db, DatabaseConnectionWrapper):
                    # PostgreSQL - use RETURNING clause with proper placeholders
                    await cursor.execute(
                        """
                        INSERT INTO voice_samples
                        (speaker_id, audio_hash, audio_data, mfcc_features, duration_ms, transcription,
                         pitch_mean, pitch_std, energy_mean, recording_timestamp, quality_score)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING sample_id
                    """,
                        (
                            speaker_id,
                            audio_hash,
                            audio_data,  # Store raw audio for embedding reconstruction
                            mfcc_features,
                            audio_duration_ms,
                            transcription,
                            pitch_mean,
                            pitch_std,
                            energy_mean,
                            datetime.now(),
                            quality_score,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row:
                        # Row might be dict-like or tuple
                        sample_id = row['sample_id'] if hasattr(row, '__getitem__') and 'sample_id' in row else row[0]
                    else:
                        sample_id = None
                else:
                    # SQLite - use ? placeholders without RETURNING
                    await cursor.execute(
                        """
                        INSERT INTO voice_samples
                        (speaker_id, audio_hash, audio_data, mfcc_features, duration_ms, transcription,
                         pitch_mean, pitch_std, energy_mean, recording_timestamp, quality_score)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            speaker_id,
                            audio_hash,
                            audio_data,  # Store raw audio for embedding reconstruction
                            mfcc_features,
                            audio_duration_ms,
                            transcription,
                            pitch_mean,
                            pitch_std,
                            energy_mean,
                            datetime.now(),
                            quality_score,
                        ),
                    )
                    sample_id = cursor.lastrowid

                # Update speaker profile stats
                await cursor.execute(
                    """
                    UPDATE speaker_profiles
                    SET total_samples = total_samples + 1,
                        average_pitch_hz = COALESCE(
                            (average_pitch_hz * total_samples + ?) / (total_samples + 1),
                            ?
                        ),
                        last_updated = ?
                    WHERE speaker_id = ?
                """,
                    (pitch_mean, pitch_mean, datetime.now(), speaker_id),
                )

                await self.db.commit()

            # 2. ALSO store to Cloud SQL (persistent storage)
            if hasattr(self, 'adapter') and self.adapter:
                try:
                    # PostgreSQL uses $1, $2 placeholders and BYTEA for binary data
                    cloud_query = """
                        INSERT INTO voice_samples
                        (speaker_id, audio_hash, audio_data, mfcc_features, duration_ms,
                         transcription, pitch_mean, pitch_std, energy_mean,
                         recording_timestamp, quality_score, sample_rate)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        RETURNING sample_id
                    """

                    result = await self.adapter.fetch(
                        cloud_query,
                        speaker_id,
                        audio_hash,
                        audio_data,  # PostgreSQL will handle BYTEA conversion
                        mfcc_features if mfcc_features else None,
                        int(audio_duration_ms) if audio_duration_ms else 0,
                        transcription or '',
                        pitch_mean,
                        pitch_std,
                        energy_mean,
                        datetime.now(),
                        float(quality_score) if quality_score else 0.0,
                        16000  # Default sample rate
                    )

                    if result:
                        cloud_sample_id = result[0]['sample_id']
                        logger.info(f"✅ Voice sample stored to Cloud SQL (ID: {cloud_sample_id})")

                except Exception as e:
                    logger.error(f"Failed to store voice sample to Cloud SQL: {e}")
                    # Continue - don't fail if Cloud SQL fails, SQLite is the backup

            logger.debug(f"🎤 Recorded voice sample {sample_id} for {speaker_name}")

            return sample_id

        except Exception as e:
            logger.error(f"Failed to record voice sample: {e}")
            return -1

    async def get_voice_samples_for_speaker(self, speaker_id: int, limit: int = 10) -> list:
        """
        Retrieve stored voice samples for a speaker.
        Tries Cloud SQL first (persistent), falls back to SQLite if needed.

        Args:
            speaker_id: Database ID of the speaker
            limit: Maximum number of samples to retrieve

        Returns:
            List of sample dictionaries with audio_data and metadata
        """
        samples = []

        # Try Cloud SQL first (it has the persistent data)
        # Check if we have a DatabaseConnectionWrapper which contains the adapter
        if hasattr(self.db, 'adapter') and self.db.adapter:
            print(f"DEBUG: Trying Cloud SQL for speaker {speaker_id}, limit {limit}")
            try:
                cloud_query = """
                    SELECT sample_id, audio_data, transcription
                    FROM voice_samples
                    WHERE speaker_id = $1 AND audio_data IS NOT NULL
                    ORDER BY sample_id DESC
                    LIMIT $2
                """

                # Use the adapter's connection to fetch data
                async with self.db.adapter.connection() as conn:
                    rows = await conn.fetch(cloud_query, speaker_id, limit)
                print(f"DEBUG: Cloud SQL returned {len(rows)} rows")

                for row in rows:
                    samples.append({
                        "sample_id": row["sample_id"],
                        "audio_data": row["audio_data"],  # BYTEA from PostgreSQL
                        "transcription": row["transcription"],
                        # Add defaults for compatibility
                        "sample_rate": 16000,  # Default sample rate
                        "quality_score": 0.8,  # Default quality
                    })

                if samples:
                    logger.info(f"✅ Retrieved {len(samples)} voice samples from Cloud SQL for speaker {speaker_id}")
                    return samples

            except Exception as e:
                logger.warning(f"Failed to retrieve voice samples for speaker {speaker_id}: {e}")
                print(f"Failed to retrieve voice samples for speaker {speaker_id}: {e}")

        # Fallback to SQLite if Cloud SQL fails or no samples found
        try:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT audio_hash, audio_data, mfcc_features, duration_ms, transcription,
                           pitch_mean, pitch_std, energy_mean, recording_timestamp,
                           quality_score
                    FROM voice_samples
                    WHERE speaker_id = ?
                    ORDER BY quality_score DESC, recording_timestamp DESC
                    LIMIT ?
                    """,
                    (speaker_id, limit),
                )

                rows = await cursor.fetchall()

                # Return samples with raw audio data for embedding reconstruction
                for row in rows:
                    samples.append({
                        "audio_hash": row[0],
                        "audio_data": row[1],  # Raw audio bytes for reconstruction
                        "mfcc_features": row[2],
                        "duration_ms": row[3],
                        "transcription": row[4],
                        "pitch_mean": row[5],
                        "pitch_std": row[6],
                        "energy_mean": row[7],
                        "recording_timestamp": row[8],
                        "quality_score": row[9],
                    })

                if samples:
                    logger.info(f"Retrieved {len(samples)} voice samples from SQLite for speaker {speaker_id}")

        except Exception as e:
            logger.error(f"Failed to retrieve voice samples for speaker {speaker_id}: {e}")

        return samples

    async def record_query_retry(
        self,
        original_transcription_id: int,
        retry_transcription_id: int,
        retry_number: int,
        time_between_ms: float,
    ) -> int:
        """
        Record when user has to repeat a query.
        Critical for identifying problematic words/phrases that need acoustic tuning.
        """
        try:
            async with self.db.cursor() as cursor:
                # Update retry count on both transcriptions
                await cursor.execute(
                    """
                    UPDATE voice_transcriptions
                    SET retry_count = retry_count + 1
                    WHERE transcription_id IN (?, ?)
                """,
                    (original_transcription_id, retry_transcription_id),
                )

                # Insert retry record
                await cursor.execute(
                    """
                    INSERT INTO query_retries
                    (original_transcription_id, retry_transcription_id, retry_number,
                     time_between_retries_ms, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                """,
                    (
                        original_transcription_id,
                        retry_transcription_id,
                        retry_number,
                        time_between_ms,
                        datetime.now(),
                    ),
                )

                await self.db.commit()
                retry_id = cursor.lastrowid

                logger.warning(f"🔁 Query retry #{retry_number} detected (gap={time_between_ms}ms)")

                return retry_id

        except Exception as e:
            logger.error(f"Failed to record query retry: {e}")
            return -1

    async def _learn_acoustic_pattern(self, misheard_id: int):
        """
        Extract acoustic learning pattern from misheard query.
        Adapts to Derek's pronunciation patterns and accent.
        """
        try:
            async with self.db.cursor() as cursor:
                # Get misheard details
                await cursor.execute(
                    """
                    SELECT mq.*, vt.confidence_score, vt.audio_duration_ms
                    FROM misheard_queries mq
                    JOIN voice_transcriptions vt ON mq.transcription_id = vt.transcription_id
                    WHERE mq.misheard_id = ?
                """,
                    (misheard_id,),
                )

                row = await cursor.fetchone()
                if not row:
                    return

                # Extract phoneme patterns
                what_heard = row["what_jarvis_heard"]
                what_meant = row["what_user_meant"]

                # Simple pattern: identify word-level differences
                heard_words = what_heard.lower().split()
                meant_words = what_meant.lower().split()

                # Find mismatched words
                for i, (heard, meant) in enumerate(zip(heard_words, meant_words)):
                    if heard != meant:
                        # Store acoustic adaptation
                        speaker_id = await self.get_or_create_speaker_profile("Derek J. Russell")

                        await cursor.execute(
                            """
                            INSERT INTO acoustic_adaptations
                            (speaker_id, phoneme_pattern, expected_pronunciation, actual_pronunciation, context_words)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT (speaker_id, phoneme_pattern) DO UPDATE
                            SET frequency_count = frequency_count + 1,
                                last_observed = ?
                        """,
                            (
                                speaker_id,
                                heard,
                                heard,
                                meant,
                                json.dumps({"position": i, "full_query": what_meant}),
                                datetime.now(),
                            ),
                        )

                # Update misheard query with learned pattern
                pattern_text = f"'{what_heard}' -> '{what_meant}'"
                await cursor.execute(
                    """
                    UPDATE misheard_queries
                    SET learned_pattern = ?
                    WHERE misheard_id = ?
                """,
                    (pattern_text, misheard_id),
                )

                await self.db.commit()

                logger.info(f"🧠 Learned acoustic pattern from misheard query: {pattern_text}")

        except Exception as e:
            logger.error(f"Failed to learn acoustic pattern: {e}")

    def _calculate_phonetic_distance(self, str1: str, str2: str) -> int:
        """Calculate Levenshtein distance for phonetic similarity"""
        if str1 == str2:
            return 0

        if len(str1) == 0:
            return len(str2)
        if len(str2) == 0:
            return len(str1)

        # Create distance matrix
        matrix = [[0] * (len(str2) + 1) for _ in range(len(str1) + 1)]

        # Initialize first row and column
        for i in range(len(str1) + 1):
            matrix[i][0] = i
        for j in range(len(str2) + 1):
            matrix[0][j] = j

        # Calculate distances
        for i in range(1, len(str1) + 1):
            for j in range(1, len(str2) + 1):
                cost = 0 if str1[i - 1] == str2[j - 1] else 1
                matrix[i][j] = min(
                    matrix[i - 1][j] + 1,  # deletion
                    matrix[i][j - 1] + 1,  # insertion
                    matrix[i - 1][j - 1] + cost,  # substitution
                )

        return matrix[len(str1)][len(str2)]

    # ==================== Pattern Learning (ML-Powered) ====================

    async def store_pattern(self, pattern: Dict[str, Any], auto_merge: bool = True) -> str:
        """Store pattern with automatic similarity-based merging"""
        pattern_id = pattern.get("pattern_id", self._generate_id("pattern"))
        pattern_hash = self._hash_pattern(pattern)

        # Check cache first
        cached_pattern = self.pattern_cache.get(pattern_hash)
        if cached_pattern:
            # Update occurrence count
            await self._increment_pattern_occurrence(cached_pattern["pattern_id"])
            return cached_pattern["pattern_id"]

        # Check for similar patterns if auto_merge enabled
        if auto_merge and "embedding" in pattern:
            similar = await self._find_similar_patterns(
                pattern["embedding"], pattern["pattern_type"], similarity_threshold=0.85
            )

            if similar:
                # Merge with most similar pattern
                await self._merge_patterns(similar[0]["pattern_id"], pattern)
                return similar[0]["pattern_id"]

        # Store new pattern
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.upsert(
                    table="patterns",
                    unique_cols=["pattern_id"],
                    data={
                        "pattern_id": pattern_id,
                        "pattern_type": pattern["pattern_type"],
                        "pattern_hash": pattern_hash,
                        "pattern_data": json.dumps(pattern.get("pattern_data", {})),
                        "confidence": pattern.get("confidence", 0.5),
                        "success_rate": pattern.get("success_rate", 0.5),
                        "occurrence_count": 1,
                        "first_seen": datetime.now(),
                        "last_seen": datetime.now(),
                        "metadata": json.dumps(pattern.get("metadata", {})),
                    },
                )

            await self.db.commit()

        # Store embedding
        if self.pattern_collection and "embedding" in pattern:
            await self._store_embedding_async(
                self.pattern_collection,
                pattern_id,
                pattern["embedding"],
                {
                    "pattern_type": pattern["pattern_type"],
                    "confidence": pattern.get("confidence", 0.5),
                    "timestamp": datetime.now().isoformat(),
                },
            )

        # Update cache
        self.pattern_cache.set(pattern_hash, {"pattern_id": pattern_id, **pattern})

        return pattern_id

    async def _find_similar_patterns(
        self, embedding: List[float], pattern_type: str, similarity_threshold: float = 0.8
    ) -> List[Dict]:
        """Find similar patterns using embeddings"""
        if not self.pattern_collection:
            return []

        try:
            results = self.pattern_collection.query(
                query_embeddings=[embedding], n_results=5, where={"pattern_type": pattern_type}
            )

            similar_patterns = []
            async with self.db.cursor() as cursor:
                for i, pattern_id in enumerate(results["ids"][0]):
                    similarity = 1.0 - results["distances"][0][i]
                    if similarity >= similarity_threshold:
                        await cursor.execute(
                            "SELECT * FROM patterns WHERE pattern_id = ?", (pattern_id,)
                        )
                        row = await cursor.fetchone()
                        if row:
                            pattern = dict(row)
                            pattern["similarity"] = similarity
                            similar_patterns.append(pattern)

            return similar_patterns

        except Exception as e:
            logger.error(f"Error finding similar patterns: {e}")
            return []

    async def _merge_patterns(self, target_pattern_id: str, new_pattern: Dict):
        """Merge new pattern into existing pattern"""
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                # Update occurrence count and confidence
                await cursor.execute(
                    """
                    UPDATE patterns SET
                        occurrence_count = occurrence_count + 1,
                        confidence = (confidence + ?) / 2,
                        success_rate = (success_rate + ?) / 2,
                        last_seen = ?,
                        boost_count = boost_count + 1
                    WHERE pattern_id = ?
                """,
                    (
                        new_pattern.get("confidence", 0.5),
                        new_pattern.get("success_rate", 0.5),
                        datetime.now(),
                        target_pattern_id,
                    ),
                )

            await self.db.commit()

    async def _increment_pattern_occurrence(self, pattern_id: str):
        """Increment pattern occurrence count"""
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE patterns SET
                        occurrence_count = occurrence_count + 1,
                        last_seen = ?
                    WHERE pattern_id = ?
                """,
                    (datetime.now(), pattern_id),
                )

            await self.db.commit()

    async def get_pattern_by_type(
        self, pattern_type: str, min_confidence: float = 0.5, limit: int = 10
    ) -> List[Dict]:
        """Get patterns by type with caching"""
        cache_key = f"patterns_{pattern_type}_{min_confidence}_{limit}"

        cached = self.query_cache.get(cache_key)
        if cached:
            return cached

        async with self.db.cursor() as cursor:
            await cursor.execute(
                """
                SELECT * FROM patterns
                WHERE pattern_type = ? AND confidence >= ?
                ORDER BY confidence DESC, occurrence_count DESC
                LIMIT ?
            """,
                (pattern_type, min_confidence, limit),
            )

            rows = await cursor.fetchall()
            patterns = [dict(row) for row in rows]

        self.query_cache.set(cache_key, patterns)
        return patterns

    # ==================== Display Patterns (Enhanced) ====================

    async def learn_display_pattern(self, display_name: str, context: Dict[str, Any]):
        """Learn display connection patterns with temporal analysis"""
        now = datetime.now()
        time_str = now.strftime("%H:%M")
        day_of_week = now.weekday()
        hour_of_day = now.hour
        context_hash = self._hash_context(context)

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                # Check if similar pattern exists
                await cursor.execute(
                    """
                    SELECT * FROM display_patterns
                    WHERE display_name = ?
                    AND hour_of_day = ?
                    AND day_of_week = ?
                """,
                    (display_name, hour_of_day, day_of_week),
                )

                existing = await cursor.fetchone()

                if existing:
                    # Update frequency and consecutive successes
                    await cursor.execute(
                        """
                        UPDATE display_patterns SET
                            frequency = frequency + 1,
                            consecutive_successes = consecutive_successes + 1,
                            last_seen = ?,
                            auto_connect = CASE WHEN frequency >= 3 THEN 1 ELSE auto_connect END,
                            context = ?
                        WHERE pattern_id = ?
                    """,
                        (datetime.now(), json.dumps(context), existing["pattern_id"]),
                    )

                    if existing["frequency"] >= 2:
                        logger.info(
                            f"📊 Display pattern strengthened: {display_name} at {hour_of_day}:00 on day {day_of_week}"
                        )
                else:
                    # Insert new pattern
                    await cursor.execute(
                        """
                        INSERT INTO display_patterns
                        (display_name, context, context_hash, connection_time, day_of_week,
                         hour_of_day, frequency, auto_connect, last_seen, consecutive_successes, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                        (
                            display_name,
                            json.dumps(context),
                            context_hash,
                            time_str,
                            day_of_week,
                            hour_of_day,
                            1,
                            False,
                            datetime.now(),
                            1,
                            json.dumps({}),
                        ),
                    )

            await self.db.commit()

    async def should_auto_connect_display(
        self, current_context: Optional[Dict] = None
    ) -> Optional[Tuple[str, float]]:
        """Predict display connection with context awareness"""
        now = datetime.now()
        hour_of_day = now.hour
        day_of_week = now.weekday()

        async with self.db.cursor() as cursor:
            # Look for matching patterns with temporal proximity
            await cursor.execute(
                """
                SELECT display_name, frequency, consecutive_successes, auto_connect
                FROM display_patterns
                WHERE hour_of_day = ?
                AND day_of_week = ?
                AND frequency >= 2
                ORDER BY frequency DESC, consecutive_successes DESC
                LIMIT 1
            """,
                (hour_of_day, day_of_week),
            )

            row = await cursor.fetchone()

            if row:
                # Calculate dynamic confidence
                base_confidence = min(row["frequency"] / 10.0, 0.85)
                consecutive_bonus = min(row["consecutive_successes"] * 0.05, 0.10)
                confidence = min(base_confidence + consecutive_bonus, 0.95)

                return row["display_name"], confidence

        return None

    # ==================== User Preferences (Enhanced) ====================

    async def learn_preference(
        self,
        category: str,
        key: str,
        value: Any,
        confidence: float = 0.5,
        learned_from: str = "implicit",
    ):
        """Learn user preference with confidence averaging"""
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO user_preferences
                    (preference_id, category, key, value, confidence,
                     learned_from, created_at, updated_at, update_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, key) DO UPDATE SET
                        value = ?,
                        confidence = (confidence * update_count + ?) / (update_count + 1),
                        update_count = update_count + 1,
                        updated_at = ?
                """,
                    (
                        f"{category}_{key}",
                        category,
                        key,
                        str(value),
                        confidence,
                        learned_from,
                        datetime.now(),
                        datetime.now(),
                        1,
                        str(value),
                        confidence,
                        datetime.now(),
                    ),
                )

            await self.db.commit()

    async def get_preference(
        self, category: str, key: str, min_confidence: float = 0.5
    ) -> Optional[Dict]:
        """Get learned preference with confidence threshold"""
        cache_key = f"pref_{category}_{key}"

        cached = self.query_cache.get(cache_key)
        if cached:
            return cached

        async with self.db.cursor() as cursor:
            await cursor.execute(
                """
                SELECT * FROM user_preferences
                WHERE category = ? AND key = ? AND confidence >= ?
            """,
                (category, key, min_confidence),
            )

            row = await cursor.fetchone()
            result = dict(row) if row else None

            if result:
                self.query_cache.set(cache_key, result)

            return result

    # ==================== Analytics & Metrics ====================

    async def get_learning_metrics(self) -> Dict[str, Any]:
        """Get comprehensive real-time learning metrics"""
        metrics = {}

        # Check if using Cloud SQL (PostgreSQL) or SQLite
        is_cloud = isinstance(self.db, DatabaseConnectionWrapper) and self.db.is_cloud

        async with self.db.cursor() as cursor:
            # Goal metrics
            if is_cloud:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_goals,
                        AVG(confidence) as avg_confidence,
                        SUM(CASE WHEN is_completed THEN 1 ELSE 0 END) as completed_goals,
                        AVG(actual_duration) as avg_duration
                    FROM goals
                    WHERE created_at >= NOW() - INTERVAL '30 days'
                """
                )
            else:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_goals,
                        AVG(confidence) as avg_confidence,
                        SUM(is_completed) as completed_goals,
                        AVG(actual_duration) as avg_duration
                    FROM goals
                    WHERE created_at >= datetime('now', '-30 days')
                """
                )
            metrics["goals"] = dict(await cursor.fetchone())

            # Action metrics
            if is_cloud:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_actions,
                        AVG(confidence) as avg_confidence,
                        SUM(CASE WHEN success THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as success_rate,
                        AVG(execution_time) as avg_execution_time,
                        AVG(retry_count) as avg_retries
                    FROM actions
                    WHERE timestamp >= NOW() - INTERVAL '30 days'
                """
                )
            else:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_actions,
                        AVG(confidence) as avg_confidence,
                        SUM(success) * 100.0 / COUNT(*) as success_rate,
                        AVG(execution_time) as avg_execution_time,
                        AVG(retry_count) as avg_retries
                    FROM actions
                    WHERE timestamp >= datetime('now', '-30 days')
                """
                )
            metrics["actions"] = dict(await cursor.fetchone())

            # Pattern metrics
            await cursor.execute(
                """
                SELECT
                    COUNT(*) as total_patterns,
                    AVG(confidence) as avg_confidence,
                    AVG(success_rate) as avg_success_rate,
                    SUM(occurrence_count) as total_occurrences,
                    AVG(occurrence_count) as avg_occurrences_per_pattern
                FROM patterns
            """
            )
            metrics["patterns"] = dict(await cursor.fetchone())

            # Display pattern metrics
            if is_cloud:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_display_patterns,
                        SUM(CASE WHEN auto_connect THEN 1 ELSE 0 END) as auto_connect_enabled,
                        MAX(frequency) as max_frequency,
                        AVG(consecutive_successes) as avg_consecutive_successes
                    FROM display_patterns
                """
                )
            else:
                await cursor.execute(
                    """
                    SELECT
                        COUNT(*) as total_display_patterns,
                        SUM(auto_connect) as auto_connect_enabled,
                        MAX(frequency) as max_frequency,
                        AVG(consecutive_successes) as avg_consecutive_successes
                    FROM display_patterns
                """
                )
            metrics["display_patterns"] = dict(await cursor.fetchone())

            # Goal-action mapping insights
            await cursor.execute(
                """
                SELECT
                    goal_type,
                    action_type,
                    confidence,
                    success_count + failure_count as total_attempts,
                    avg_execution_time
                FROM goal_action_mappings
                WHERE confidence > 0.7
                ORDER BY confidence DESC
                LIMIT 10
            """
            )
            metrics["top_mappings"] = [dict(row) for row in await cursor.fetchall()]

        # Add cache metrics
        metrics["cache_performance"] = {
            "pattern_cache_hit_rate": self.pattern_cache.hit_rate(),
            "goal_cache_hit_rate": self.goal_cache.hit_rate(),
            "query_cache_hit_rate": self.query_cache.hit_rate(),
        }

        # Update internal metrics
        self.metrics.total_patterns = metrics["patterns"]["total_patterns"]
        self.metrics.avg_confidence = metrics["patterns"]["avg_confidence"] or 0.0
        self.metrics.cache_hit_rate = metrics["cache_performance"]["pattern_cache_hit_rate"]
        self.metrics.last_updated = datetime.now()

        return metrics

    async def analyze_patterns(self) -> List[Dict]:
        """Advanced pattern analysis with ML insights"""
        patterns = []

        async with self.db.cursor() as cursor:
            await cursor.execute(
                """
                SELECT *
                FROM patterns
                WHERE occurrence_count >= 3
                ORDER BY success_rate DESC, occurrence_count DESC
                LIMIT 50
            """
            )

            rows = await cursor.fetchall()

            for row in rows:
                pattern = dict(row)

                # Calculate pattern strength score
                strength = (
                    pattern["confidence"] * 0.4
                    + pattern["success_rate"] * 0.4
                    + min(pattern["occurrence_count"] / 100.0, 1.0) * 0.2
                )
                pattern["strength_score"] = strength

                # Time since last seen
                last_seen = datetime.fromisoformat(pattern["last_seen"])
                days_since = (datetime.now() - last_seen).days
                pattern["days_since_last_seen"] = days_since

                # Decay recommendation
                if days_since > 30 and pattern["occurrence_count"] < 5:
                    pattern["should_decay"] = True
                else:
                    pattern["should_decay"] = False

                patterns.append(pattern)

        return patterns

    async def boost_pattern_confidence(
        self,
        pattern_id: str,
        boost: float = 0.05,
        strategy: ConfidenceBoostStrategy = ConfidenceBoostStrategy.ADAPTIVE,
    ):
        """Boost pattern confidence using configurable strategy"""
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    "SELECT confidence, boost_count FROM patterns WHERE pattern_id = ?",
                    (pattern_id,),
                )
                row = await cursor.fetchone()

                if row:
                    current_confidence = row["confidence"]
                    boost_count = row["boost_count"]

                    # Apply strategy
                    if strategy == ConfidenceBoostStrategy.LINEAR:
                        new_confidence = min(current_confidence + boost, 1.0)
                    elif strategy == ConfidenceBoostStrategy.EXPONENTIAL:
                        new_confidence = min(current_confidence * (1.0 + boost), 1.0)
                    elif strategy == ConfidenceBoostStrategy.LOGARITHMIC:
                        new_confidence = min(current_confidence + boost / (1 + boost_count), 1.0)
                    else:  # ADAPTIVE
                        # Reduces boost as confidence increases
                        adaptive_boost = boost * (1.0 - current_confidence)
                        new_confidence = min(current_confidence + adaptive_boost, 1.0)

                    await cursor.execute(
                        """
                        UPDATE patterns SET
                            confidence = ?,
                            boost_count = boost_count + 1
                        WHERE pattern_id = ?
                    """,
                        (new_confidence, pattern_id),
                    )

                await self.db.commit()

    # ==================== Maintenance & Optimization ====================

    async def cleanup_old_patterns(self, days: int = 30):
        """Clean up old unused patterns with decay"""
        cutoff_date = datetime.now() - timedelta(days=days)

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                # Decay old patterns instead of deleting
                await cursor.execute(
                    """
                    UPDATE patterns SET
                        confidence = confidence * 0.9,
                        decay_applied = 1
                    WHERE last_seen < ? AND occurrence_count < 3
                """,
                    (cutoff_date,),
                )

                # Delete very old patterns with low occurrence
                await cursor.execute(
                    """
                    DELETE FROM patterns
                    WHERE last_seen < ? AND occurrence_count = 1 AND confidence < 0.3
                """,
                    (cutoff_date,),
                )

                deleted_count = cursor.rowcount

            await self.db.commit()

        logger.info(f"🧹 Cleaned up {deleted_count} old patterns")

    async def optimize(self):
        """Optimize database performance"""
        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.execute("ANALYZE")
                await cursor.execute("VACUUM")

            await self.db.commit()

        logger.info("✨ Database optimized")

    async def _auto_flush_batches(self):
        """Auto-flush batch queues periodically with shutdown support"""
        try:
            while not self._shutdown_event.is_set():
                try:
                    # Wait with timeout to allow checking shutdown event
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=5.0  # Flush every 5 seconds
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    # Timeout - time to flush
                    pass

                if self.pending_goals:
                    await self._flush_goal_batch()

                if self.pending_actions:
                    await self._flush_action_batch()

                if self.pending_patterns:
                    await self._flush_pattern_batch()

        except asyncio.CancelledError:
            logger.debug("Auto-flush task cancelled")
        finally:
            logger.debug("Auto-flush task exiting")

    async def _flush_goal_batch(self):
        """Flush pending goals"""
        if not self.pending_goals:
            return

        goals_to_insert = list(self.pending_goals)
        self.pending_goals.clear()

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT OR REPLACE INTO goals
                    (goal_id, goal_type, goal_level, description, confidence,
                     progress, is_completed, created_at, evidence, metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        (
                            goal_id,
                            goal["goal_type"],
                            goal.get("goal_level", "UNKNOWN"),
                            goal.get("description", ""),
                            goal.get("confidence", 0.0),
                            goal.get("progress", 0.0),
                            goal.get("is_completed", False),
                            goal.get("created_at", datetime.now()),
                            json.dumps(goal.get("evidence", [])),
                            json.dumps(goal.get("metadata", {})),
                        )
                        for goal_id, goal in goals_to_insert
                    ],
                )

            await self.db.commit()

    async def _flush_action_batch(self):
        """Flush pending actions"""
        if not self.pending_actions:
            return

        actions_to_insert = list(self.pending_actions)
        self.pending_actions.clear()

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO actions
                    (action_id, action_type, target, goal_id, confidence,
                     success, execution_time, timestamp, params, result)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    [
                        (
                            action_id,
                            action["action_type"],
                            action.get("target", ""),
                            action.get("goal_id"),
                            action.get("confidence", 0.0),
                            action.get("success", False),
                            action.get("execution_time", 0.0),
                            action.get("timestamp", datetime.now()),
                            json.dumps(action.get("params", {})),
                            json.dumps(action.get("result", {})),
                        )
                        for action_id, action in actions_to_insert
                    ],
                )

            await self.db.commit()

    async def _flush_pattern_batch(self):
        """Flush pending patterns with intelligent deduplication and merging"""
        if not self.pending_patterns:
            return

        patterns_to_process = list(self.pending_patterns)
        self.pending_patterns.clear()

        # Group patterns by hash for intelligent merging
        pattern_groups: Dict[str, List[Tuple[str, Dict]]] = defaultdict(list)

        for pattern_id, pattern in patterns_to_process:
            pattern_hash = self._hash_pattern(pattern)
            pattern_groups[pattern_hash].append((pattern_id, pattern))

        async with self._db_lock:
            async with self.db.cursor() as cursor:
                # Process each group
                for pattern_hash, group in pattern_groups.items():
                    if len(group) == 1:
                        # Single pattern - insert directly
                        pattern_id, pattern = group[0]

                        await cursor.execute(
                            """
                            INSERT OR REPLACE INTO patterns
                            (pattern_id, pattern_type, pattern_hash, pattern_data, confidence,
                             success_rate, occurrence_count, first_seen, last_seen, metadata)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                pattern_id,
                                pattern["pattern_type"],
                                pattern_hash,
                                json.dumps(pattern.get("pattern_data", {})),
                                pattern.get("confidence", 0.5),
                                pattern.get("success_rate", 0.5),
                                1,
                                datetime.now(),
                                datetime.now(),
                                json.dumps(pattern.get("metadata", {})),
                            ),
                        )

                        # Store embedding if available
                        if self.pattern_collection and "embedding" in pattern:
                            await self._store_embedding_async(
                                self.pattern_collection,
                                pattern_id,
                                pattern["embedding"],
                                {
                                    "pattern_type": pattern["pattern_type"],
                                    "confidence": pattern.get("confidence", 0.5),
                                    "timestamp": datetime.now().isoformat(),
                                },
                            )
                    else:
                        # Multiple patterns with same hash - merge intelligently
                        # Use the first pattern as base
                        base_pattern_id, base_pattern = group[0]

                        # Calculate merged confidence (weighted average)
                        total_confidence = sum(p.get("confidence", 0.5) for _, p in group)
                        avg_confidence = total_confidence / len(group)

                        # Calculate merged success rate
                        total_success_rate = sum(p.get("success_rate", 0.5) for _, p in group)
                        avg_success_rate = total_success_rate / len(group)

                        # Merge metadata intelligently
                        merged_metadata = {}
                        for _, pattern in group:
                            pattern_meta = pattern.get("metadata", {})
                            for key, value in pattern_meta.items():
                                if key not in merged_metadata:
                                    merged_metadata[key] = []
                                if value not in merged_metadata[key]:
                                    merged_metadata[key].append(value)

                        # Flatten single-value lists
                        for key in merged_metadata:
                            if len(merged_metadata[key]) == 1:
                                merged_metadata[key] = merged_metadata[key][0]

                        # Insert merged pattern
                        await cursor.execute(
                            """
                            INSERT OR REPLACE INTO patterns
                            (pattern_id, pattern_type, pattern_hash, pattern_data, confidence,
                             success_rate, occurrence_count, first_seen, last_seen, boost_count, metadata)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                            (
                                base_pattern_id,
                                base_pattern["pattern_type"],
                                pattern_hash,
                                json.dumps(base_pattern.get("pattern_data", {})),
                                avg_confidence,
                                avg_success_rate,
                                len(group),  # Number of merged patterns
                                datetime.now(),
                                datetime.now(),
                                len(group) - 1,  # Boost count from merging
                                json.dumps(merged_metadata),
                            ),
                        )

                        # Store embedding for merged pattern
                        if self.pattern_collection and "embedding" in base_pattern:
                            # Average all embeddings if multiple available
                            embeddings_to_merge = [
                                p.get("embedding") for _, p in group if "embedding" in p
                            ]

                            if embeddings_to_merge and NUMPY_AVAILABLE:
                                # Average embeddings
                                import numpy as np

                                avg_embedding = np.mean(embeddings_to_merge, axis=0).tolist()

                                await self._store_embedding_async(
                                    self.pattern_collection,
                                    base_pattern_id,
                                    avg_embedding,
                                    {
                                        "pattern_type": base_pattern["pattern_type"],
                                        "confidence": avg_confidence,
                                        "merged_count": len(group),
                                        "timestamp": datetime.now().isoformat(),
                                    },
                                )
                            elif embeddings_to_merge:
                                # Use first embedding if numpy not available
                                await self._store_embedding_async(
                                    self.pattern_collection,
                                    base_pattern_id,
                                    embeddings_to_merge[0],
                                    {
                                        "pattern_type": base_pattern["pattern_type"],
                                        "confidence": avg_confidence,
                                        "merged_count": len(group),
                                        "timestamp": datetime.now().isoformat(),
                                    },
                                )

                        # Update cache with merged pattern
                        self.pattern_cache.set(
                            pattern_hash,
                            {
                                "pattern_id": base_pattern_id,
                                "confidence": avg_confidence,
                                "success_rate": avg_success_rate,
                                "occurrence_count": len(group),
                            },
                        )

                        logger.debug(
                            f"🔀 Merged {len(group)} similar patterns into {base_pattern_id}"
                        )

            await self.db.commit()

        logger.debug(
            f"✅ Flushed {len(patterns_to_process)} patterns ({len(pattern_groups)} unique)"
        )

    async def _auto_optimize_task(self):
        """Auto-optimize database periodically with shutdown support"""
        if not self.auto_optimize:
            logger.debug("Auto-optimize disabled")
            return

        try:
            while not self._shutdown_event.is_set():
                try:
                    # Wait with timeout to allow checking shutdown event
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=3600.0  # Every hour
                    )
                    break  # Shutdown requested
                except asyncio.TimeoutError:
                    # Timeout - time to optimize
                    pass

                await self.optimize()

        except asyncio.CancelledError:
            logger.debug("Auto-optimize task cancelled")
        finally:
            logger.debug("Auto-optimize task exiting")

    async def _load_metrics(self):
        """Load initial metrics"""
        try:
            metrics = await self.get_learning_metrics()
            logger.info(f"📊 Loaded metrics: {metrics['patterns']['total_patterns']} patterns")
        except Exception as e:
            logger.error(f"Failed to load metrics: {e}")

    async def _store_embedding_async(
        self, collection, doc_id: str, embedding: List[float], metadata: Dict
    ):
        """Store embedding asynchronously"""
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: collection.upsert(
                    ids=[doc_id], embeddings=[embedding], metadatas=[metadata]
                ),
            )
        except Exception as e:
            logger.error(f"Failed to store embedding: {e}")

    # ==================== Utility Methods ====================

    @staticmethod
    def _generate_id(prefix: str) -> str:
        """Generate unique ID"""
        timestamp = datetime.now().timestamp()
        return f"{prefix}_{timestamp}_{hashlib.md5(str(timestamp).encode(), usedforsecurity=False).hexdigest()[:8]}"

    @staticmethod
    def _hash_context(context: Dict) -> str:
        """Generate hash for context deduplication"""
        context_str = json.dumps(context, sort_keys=True)
        return hashlib.sha256(context_str.encode()).hexdigest()[:16]

    @staticmethod
    def _hash_pattern(pattern: Dict) -> str:
        """Generate hash for pattern deduplication"""
        pattern_str = json.dumps(
            {"type": pattern.get("pattern_type"), "data": pattern.get("pattern_data")},
            sort_keys=True,
        )
        return hashlib.sha256(pattern_str.encode()).hexdigest()[:16]

    # ========================================================================
    # PHASE 3: BEHAVIORAL LEARNING METHODS
    # ========================================================================

    async def store_workspace_usage(
        self,
        space_id: int,
        app_name: str,
        window_title: Optional[str] = None,
        window_position: Optional[Dict] = None,
        focus_duration: Optional[float] = None,
        is_fullscreen: bool = False,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Store workspace usage event

        Args:
            space_id: Space/Desktop ID
            app_name: Application name
            window_title: Window title
            window_position: Window frame {x, y, w, h}
            focus_duration: Focus duration in seconds
            is_fullscreen: Whether window is fullscreen
            metadata: Additional metadata

        Returns:
            usage_id
        """
        now = datetime.now()

        try:
            async with self.db.execute(
                """
                INSERT INTO workspace_usage (
                    space_id, space_label, app_name, window_title,
                    window_position, focus_duration_seconds, timestamp,
                    day_of_week, hour_of_day, is_fullscreen, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    space_id,
                    None,  # space_label can be updated separately
                    app_name,
                    window_title,
                    json.dumps(window_position) if window_position else None,
                    focus_duration,
                    now.isoformat(),
                    now.weekday(),
                    now.hour,
                    is_fullscreen,
                    json.dumps(metadata) if metadata else None,
                ),
            ) as cursor:
                await self.db.commit()
                return cursor.lastrowid

        except Exception as e:
            logger.error(f"Error storing workspace usage: {e}", exc_info=True)
            raise

    async def update_app_usage_pattern(
        self,
        app_name: str,
        space_id: int,
        session_duration: float,
        timestamp: Optional[datetime] = None,
    ):
        """
        Update app usage pattern (incremental learning)

        Args:
            app_name: Application name
            space_id: Space ID
            session_duration: Session duration in seconds
            timestamp: Timestamp (defaults to now)
        """
        if timestamp is None:
            timestamp = datetime.now()

        hour = timestamp.hour
        day = timestamp.weekday()

        try:
            # Check if pattern exists
            async with self.db.execute(
                """
                SELECT pattern_id, usage_frequency, avg_session_duration,
                       total_usage_time, confidence
                FROM app_usage_patterns
                WHERE app_name = ? AND space_id = ?
                  AND typical_time_of_day = ? AND typical_day_of_week = ?
            """,
                (app_name, space_id, hour, day),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update existing pattern
                pattern_id, freq, avg_dur, total_time, conf = row
                new_freq = freq + 1
                new_total = total_time + session_duration
                new_avg = new_total / new_freq
                new_conf = min(0.99, conf + 0.02)  # Incremental confidence boost

                await self.db.execute(
                    """
                    UPDATE app_usage_patterns
                    SET usage_frequency = ?,
                        avg_session_duration = ?,
                        total_usage_time = ?,
                        last_used = ?,
                        confidence = ?
                    WHERE pattern_id = ?
                """,
                    (new_freq, new_avg, new_total, timestamp.isoformat(), new_conf, pattern_id),
                )

            else:
                # Create new pattern
                await self.db.execute(
                    """
                    INSERT INTO app_usage_patterns (
                        app_name, space_id, usage_frequency,
                        avg_session_duration, total_usage_time,
                        typical_time_of_day, typical_day_of_week,
                        last_used, confidence, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        app_name,
                        space_id,
                        1,
                        session_duration,
                        session_duration,
                        hour,
                        day,
                        timestamp.isoformat(),
                        0.3,
                        json.dumps({}),
                    ),
                )

            await self.db.commit()

        except Exception as e:
            logger.error(f"Error updating app usage pattern: {e}", exc_info=True)

    async def store_user_workflow(
        self,
        workflow_name: str,
        action_sequence: List[Dict],
        space_sequence: Optional[List[int]] = None,
        app_sequence: Optional[List[str]] = None,
        duration: Optional[float] = None,
        success: bool = True,
        confidence: float = 0.5,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Store or update user workflow pattern

        Args:
            workflow_name: Name/ID of workflow
            action_sequence: Sequence of actions
            space_sequence: Sequence of Spaces used
            app_sequence: Sequence of apps used
            duration: Duration in seconds
            success: Whether workflow succeeded
            confidence: Confidence score
            metadata: Additional metadata

        Returns:
            workflow_id
        """
        now = datetime.now()
        hour = now.hour

        try:
            # Check if workflow exists
            async with self.db.execute(
                """
                SELECT workflow_id, frequency, success_rate, time_of_day_pattern
                FROM user_workflows
                WHERE workflow_name = ?
            """,
                (workflow_name,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update existing workflow
                wf_id, freq, success_rate, time_pattern = row
                new_freq = freq + 1

                # Update success rate
                new_success_rate = ((success_rate * freq) + (1.0 if success else 0.0)) / new_freq

                # Update time pattern
                time_patterns = json.loads(time_pattern) if time_pattern else []
                time_patterns.append(hour)

                await self.db.execute(
                    """
                    UPDATE user_workflows
                    SET frequency = ?,
                        success_rate = ?,
                        last_seen = ?,
                        time_of_day_pattern = ?,
                        confidence = ?
                    WHERE workflow_id = ?
                """,
                    (
                        new_freq,
                        new_success_rate,
                        now.isoformat(),
                        json.dumps(time_patterns[-20:]),  # Keep last 20
                        min(0.99, confidence + 0.05),
                        wf_id,
                    ),
                )

                return wf_id

            else:
                # Create new workflow
                async with self.db.execute(
                    """
                    INSERT INTO user_workflows (
                        workflow_name, action_sequence, space_sequence,
                        app_sequence, frequency, avg_duration, success_rate,
                        first_seen, last_seen, time_of_day_pattern,
                        confidence, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        workflow_name,
                        json.dumps(action_sequence),
                        json.dumps(space_sequence) if space_sequence else None,
                        json.dumps(app_sequence) if app_sequence else None,
                        1,
                        duration,
                        1.0 if success else 0.0,
                        now.isoformat(),
                        now.isoformat(),
                        json.dumps([hour]),
                        confidence,
                        json.dumps(metadata) if metadata else None,
                    ),
                ) as cursor:
                    await self.db.commit()
                    return cursor.lastrowid

        except Exception as e:
            logger.error(f"Error storing workflow: {e}", exc_info=True)
            raise

    async def store_space_transition(
        self,
        from_space: int,
        to_space: int,
        trigger_app: Optional[str] = None,
        trigger_action: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ):
        """
        Store Space transition event

        Args:
            from_space: Source Space ID
            to_space: Target Space ID
            trigger_app: App that triggered transition
            trigger_action: Action that triggered transition
            metadata: Additional metadata
        """
        now = datetime.now()

        try:
            # Check for existing transition pattern
            async with self.db.execute(
                """
                SELECT transition_id, frequency, avg_time_between_seconds
                FROM space_transitions
                WHERE from_space_id = ? AND to_space_id = ?
                  AND hour_of_day = ? AND day_of_week = ?
            """,
                (from_space, to_space, now.hour, now.weekday()),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update existing
                trans_id, freq, avg_time = row
                await self.db.execute(
                    """
                    UPDATE space_transitions
                    SET frequency = ?,
                        timestamp = ?
                    WHERE transition_id = ?
                """,
                    (freq + 1, now.isoformat(), trans_id),
                )
            else:
                # Create new
                await self.db.execute(
                    """
                    INSERT INTO space_transitions (
                        from_space_id, to_space_id, trigger_app, trigger_action,
                        frequency, timestamp, hour_of_day, day_of_week, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        from_space,
                        to_space,
                        trigger_app,
                        trigger_action,
                        1,
                        now.isoformat(),
                        now.hour,
                        now.weekday(),
                        json.dumps(metadata) if metadata else None,
                    ),
                )

            await self.db.commit()

        except Exception as e:
            logger.error(f"Error storing space transition: {e}", exc_info=True)

    async def store_behavioral_pattern(
        self,
        behavior_type: str,
        description: str,
        pattern_data: Dict,
        confidence: float = 0.5,
        temporal_pattern: Optional[Dict] = None,
        contextual_triggers: Optional[List[str]] = None,
        prediction_accuracy: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Store high-level behavioral pattern

        Args:
            behavior_type: Type of behavior
            description: Human-readable description
            pattern_data: Pattern data structure
            confidence: Confidence score
            temporal_pattern: Time-based pattern info
            contextual_triggers: List of triggers
            prediction_accuracy: How accurate predictions are
            metadata: Additional metadata

        Returns:
            behavior_id
        """
        now = datetime.now()

        try:
            # Check if behavior exists
            async with self.db.execute(
                """
                SELECT behavior_id, frequency, confidence
                FROM behavioral_patterns
                WHERE behavior_type = ? AND behavior_description = ?
            """,
                (behavior_type, description),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update existing
                beh_id, freq, old_conf = row
                new_conf = min(0.99, old_conf + 0.03)

                await self.db.execute(
                    """
                    UPDATE behavioral_patterns
                    SET frequency = ?,
                        confidence = ?,
                        last_observed = ?,
                        prediction_accuracy = ?
                    WHERE behavior_id = ?
                """,
                    (freq + 1, new_conf, now.isoformat(), prediction_accuracy, beh_id),
                )

                return beh_id
            else:
                # Create new
                async with self.db.execute(
                    """
                    INSERT INTO behavioral_patterns (
                        behavior_type, behavior_description, pattern_data,
                        frequency, confidence, temporal_pattern,
                        contextual_triggers, first_observed, last_observed,
                        prediction_accuracy, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        behavior_type,
                        description,
                        json.dumps(pattern_data),
                        1,
                        confidence,
                        json.dumps(temporal_pattern) if temporal_pattern else None,
                        json.dumps(contextual_triggers) if contextual_triggers else None,
                        now.isoformat(),
                        now.isoformat(),
                        prediction_accuracy,
                        json.dumps(metadata) if metadata else None,
                    ),
                ) as cursor:
                    await self.db.commit()
                    return cursor.lastrowid

        except Exception as e:
            logger.error(f"Error storing behavioral pattern: {e}", exc_info=True)
            raise

    async def store_temporal_pattern(
        self,
        pattern_type: str,
        action_type: str,
        target: str,
        time_of_day: Optional[int] = None,
        day_of_week: Optional[int] = None,
        day_of_month: Optional[int] = None,
        month_of_year: Optional[int] = None,
        frequency: int = 1,
        confidence: float = 0.5,
        metadata: Optional[Dict] = None,
    ):
        """
        Store temporal (time-based) behavioral pattern

        Args:
            pattern_type: Type of temporal pattern
            action_type: Type of action
            target: Target of action
            time_of_day: Hour (0-23)
            day_of_week: Day (0-6)
            day_of_month: Day (1-31)
            month_of_year: Month (1-12)
            frequency: Occurrence frequency
            confidence: Confidence score
            metadata: Additional metadata
        """
        now = datetime.now()
        is_leap = calendar.isleap(now.year)

        try:
            # Check if pattern exists
            async with self.db.execute(
                """
                SELECT temporal_id, frequency, confidence
                FROM temporal_patterns
                WHERE pattern_type = ? AND action_type = ? AND target = ?
                  AND time_of_day = ? AND day_of_week = ?
            """,
                (pattern_type, action_type, target, time_of_day, day_of_week),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                # Update existing
                temp_id, freq, old_conf = row
                new_freq = freq + frequency
                new_conf = min(0.99, old_conf + 0.02)

                await self.db.execute(
                    """
                    UPDATE temporal_patterns
                    SET frequency = ?,
                        confidence = ?,
                        last_occurrence = ?
                    WHERE temporal_id = ?
                """,
                    (new_freq, new_conf, now.isoformat(), temp_id),
                )
            else:
                # Create new
                await self.db.execute(
                    """
                    INSERT INTO temporal_patterns (
                        pattern_type, time_of_day, day_of_week,
                        day_of_month, month_of_year, is_leap_year,
                        action_type, target, frequency, confidence,
                        last_occurrence, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        pattern_type,
                        time_of_day,
                        day_of_week,
                        day_of_month,
                        month_of_year,
                        is_leap,
                        action_type,
                        target,
                        frequency,
                        confidence,
                        now.isoformat(),
                        json.dumps(metadata) if metadata else None,
                    ),
                )

            await self.db.commit()

        except Exception as e:
            logger.error(f"Error storing temporal pattern: {e}", exc_info=True)

    async def store_proactive_suggestion(
        self,
        suggestion_type: str,
        suggestion_text: str,
        trigger_pattern_id: Optional[str] = None,
        confidence: float = 0.7,
        metadata: Optional[Dict] = None,
    ) -> int:
        """
        Store proactive suggestion from AI

        Args:
            suggestion_type: Type of suggestion
            suggestion_text: The suggestion itself
            trigger_pattern_id: Pattern that triggered this
            confidence: Confidence score
            metadata: Additional metadata

        Returns:
            suggestion_id
        """
        now = datetime.now()

        try:
            async with self.db.execute(
                """
                INSERT INTO proactive_suggestions (
                    suggestion_type, suggestion_text, trigger_pattern_id,
                    confidence, times_suggested, times_accepted,
                    times_rejected, acceptance_rate, created_at,
                    last_suggested, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    suggestion_type,
                    suggestion_text,
                    trigger_pattern_id,
                    confidence,
                    0,
                    0,
                    0,
                    0.0,
                    now.isoformat(),
                    None,
                    json.dumps(metadata) if metadata else None,
                ),
            ) as cursor:
                await self.db.commit()
                return cursor.lastrowid

        except Exception as e:
            logger.error(f"Error storing suggestion: {e}", exc_info=True)
            raise

    async def update_suggestion_feedback(self, suggestion_id: int, accepted: bool):
        """
        Update suggestion with user feedback

        Args:
            suggestion_id: Suggestion ID
            accepted: Whether user accepted/rejected
        """
        now = datetime.now()

        try:
            # Get current stats
            async with self.db.execute(
                """
                SELECT times_suggested, times_accepted, times_rejected
                FROM proactive_suggestions
                WHERE suggestion_id = ?
            """,
                (suggestion_id,),
            ) as cursor:
                row = await cursor.fetchone()

            if row:
                suggested, accepted_count, rejected_count = row

                if accepted:
                    accepted_count += 1
                else:
                    rejected_count += 1

                total = accepted_count + rejected_count
                acceptance_rate = accepted_count / total if total > 0 else 0.0

                await self.db.execute(
                    """
                    UPDATE proactive_suggestions
                    SET times_accepted = ?,
                        times_rejected = ?,
                        acceptance_rate = ?,
                        last_suggested = ?
                    WHERE suggestion_id = ?
                """,
                    (
                        accepted_count,
                        rejected_count,
                        acceptance_rate,
                        now.isoformat(),
                        suggestion_id,
                    ),
                )

                await self.db.commit()

        except Exception as e:
            logger.error(f"Error updating suggestion feedback: {e}", exc_info=True)

    # Behavioral Query Methods

    async def get_app_usage_patterns(
        self,
        app_name: Optional[str] = None,
        space_id: Optional[int] = None,
        time_of_day: Optional[int] = None,
        min_confidence: float = 0.6,
    ) -> List[Dict]:
        """
        Query app usage patterns

        Args:
            app_name: Filter by app name
            space_id: Filter by Space ID
            time_of_day: Filter by hour
            min_confidence: Minimum confidence threshold

        Returns:
            List of app usage patterns
        """
        query = """
            SELECT * FROM app_usage_patterns
            WHERE confidence >= ?
        """
        params = [min_confidence]

        if app_name:
            query += " AND app_name = ?"
            params.append(app_name)
        if space_id is not None:
            query += " AND space_id = ?"
            params.append(space_id)
        if time_of_day is not None:
            query += " AND typical_time_of_day = ?"
            params.append(time_of_day)

        query += " ORDER BY confidence DESC, usage_frequency DESC LIMIT 50"

        try:
            async with self.db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error querying app patterns: {e}", exc_info=True)
            return []

    async def get_workflows_by_context(
        self,
        app_sequence: Optional[List[str]] = None,
        space_sequence: Optional[List[int]] = None,
        time_of_day: Optional[int] = None,
        min_confidence: float = 0.6,
    ) -> List[Dict]:
        """
        Query workflows by context

        Args:
            app_sequence: Partial app sequence to match
            space_sequence: Partial space sequence to match
            time_of_day: Hour of day
            min_confidence: Minimum confidence

        Returns:
            List of matching workflows
        """
        query = """
            SELECT * FROM user_workflows
            WHERE confidence >= ?
        """
        params = [min_confidence]

        if time_of_day is not None:
            query += " AND json_extract(time_of_day_pattern, '$') LIKE ?"
            params.append(f"%{time_of_day}%")

        query += " ORDER BY confidence DESC, frequency DESC LIMIT 30"

        try:
            async with self.db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
                workflows = [dict(row) for row in rows]

                # Filter by sequences if provided
                if app_sequence:
                    workflows = [
                        w
                        for w in workflows
                        if self._sequence_matches(
                            json.loads(w["app_sequence"]) if w["app_sequence"] else [], app_sequence
                        )
                    ]

                return workflows
        except Exception as e:
            logger.error(f"Error querying workflows: {e}", exc_info=True)
            return []

    async def predict_next_action(self, current_context: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Predict next likely actions based on current context

        Args:
            current_context: Current context including:
                - current_app: Current app name
                - current_space: Current Space ID
                - time_of_day: Current hour
                - recent_actions: List of recent actions

        Returns:
            List of predicted actions with confidence scores
        """
        predictions = []

        try:
            now = datetime.now()
            current_app = current_context.get("current_app")
            current_space = current_context.get("current_space")
            time_of_day = current_context.get("time_of_day", now.hour)

            # Check temporal patterns
            async with self.db.execute(
                """
                SELECT action_type, target, confidence, frequency
                FROM temporal_patterns
                WHERE time_of_day = ? AND day_of_week = ?
                ORDER BY confidence DESC, frequency DESC
                LIMIT 10
            """,
                (time_of_day, now.weekday()),
            ) as cursor:
                temporal_rows = await cursor.fetchall()

            for row in temporal_rows:
                predictions.append(
                    {
                        "source": "temporal_pattern",
                        "action_type": row[0],
                        "target": row[1],
                        "confidence": row[2],
                        "reasoning": f"Typical behavior at {time_of_day}:00",
                    }
                )

            # Check app transitions
            if current_app:
                # Find common next apps
                async with self.db.execute(
                    """
                    SELECT app_name, confidence, usage_frequency
                    FROM app_usage_patterns
                    WHERE typical_time_of_day = ?
                      AND space_id = ?
                    ORDER BY confidence DESC
                    LIMIT 5
                """,
                    (time_of_day, current_space),
                ) as cursor:
                    app_rows = await cursor.fetchall()

                for row in app_rows:
                    predictions.append(
                        {
                            "source": "app_usage_pattern",
                            "action_type": "switch_app",
                            "target": row[0],
                            "confidence": row[1],
                            "reasoning": f"Commonly used on Space {current_space} at this time",
                        }
                    )

            # Check workflows
            workflows = await self.get_workflows_by_context(
                time_of_day=time_of_day, min_confidence=0.6
            )

            for workflow in workflows[:3]:
                predictions.append(
                    {
                        "source": "workflow_pattern",
                        "action_type": "workflow",
                        "target": workflow["workflow_name"],
                        "confidence": workflow["confidence"],
                        "reasoning": f"Part of frequent workflow",
                    }
                )

            # Sort by confidence
            predictions.sort(key=lambda x: x["confidence"], reverse=True)

            return predictions[:10]

        except Exception as e:
            logger.error(f"Error predicting next action: {e}", exc_info=True)
            return []

    def _sequence_matches(self, full_sequence: List, partial: List) -> bool:
        """Check if partial sequence appears in full sequence"""
        if not partial:
            return True
        if len(partial) > len(full_sequence):
            return False

        for i in range(len(full_sequence) - len(partial) + 1):
            if full_sequence[i : i + len(partial)] == partial:
                return True
        return False

    async def get_behavioral_insights(self) -> Dict[str, Any]:
        """
        Get comprehensive behavioral insights

        Returns:
            Dictionary of behavioral insights and statistics
        """
        insights = {
            "most_used_apps": [],
            "most_used_spaces": [],
            "common_workflows": [],
            "temporal_habits": [],
            "space_transitions": [],
            "prediction_accuracy": 0.0,
        }

        try:
            # Most used apps
            async with self.db.execute(
                """
                SELECT app_name, space_id, SUM(usage_frequency) as total_use,
                       AVG(confidence) as avg_conf
                FROM app_usage_patterns
                GROUP BY app_name, space_id
                ORDER BY total_use DESC
                LIMIT 10
            """
            ) as cursor:
                rows = await cursor.fetchall()
                insights["most_used_apps"] = [
                    {"app": r[0], "space": r[1], "usage": r[2], "confidence": r[3]} for r in rows
                ]

            # Most used spaces
            async with self.db.execute(
                """
                SELECT space_id, COUNT(*) as usage_count
                FROM workspace_usage
                GROUP BY space_id
                ORDER BY usage_count DESC
                LIMIT 5
            """
            ) as cursor:
                rows = await cursor.fetchall()
                insights["most_used_spaces"] = [
                    {"space_id": r[0], "usage_count": r[1]} for r in rows
                ]

            # Common workflows
            async with self.db.execute(
                """
                SELECT workflow_name, frequency, success_rate, confidence
                FROM user_workflows
                ORDER BY frequency DESC
                LIMIT 10
            """
            ) as cursor:
                rows = await cursor.fetchall()
                insights["common_workflows"] = [
                    {"name": r[0], "frequency": r[1], "success_rate": r[2], "confidence": r[3]}
                    for r in rows
                ]

            # Temporal habits
            async with self.db.execute(
                """
                SELECT time_of_day, day_of_week, action_type,
                       COUNT(*) as occurrences, AVG(confidence) as avg_conf
                FROM temporal_patterns
                GROUP BY time_of_day, day_of_week, action_type
                HAVING COUNT(*) > 2
                ORDER BY COUNT(*) DESC
                LIMIT 20
            """
            ) as cursor:
                rows = await cursor.fetchall()
                insights["temporal_habits"] = [
                    {
                        "hour": r[0],
                        "day": r[1],
                        "action": r[2],
                        "occurrences": r[3],
                        "confidence": r[4],
                    }
                    for r in rows
                ]

            # Space transitions
            async with self.db.execute(
                """
                SELECT from_space_id, to_space_id, trigger_app,
                       SUM(frequency) as total_transitions
                FROM space_transitions
                GROUP BY from_space_id, to_space_id, trigger_app
                ORDER BY total_transitions DESC
                LIMIT 15
            """
            ) as cursor:
                rows = await cursor.fetchall()
                insights["space_transitions"] = [
                    {"from": r[0], "to": r[1], "trigger": r[2], "frequency": r[3]} for r in rows
                ]

            # Overall prediction accuracy
            async with self.db.execute(
                """
                SELECT AVG(acceptance_rate) as avg_acceptance
                FROM proactive_suggestions
                WHERE times_suggested > 0
            """
            ) as cursor:
                row = await cursor.fetchone()
                if row and row[0]:
                    insights["prediction_accuracy"] = row[0]

            return insights

        except Exception as e:
            logger.error(f"Error getting behavioral insights: {e}", exc_info=True)
            return insights

    async def get_all_speaker_profiles(self) -> List[Dict]:
        """
        Get all speaker profiles from database for speaker recognition.

        Returns list of profile dicts with embeddings and metadata.
        INCLUDES: All acoustic features for BEAST MODE verification.
        """
        try:
            # Guard against uninitialized database
            if not self.db:
                logger.warning("Database not initialized yet - returning empty profiles")
                return []
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT speaker_id, speaker_name, voiceprint_embedding,
                           total_samples, average_pitch_hz, recognition_confidence,
                           is_primary_user, security_level, created_at, last_updated,
                           -- BEAST MODE: Acoustic features
                           pitch_mean_hz, pitch_std_hz, pitch_range_hz, pitch_min_hz, pitch_max_hz,
                           formant_f1_hz, formant_f1_std, formant_f2_hz, formant_f2_std,
                           formant_f3_hz, formant_f3_std, formant_f4_hz, formant_f4_std,
                           spectral_centroid_hz, spectral_centroid_std, spectral_rolloff_hz, spectral_rolloff_std,
                           spectral_flux, spectral_flux_std, spectral_entropy, spectral_entropy_std,
                           spectral_flatness, spectral_bandwidth_hz,
                           speaking_rate_wpm, speaking_rate_std, pause_ratio, pause_ratio_std,
                           syllable_rate, articulation_rate,
                           energy_mean, energy_std, energy_dynamic_range_db,
                           jitter_percent, jitter_std, shimmer_percent, shimmer_std,
                           harmonic_to_noise_ratio_db, hnr_std,
                           feature_covariance_matrix, feature_statistics,
                           enrollment_quality_score, feature_extraction_version,
                           embedding_dimension
                    FROM speaker_profiles
                """
                )
                rows = await cursor.fetchall()
                logger.info(f"Got {len(rows) if rows else 0} speaker profile rows")
                logger.info(
                    f"Type of rows: {type(rows)}, Type of first row: {type(rows[0]) if rows else 'empty'}"
                )

                if rows and len(rows) > 0:
                    logger.info(f"First row content: {rows[0]}")
                    logger.info(
                        f"First row keys: {list(rows[0].keys()) if hasattr(rows[0], 'keys') else 'no keys'}"
                    )

                profiles = []
                for raw_row in rows:
                    # Convert sqlite3.Row to dict for .get() support
                    row = _row_to_dict(raw_row)
                    profile = {
                        "speaker_id": row.get("speaker_id"),
                        "speaker_name": row.get("speaker_name"),
                        "voiceprint_embedding": row.get("voiceprint_embedding"),
                        "total_samples": row.get("total_samples"),
                        "average_pitch_hz": row.get("average_pitch_hz"),
                        "recognition_confidence": row.get("recognition_confidence"),
                        "is_primary_user": bool(row.get("is_primary_user", False)),
                        "security_level": row.get("security_level") or "standard",
                        "created_at": row.get("created_at"),
                        "last_updated": row.get("last_updated"),
                        "embedding_dimension": row.get("embedding_dimension"),
                        "updated_at": row.get("last_updated"),  # Map last_updated to updated_at for compatibility
                        "enrollment_quality_score": row.get("enrollment_quality_score"),
                        "feature_extraction_version": row.get("feature_extraction_version"),

                        # BEAST MODE: Add all acoustic features
                        "pitch_mean_hz": row.get("pitch_mean_hz"),
                        "pitch_std_hz": row.get("pitch_std_hz"),
                        "pitch_range_hz": row.get("pitch_range_hz"),
                        "pitch_min_hz": row.get("pitch_min_hz"),
                        "pitch_max_hz": row.get("pitch_max_hz"),

                        "formant_f1_hz": row.get("formant_f1_hz"),
                        "formant_f1_std": row.get("formant_f1_std"),
                        "formant_f2_hz": row.get("formant_f2_hz"),
                        "formant_f2_std": row.get("formant_f2_std"),
                        "formant_f3_hz": row.get("formant_f3_hz"),
                        "formant_f3_std": row.get("formant_f3_std"),
                        "formant_f4_hz": row.get("formant_f4_hz"),
                        "formant_f4_std": row.get("formant_f4_std"),

                        "spectral_centroid_hz": row.get("spectral_centroid_hz"),
                        "spectral_centroid_std": row.get("spectral_centroid_std"),
                        "spectral_rolloff_hz": row.get("spectral_rolloff_hz"),
                        "spectral_rolloff_std": row.get("spectral_rolloff_std"),
                        "spectral_flux": row.get("spectral_flux"),
                        "spectral_flux_std": row.get("spectral_flux_std"),
                        "spectral_entropy": row.get("spectral_entropy"),
                        "spectral_entropy_std": row.get("spectral_entropy_std"),
                        "spectral_flatness": row.get("spectral_flatness"),
                        "spectral_bandwidth_hz": row.get("spectral_bandwidth_hz"),

                        "speaking_rate_wpm": row.get("speaking_rate_wpm"),
                        "speaking_rate_std": row.get("speaking_rate_std"),
                        "pause_ratio": row.get("pause_ratio"),
                        "pause_ratio_std": row.get("pause_ratio_std"),
                        "syllable_rate": row.get("syllable_rate"),
                        "articulation_rate": row.get("articulation_rate"),

                        "energy_mean": row.get("energy_mean"),
                        "energy_std": row.get("energy_std"),
                        "energy_dynamic_range_db": row.get("energy_dynamic_range_db"),

                        "jitter_percent": row.get("jitter_percent"),
                        "jitter_std": row.get("jitter_std"),
                        "shimmer_percent": row.get("shimmer_percent"),
                        "shimmer_std": row.get("shimmer_std"),
                        "harmonic_to_noise_ratio_db": row.get("harmonic_to_noise_ratio_db"),
                        "hnr_std": row.get("hnr_std"),

                        "feature_covariance_matrix": row.get("feature_covariance_matrix"),
                        "feature_statistics": row.get("feature_statistics"),
                    }

                    # Add compatibility mappings for components expecting different field names
                    profile["name"] = profile["speaker_name"]  # Map speaker_name -> name
                    profile["embedding"] = profile["voiceprint_embedding"]  # Map voiceprint_embedding -> embedding

                    # Convert embedding to list if it's bytes
                    if profile["embedding"] and isinstance(profile["embedding"], (bytes, memoryview)):
                        import numpy as np
                        # Convert bytes to numpy array (assuming float32)
                        try:
                            embedding_array = np.frombuffer(profile["embedding"], dtype=np.float32)
                            profile["embedding"] = embedding_array.tolist()
                        except:
                            logger.warning(f"Could not convert embedding for {profile['speaker_name']}")
                            profile["embedding"] = None

                    # Check if this is a valid profile with embedding
                    has_embedding = profile.get("voiceprint_embedding") is not None

                    # Log if acoustic features are present
                    has_acoustic = any([
                        profile.get("pitch_mean_hz"),
                        profile.get("formant_f1_hz"),
                        profile.get("spectral_centroid_hz")
                    ])

                    # Skip profiles without embeddings (they're useless for recognition)
                    if not has_embedding:
                        logger.debug(f"⏭️  Skipping profile '{profile['speaker_name']}' - no embedding (incomplete enrollment)")
                        continue

                    if has_acoustic:
                        logger.info(f"✅ Profile '{profile['speaker_name']}' has BEAST MODE acoustic features")
                    else:
                        # Only warn for valid profiles (with embeddings) that are missing acoustic features
                        # For "unknown" or placeholder profiles, use debug level
                        speaker_name_lower = profile['speaker_name'].lower()
                        if speaker_name_lower in ('unknown', 'test', 'placeholder', ''):
                            logger.debug(f"⏭️  Profile '{profile['speaker_name']}' is a placeholder - missing acoustic features (expected)")
                        else:
                            logger.warning(f"⚠️  Profile '{profile['speaker_name']}' missing acoustic features - consider re-enrollment")

                    profiles.append(profile)

                return profiles

        except Exception as e:
            logger.error(f"Failed to get speaker profiles: {e}", exc_info=True)
            return []

    async def store_voice_sample(
        self,
        speaker_name: str,
        audio_data: bytes,
        embedding: Optional[np.ndarray] = None,
        confidence: float = 0.0,
        verified: bool = False,
        command: Optional[str] = None,
        transcription: Optional[str] = None,
        environment_type: Optional[str] = None,
        quality_score: Optional[float] = None,
        metadata: Optional[Dict] = None
    ) -> int:
        """
        Store voice sample for continuous learning with RLHF support

        Args:
            speaker_name: Speaker identifier
            audio_data: Raw audio bytes
            embedding: Optional pre-computed embedding
            confidence: Verification confidence score
            verified: Whether verification succeeded
            command: Command spoken (if any)
            transcription: Speech transcription
            environment_type: Environment (quiet, noisy, etc)
            quality_score: Audio quality score
            metadata: Additional metadata

        Returns:
            sample_id of stored sample
        """
        try:
            # Store in both SQLite and CloudSQL for redundancy
            embedding_bytes = embedding.tobytes() if embedding is not None else None

            # CloudSQL storage
            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            """
                            INSERT INTO voice_samples (
                                speaker_name, audio_data, embedding,
                                verification_confidence, verification_result,
                                command, transcription, environment_type,
                                quality_score, timestamp, metadata
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), %s)
                            RETURNING sample_id
                            """,
                            (
                                speaker_name, audio_data, embedding_bytes,
                                confidence, verified, command, transcription,
                                environment_type, quality_score,
                                json.dumps(metadata) if metadata else None
                            )
                        )
                        sample_id = (await cursor.fetchone())[0]
                        await conn.commit()

            # Local SQLite storage
            async with self.db.execute(
                """
                INSERT INTO voice_samples (
                    speaker_name, audio_data, embedding,
                    verification_confidence, verification_result,
                    command, transcription, environment_type,
                    quality_score, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    speaker_name, audio_data, embedding_bytes,
                    confidence, verified, command, transcription,
                    environment_type, quality_score,
                    json.dumps(metadata) if metadata else None
                )
            ) as cursor:
                await self.db.commit()
                local_sample_id = cursor.lastrowid

            logger.info(f"✅ Stored voice sample for {speaker_name} (confidence: {confidence:.2%}, verified: {verified})")

            # Trigger continuous learning if enough samples
            await self._check_and_trigger_learning(speaker_name)

            return sample_id if self.cloud_adapter else local_sample_id

        except Exception as e:
            logger.error(f"Failed to store voice sample: {e}")
            return -1

    async def get_voice_samples_for_training(
        self, speaker_name: str, limit: int = 50, min_confidence: float = 0.1
    ) -> List[Dict]:
        """
        Retrieve voice samples for ML training with RAG

        Args:
            speaker_name: Speaker to get samples for
            limit: Maximum samples to retrieve
            min_confidence: Minimum confidence threshold

        Returns:
            List of voice samples with metadata
        """
        try:
            if self.cloud_adapter:
                # Use CloudSQL for production
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            """
                            SELECT sample_id, audio_data, embedding, verification_confidence,
                                   verification_result, quality_score, timestamp, metadata
                            FROM voice_samples
                            WHERE speaker_name = %s
                              AND verification_confidence >= %s
                              AND used_for_training = FALSE
                            ORDER BY timestamp DESC
                            LIMIT %s
                            """,
                            (speaker_name, min_confidence, limit)
                        )

                        samples = []
                        for row in await cursor.fetchall():
                            samples.append({
                                'sample_id': row[0],
                                'audio_data': row[1],
                                'embedding': np.frombuffer(row[2]) if row[2] else None,
                                'confidence': row[3],
                                'verified': row[4],
                                'quality_score': row[5],
                                'timestamp': row[6],
                                'metadata': json.loads(row[7]) if row[7] else {}
                            })
                        return samples
            else:
                # Fallback to SQLite
                async with self.db.execute(
                    """
                    SELECT sample_id, audio_data, embedding, verification_confidence,
                           verification_result, quality_score, timestamp, metadata
                    FROM voice_samples
                    WHERE speaker_name = ?
                      AND verification_confidence >= ?
                      AND used_for_training = 0
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (speaker_name, min_confidence, limit)
                ) as cursor:
                    samples = []
                    async for row in cursor:
                        samples.append({
                            'sample_id': row[0],
                            'audio_data': row[1],
                            'embedding': np.frombuffer(row[2]) if row[2] else None,
                            'confidence': row[3],
                            'verified': row[4],
                            'quality_score': row[5],
                            'timestamp': row[6],
                            'metadata': json.loads(row[7]) if row[7] else {}
                        })
                    return samples

        except Exception as e:
            logger.error(f"Failed to retrieve voice samples: {e}")
            return []

    async def apply_rlhf_feedback(
        self, sample_id: int, feedback_score: float, feedback_notes: Optional[str] = None
    ):
        """
        Apply Reinforcement Learning from Human Feedback

        Args:
            sample_id: Voice sample ID
            feedback_score: Human feedback score (0-1)
            feedback_notes: Optional feedback notes
        """
        try:
            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            """
                            UPDATE voice_samples
                            SET feedback_score = %s, feedback_notes = %s
                            WHERE sample_id = %s
                            """,
                            (feedback_score, feedback_notes, sample_id)
                        )
                        await conn.commit()
            else:
                await self.db.execute(
                    """
                    UPDATE voice_samples
                    SET feedback_score = ?, feedback_notes = ?
                    WHERE sample_id = ?
                    """,
                    (feedback_score, feedback_notes, sample_id)
                )
                await self.db.commit()

            logger.info(f"✅ Applied RLHF feedback to sample {sample_id} (score: {feedback_score})")

        except Exception as e:
            logger.error(f"Failed to apply RLHF feedback: {e}")

    async def perform_incremental_learning(
        self, speaker_name: str, new_samples: List[Dict]
    ) -> Dict:
        """
        Perform incremental learning with new voice samples

        Args:
            speaker_name: Speaker to update
            new_samples: List of new voice samples

        Returns:
            Training results dictionary
        """
        try:
            # Get current embedding
            profile = await self.get_speaker_profile(speaker_name)
            if not profile:
                return {'success': False, 'error': 'Profile not found'}

            current_embedding = np.frombuffer(profile['voiceprint_embedding'])

            # Extract embeddings from new samples
            new_embeddings = []
            weights = []  # Weight by confidence and quality

            for sample in new_samples:
                if sample.get('embedding') is not None:
                    new_embeddings.append(sample['embedding'])
                    # Weight by confidence, quality, and feedback
                    weight = (
                        sample.get('confidence', 0.5) *
                        sample.get('quality_score', 0.5) *
                        sample.get('feedback_score', 1.0)
                    )
                    weights.append(weight)

            if not new_embeddings:
                return {'success': False, 'error': 'No valid embeddings'}

            # Weighted average for incremental update
            new_embeddings = np.array(new_embeddings)
            weights = np.array(weights)
            weights = weights / weights.sum()  # Normalize

            # Compute weighted average of new embeddings
            avg_new_embedding = np.average(new_embeddings, axis=0, weights=weights)

            # Incremental update: blend old and new
            update_weight = min(0.3, len(new_samples) * 0.05)  # Adaptive weight
            updated_embedding = (
                (1 - update_weight) * current_embedding +
                update_weight * avg_new_embedding
            )

            # Update speaker profile
            await self.update_speaker_embedding(
                speaker_name=speaker_name,
                embedding=updated_embedding,
                metadata={
                    'learning_type': 'incremental',
                    'samples_used': len(new_samples),
                    'update_weight': update_weight,
                    'timestamp': datetime.now().isoformat()
                }
            )

            # Record training history
            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            """
                            INSERT INTO ml_training_history (
                                speaker_name, samples_used, training_type,
                                improvement_score, timestamp, metadata
                            ) VALUES (%s, %s, %s, %s, NOW(), %s)
                            """,
                            (
                                speaker_name, len(new_samples), 'incremental',
                                update_weight, json.dumps({
                                    'avg_confidence': float(np.mean([s.get('confidence', 0) for s in new_samples])),
                                    'avg_quality': float(np.mean([s.get('quality_score', 0) for s in new_samples]))
                                })
                            )
                        )
                        await conn.commit()

            logger.info(f"✅ Incremental learning complete for {speaker_name} using {len(new_samples)} samples")

            return {
                'success': True,
                'samples_used': len(new_samples),
                'update_weight': update_weight,
                'improvement_estimate': update_weight * 0.5  # Rough estimate
            }

        except Exception as e:
            logger.error(f"Incremental learning failed: {e}")
            return {'success': False, 'error': str(e)}

    async def _check_and_trigger_learning(self, speaker_name: str):
        """
        Check if we should trigger automatic learning
        """
        try:
            # Count unused training samples
            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            """
                            SELECT COUNT(*) FROM voice_samples
                            WHERE speaker_name = %s AND used_for_training = FALSE
                            """,
                            (speaker_name,)
                        )
                        unused_count = (await cursor.fetchone())[0]
            else:
                async with self.db.execute(
                    """
                    SELECT COUNT(*) FROM voice_samples
                    WHERE speaker_name = ? AND used_for_training = 0
                    """,
                    (speaker_name,)
                ) as cursor:
                    unused_count = (await cursor.fetchone())[0]

            # Trigger learning if enough samples
            if unused_count >= 10:  # Configurable threshold
                logger.info(f"🎓 Triggering automatic learning for {speaker_name} ({unused_count} samples)")

                # Get samples and perform learning
                samples = await self.get_voice_samples_for_training(speaker_name, limit=20)
                if samples:
                    result = await self.perform_incremental_learning(speaker_name, samples)

                    if result.get('success'):
                        # Mark samples as used
                        sample_ids = [s['sample_id'] for s in samples]
                        await self._mark_samples_as_used(sample_ids)

        except Exception as e:
            logger.error(f"Auto-learning check failed: {e}")

    async def _mark_samples_as_used(self, sample_ids: List[int]):
        """Mark voice samples as used for training"""
        try:
            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            f"""
                            UPDATE voice_samples
                            SET used_for_training = TRUE
                            WHERE sample_id IN ({','.join(['%s'] * len(sample_ids))})
                            """,
                            sample_ids
                        )
                        await conn.commit()
            else:
                placeholders = ','.join(['?' * len(sample_ids)])
                await self.db.execute(
                    f"""
                    UPDATE voice_samples
                    SET used_for_training = 1
                    WHERE sample_id IN ({placeholders})
                    """,
                    sample_ids
                )
                await self.db.commit()

        except Exception as e:
            logger.error(f"Failed to mark samples as used: {e}")

    async def manage_sample_freshness(
        self, speaker_name: str, max_age_days: int = 30, target_sample_count: int = 100
    ) -> Dict:
        """
        Advanced sample freshness management system

        Automatically:
        - Ages out old samples
        - Balances sample distribution across time
        - Maintains optimal sample count
        - Ensures representation across different conditions

        Args:
            speaker_name: Speaker to manage samples for
            max_age_days: Maximum age before samples are considered stale
            target_sample_count: Target number of active samples to maintain

        Returns:
            Dict with freshness statistics and actions taken
        """
        try:
            from datetime import timedelta
            cutoff_date = datetime.now() - timedelta(days=max_age_days)

            stats = {
                'speaker_name': speaker_name,
                'total_samples': 0,
                'fresh_samples': 0,
                'stale_samples': 0,
                'samples_archived': 0,
                'samples_retained': 0,
                'freshness_score': 0.0,
                'actions': []
            }

            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        # Get all samples with freshness analysis
                        await cursor.execute(
                            """
                            SELECT
                                sample_id,
                                timestamp,
                                verification_confidence,
                                quality_score,
                                used_for_training,
                                EXTRACT(EPOCH FROM (NOW() - timestamp)) / 86400 as age_days
                            FROM voice_samples
                            WHERE speaker_name = %s
                            ORDER BY timestamp DESC
                            """,
                            (speaker_name,)
                        )

                        samples = await cursor.fetchall()
                        stats['total_samples'] = len(samples)

                        if not samples:
                            return stats

                        # Categorize samples by freshness
                        fresh = []
                        stale = []

                        for sample in samples:
                            sample_id, timestamp, confidence, quality, used, age_days = sample

                            if age_days <= max_age_days:
                                fresh.append({
                                    'id': sample_id,
                                    'age': age_days,
                                    'confidence': confidence or 0,
                                    'quality': quality or 0,
                                    'used': used,
                                    'score': (confidence or 0) * (quality or 0) / (1 + age_days/30)
                                })
                            else:
                                stale.append({
                                    'id': sample_id,
                                    'age': age_days,
                                    'used': used
                                })

                        stats['fresh_samples'] = len(fresh)
                        stats['stale_samples'] = len(stale)

                        # Calculate freshness score (0-1)
                        if samples:
                            avg_age = sum(s.get('age', 0) for s in fresh) / len(fresh) if fresh else max_age_days
                            stats['freshness_score'] = max(0, 1 - (avg_age / max_age_days))

                        # Strategy 1: Archive old, low-quality samples
                        if len(stale) > 0:
                            # Keep stale samples that were high quality for historical reference
                            stale_to_archive = [
                                s['id'] for s in stale
                                if not s['used']  # Archive unused stale samples
                            ][:50]  # Limit batch size

                            if stale_to_archive:
                                await cursor.execute(
                                    f"""
                                    UPDATE voice_samples
                                    SET metadata = jsonb_set(
                                        COALESCE(metadata, '{{}}')::jsonb,
                                        '{{archived}}',
                                        'true'::jsonb
                                    )
                                    WHERE sample_id IN ({','.join(['%s'] * len(stale_to_archive))})
                                    """,
                                    stale_to_archive
                                )
                                stats['samples_archived'] = len(stale_to_archive)
                                stats['actions'].append(f"Archived {len(stale_to_archive)} stale samples")

                        # Strategy 2: Maintain target count with best samples
                        if len(fresh) > target_sample_count:
                            # Sort by composite score (confidence * quality / age_factor)
                            fresh.sort(key=lambda x: x['score'], reverse=True)

                            # Keep top samples
                            samples_to_keep = [s['id'] for s in fresh[:target_sample_count]]
                            samples_to_archive = [s['id'] for s in fresh[target_sample_count:]]

                            if samples_to_archive:
                                await cursor.execute(
                                    f"""
                                    UPDATE voice_samples
                                    SET metadata = jsonb_set(
                                        COALESCE(metadata, '{{}}')::jsonb,
                                        '{{archived}}',
                                        'true'::jsonb
                                    )
                                    WHERE sample_id IN ({','.join(['%s'] * len(samples_to_archive))})
                                    """,
                                    samples_to_archive
                                )
                                stats['samples_archived'] += len(samples_to_archive)
                                stats['actions'].append(f"Archived {len(samples_to_archive)} excess samples")

                            stats['samples_retained'] = len(samples_to_keep)
                        else:
                            stats['samples_retained'] = len(fresh)

                        # Strategy 3: Identify need for refresh
                        if stats['freshness_score'] < 0.7:
                            stats['actions'].append(
                                f"RECOMMENDATION: Schedule enrollment refresh (freshness: {stats['freshness_score']:.1%})"
                            )

                        if stats['fresh_samples'] < 20:
                            stats['actions'].append(
                                f"WARNING: Low sample count ({stats['fresh_samples']}). Record more samples."
                            )

                        await conn.commit()

            logger.info(f"✅ Sample freshness managed for {speaker_name}: {stats['freshness_score']:.1%} fresh")
            return stats

        except Exception as e:
            logger.error(f"Sample freshness management failed: {e}")
            return {'error': str(e)}

    async def get_sample_freshness_report(self, speaker_name: str) -> Dict:
        """
        Generate comprehensive freshness report

        Returns:
            Detailed report on sample age distribution, quality, and recommendations
        """
        try:
            report = {
                'speaker_name': speaker_name,
                'report_timestamp': datetime.now().isoformat(),
                'age_distribution': {},
                'quality_distribution': {},
                'recommendations': [],
                'statistics': {}
            }

            if self.cloud_adapter:
                async with self.cloud_adapter.get_connection() as conn:
                    async with conn.cursor() as cursor:
                        # Age distribution
                        await cursor.execute(
                            """
                            SELECT
                                CASE
                                    WHEN EXTRACT(EPOCH FROM (NOW() - timestamp)) / 86400 <= 7 THEN '0-7 days'
                                    WHEN EXTRACT(EPOCH FROM (NOW() - timestamp)) / 86400 <= 14 THEN '8-14 days'
                                    WHEN EXTRACT(EPOCH FROM (NOW() - timestamp)) / 86400 <= 30 THEN '15-30 days'
                                    WHEN EXTRACT(EPOCH FROM (NOW() - timestamp)) / 86400 <= 60 THEN '31-60 days'
                                    ELSE '60+ days'
                                END as age_bracket,
                                COUNT(*) as count,
                                AVG(verification_confidence) as avg_confidence,
                                AVG(quality_score) as avg_quality
                            FROM voice_samples
                            WHERE speaker_name = %s
                            GROUP BY age_bracket
                            ORDER BY
                                CASE age_bracket
                                    WHEN '0-7 days' THEN 1
                                    WHEN '8-14 days' THEN 2
                                    WHEN '15-30 days' THEN 3
                                    WHEN '31-60 days' THEN 4
                                    ELSE 5
                                END
                            """,
                            (speaker_name,)
                        )

                        age_dist = await cursor.fetchall()
                        for bracket, count, avg_conf, avg_qual in age_dist:
                            report['age_distribution'][bracket] = {
                                'count': count,
                                'avg_confidence': float(avg_conf) if avg_conf else 0,
                                'avg_quality': float(avg_qual) if avg_qual else 0
                            }

                        # Generate recommendations
                        total_samples = sum(d['count'] for d in report['age_distribution'].values())
                        recent_samples = report['age_distribution'].get('0-7 days', {}).get('count', 0)

                        if recent_samples < total_samples * 0.3:
                            report['recommendations'].append({
                                'priority': 'HIGH',
                                'action': 'Record new samples',
                                'reason': f'Only {recent_samples}/{total_samples} samples are recent (< 7 days)'
                            })

                        old_samples = report['age_distribution'].get('60+ days', {}).get('count', 0)
                        if old_samples > total_samples * 0.5:
                            report['recommendations'].append({
                                'priority': 'MEDIUM',
                                'action': 'Archive old samples',
                                'reason': f'{old_samples} samples are > 60 days old'
                            })

                        if total_samples < 30:
                            report['recommendations'].append({
                                'priority': 'HIGH',
                                'action': 'Increase sample count',
                                'reason': f'Only {total_samples} total samples (target: 30-50)'
                            })

            return report

        except Exception as e:
            logger.error(f"Freshness report generation failed: {e}")
            return {'error': str(e)}

    async def update_speaker_embedding(
        self,
        speaker_id: int,
        embedding: bytes,
        confidence: float,
        is_primary_user: bool = False,
    ) -> bool:
        """
        Update speaker profile with voice embedding.

        Args:
            speaker_id: Speaker profile ID
            embedding: Voice embedding bytes (numpy array serialized)
            confidence: Recognition confidence (0.0-1.0)
            is_primary_user: Mark as device owner (Derek J. Russell)

        Returns:
            True if successful
        """
        try:
            async with self.db.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE speaker_profiles
                    SET voiceprint_embedding = ?,
                        recognition_confidence = ?,
                        is_primary_user = ?,
                        security_level = ?,
                        last_updated = CURRENT_TIMESTAMP
                    WHERE speaker_id = ?
                """,
                    (
                        embedding,
                        confidence,
                        is_primary_user,  # PostgreSQL expects boolean, not int
                        "admin" if is_primary_user else "standard",
                        speaker_id,
                    ),
                )

                await self.db.commit()

                logger.info(
                    f"✅ Updated speaker embedding (ID: {speaker_id}, confidence: {confidence:.2f}, owner: {is_primary_user})"
                )

                return True

        except Exception as e:
            logger.error(f"Failed to update speaker embedding: {e}")
            return False

    async def close(self):
        """Close database connections gracefully with timeout protection"""
        logger.info("🧹 Closing learning database...")

        # Signal shutdown to background tasks
        self._shutdown_event.set()

        # Cancel and wait for background tasks (with timeout)
        if self._background_tasks:
            logger.debug(f"   Canceling {len(self._background_tasks)} background tasks...")
            for task in self._background_tasks:
                if not task.done():
                    task.cancel()

            # Wait for task cancellation with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._background_tasks, return_exceptions=True),
                    timeout=2.0
                )
                logger.debug("   ✓ Background tasks cancelled")
            except asyncio.TimeoutError:
                logger.warning("   ⚠ Background task cancellation timeout")

        # Flush pending batches (with timeout)
        try:
            await asyncio.wait_for(
                asyncio.gather(
                    self._flush_goal_batch(),
                    self._flush_action_batch(),
                    self._flush_pattern_batch(),
                    return_exceptions=True
                ),
                timeout=5.0
            )
            logger.debug("   ✓ Pending batches flushed")
        except asyncio.TimeoutError:
            logger.warning("   ⚠ Batch flush timeout - some data may not be saved")
        except Exception as e:
            logger.warning(f"   ⚠ Batch flush error: {e}")

        # Close ChromaDB client (releases background threads)
        if self.chroma_client:
            try:
                logger.debug("   Closing ChromaDB client...")
                # ChromaDB doesn't have an async close, but we should clean up references
                self.goal_collection = None
                self.pattern_collection = None
                self.context_collection = None
                self.chroma_client = None
                logger.debug("   ✓ ChromaDB client cleaned up")
            except Exception as e:
                logger.warning(f"   ⚠ ChromaDB cleanup error: {e}")

        # Close database connection
        if self.db:
            try:
                await asyncio.wait_for(self.db.close(), timeout=3.0)
                logger.debug("   ✓ Database connection closed")
            except asyncio.TimeoutError:
                logger.warning("   ⚠ Database close timeout")
            except Exception as e:
                logger.warning(f"   ⚠ Database close error: {e}")

        # Reset state
        self._initialized = False
        self._background_tasks.clear()

        logger.info("✅ Learning database closed gracefully")


class CrossRepoSync:
    """
    Intelligent cross-repository database synchronization.

    Syncs learning data between:
    - JARVIS (main system)
    - JARVIS Prime (advanced model server)
    - Reactor Core (multi-agent orchestration)

    Features:
    - Async parallel sync across all repos
    - Intelligent conflict resolution (latest wins)
    - Delta sync (only changed records)
    - Automatic reconnection on failure
    - Zero hardcoding - dynamic repo detection
    """

    def __init__(self, jarvis_db: "JARVISLearningDatabase"):
        """
        Initialize cross-repo sync.

        Args:
            jarvis_db: Main JARVIS learning database instance
        """
        self.jarvis_db = jarvis_db
        self.logger = logging.getLogger(__name__)

        # Detect available repos dynamically
        self.repos = self._detect_repos()
        self._sync_interval = 60  # seconds
        self._sync_task = None
        self._running = False

    def _detect_repos(self) -> Dict[str, Any]:
        """
        Dynamically detect available cross-repo integrations.

        Returns:
            Dict mapping repo name to connection info
        """
        repos = {}

        # Try to detect JARVIS Prime
        try:
            from core.jarvis_prime_client import JARVISPrimeClient
            repos['jarvis_prime'] = {
                'client': JARVISPrimeClient(),
                'available': True,
                'type': 'http_api'
            }
            self.logger.info("✅ Detected JARVIS Prime integration")
        except (ImportError, Exception) as e:
            self.logger.debug(f"JARVIS Prime not available: {e}")

        # Try to detect Reactor Core
        try:
            from autonomy.reactor_core_integration import get_reactor_integration
            repos['reactor_core'] = {
                'client': get_reactor_integration(),
                'available': True,
                'type': 'websocket'
            }
            self.logger.info("✅ Detected Reactor Core integration")
        except (ImportError, Exception) as e:
            self.logger.debug(f"Reactor Core not available: {e}")

        return repos

    async def start_sync(self):
        """Start automatic background synchronization."""
        if self._running:
            self.logger.warning("Cross-repo sync already running")
            return

        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        self.logger.info(f"🔄 Cross-repo sync started (interval: {self._sync_interval}s)")
        self.logger.info(f"   Connected repos: {list(self.repos.keys())}")

    async def stop_sync(self):
        """Stop automatic synchronization."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        self.logger.info("🔄 Cross-repo sync stopped")

    async def _sync_loop(self):
        """Background sync loop."""
        while self._running:
            try:
                await self.sync_all()
                await asyncio.sleep(self._sync_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in sync loop: {e}")
                await asyncio.sleep(self._sync_interval)

    async def sync_all(self):
        """
        Sync all data with all available repos in parallel.

        Syncs:
        - Goals and patterns
        - Voice samples (metadata only, not audio)
        - Behavioral insights
        - Workflow patterns
        """
        if not self.repos:
            self.logger.debug("No repos available for sync")
            return

        # Sync in parallel across all repos
        sync_tasks = []
        for repo_name, repo_info in self.repos.items():
            if repo_info.get('available'):
                sync_tasks.append(self._sync_repo(repo_name, repo_info))

        if sync_tasks:
            results = await asyncio.gather(*sync_tasks, return_exceptions=True)
            success_count = sum(1 for r in results if not isinstance(r, Exception))
            self.logger.debug(f"🔄 Sync completed: {success_count}/{len(results)} repos synced")

    async def _sync_repo(self, repo_name: str, repo_info: Dict):
        """
        Sync data with a specific repo.

        Args:
            repo_name: Name of the repository
            repo_info: Repository connection info
        """
        try:
            client = repo_info['client']

            # Get recent learning data from JARVIS
            recent_data = await self._get_recent_learning_data()

            # Send to repo (method depends on repo type)
            if repo_name == 'jarvis_prime':
                await self._sync_to_jarvis_prime(client, recent_data)
            elif repo_name == 'reactor_core':
                await self._sync_to_reactor_core(client, recent_data)

            self.logger.debug(f"✅ Synced to {repo_name}")

        except Exception as e:
            self.logger.error(f"❌ Failed to sync to {repo_name}: {e}")
            raise

    async def _get_recent_learning_data(self, since_seconds: int = 3600) -> Dict[str, List]:
        """
        Get recent learning data from JARVIS database.

        Args:
            since_seconds: Get data from last N seconds (default: 1 hour)

        Returns:
            Dict with recent goals, patterns, insights
        """
        since_time = datetime.now() - timedelta(seconds=since_seconds)

        data = {
            'goals': [],
            'patterns': [],
            'insights': [],
            'timestamp': datetime.now().isoformat()
        }

        try:
            # Get recent goals
            async with self.jarvis_db.db.execute(
                """
                SELECT goal_id, goal_type, confidence, description, created_at
                FROM goals
                WHERE created_at >= ?
                ORDER BY created_at DESC
                LIMIT 100
                """,
                (since_time.isoformat(),)
            ) as cursor:
                async for row in cursor:
                    data['goals'].append({
                        'goal_id': row[0],
                        'goal_type': row[1],
                        'confidence': row[2],
                        'description': row[3],
                        'created_at': row[4]
                    })

            # Get recent behavioral insights (if available)
            insights = await self.jarvis_db.get_behavioral_insights()
            if insights:
                data['insights'] = [insights]

        except Exception as e:
            self.logger.error(f"Error getting recent learning data: {e}")

        return data

    async def _sync_to_jarvis_prime(self, client, data: Dict):
        """
        Sync learning data to JARVIS Prime.

        Args:
            client: JARVIS Prime client instance
            data: Learning data to sync
        """
        # Check if client has sync method
        if hasattr(client, 'sync_learning_data'):
            await client.sync_learning_data(data)
        elif hasattr(client, 'post'):
            # Fallback to generic HTTP POST
            await client.post('/api/sync/learning', json=data)

    async def _sync_to_reactor_core(self, client, data: Dict):
        """
        Sync learning data to Reactor Core.

        Args:
            client: Reactor Core client instance
            data: Learning data to sync
        """
        # Check if client has sync method
        if hasattr(client, 'sync_learning_data'):
            await client.sync_learning_data(data)
        elif hasattr(client, 'send_message'):
            # Fallback to websocket message
            await client.send_message({
                'type': 'learning_sync',
                'data': data
            })


# Global instance with async initialization
_db_instance = None
_db_lock = asyncio.Lock()
_cross_repo_sync = None


async def get_learning_database(config: Optional[Dict] = None) -> JARVISLearningDatabase:
    """Get or create the global async learning database"""
    global _db_instance

    async with _db_lock:
        if _db_instance is None:
            _db_instance = JARVISLearningDatabase(config=config)
            await _db_instance.initialize()
        return _db_instance


async def get_cross_repo_sync(auto_start: bool = True) -> CrossRepoSync:
    """
    Get or create the global cross-repo sync instance.

    Args:
        auto_start: Automatically start sync loop (default: True)

    Returns:
        CrossRepoSync instance
    """
    global _cross_repo_sync, _db_instance

    async with _db_lock:
        if _cross_repo_sync is None:
            # Ensure database is initialized first
            if _db_instance is None:
                _db_instance = await get_learning_database()

            _cross_repo_sync = CrossRepoSync(_db_instance)

            if auto_start:
                await _cross_repo_sync.start_sync()
                logger.info("🔄 Cross-repo sync initialized and started")

        return _cross_repo_sync


async def close_learning_database():
    """Close the global learning database instance"""
    global _db_instance, _cross_repo_sync

    async with _db_lock:
        # Stop cross-repo sync first
        if _cross_repo_sync is not None:
            await _cross_repo_sync.stop_sync()
            _cross_repo_sync = None

        # Then close database
        if _db_instance is not None:
            await _db_instance.close()
            _db_instance = None
            logger.info("✅ Global learning database instance closed")


async def test_database():
    """Test the advanced learning database"""
    print("🗄️ Testing Advanced JARVIS Learning Database")
    print("=" * 60)

    db = await get_learning_database()

    # Test storing a goal
    goal = {
        "goal_id": "test_goal_1",
        "goal_type": "meeting_preparation",
        "goal_level": "HIGH",
        "description": "Prepare for team meeting",
        "confidence": 0.92,
        "evidence": [{"source": "calendar", "data": "meeting in 10 min"}],
    }

    goal_id = await db.store_goal(goal)
    print(f"✅ Stored goal: {goal_id}")

    # Test batch storage
    for i in range(5):
        await db.store_goal({"goal_type": "test", "confidence": 0.5 + i * 0.1}, batch=True)
    print(f"✅ Queued 5 goals for batch insert")

    # Test storing an action
    action = {
        "action_id": "test_action_1",
        "action_type": "connect_display",
        "target": "Living Room TV",
        "goal_id": goal_id,
        "confidence": 0.85,
        "success": True,
        "execution_time": 0.45,
    }

    action_id = await db.store_action(action)
    print(f"✅ Stored action: {action_id}")

    # Test learning display pattern
    await db.learn_display_pattern(
        "Living Room TV", {"apps": ["keynote", "calendar"], "time": "09:00"}
    )
    print("✅ Learned display pattern")

    # Test learning preference
    await db.learn_preference("display", "default", "Living Room TV", 0.8)
    print("✅ Learned preference")

    # Get metrics
    metrics = await db.get_learning_metrics()
    print("\n📊 Learning Metrics:")
    print(f"   Total Goals: {metrics['goals']['total_goals']}")
    print(f"   Total Actions: {metrics['actions']['total_actions']}")
    print(f"   Total Patterns: {metrics['patterns']['total_patterns']}")
    print(
        f"   Pattern Cache Hit Rate: {metrics['cache_performance']['pattern_cache_hit_rate']:.2%}"
    )

    # Test pattern analysis
    patterns = await db.analyze_patterns()
    print(f"\n🔍 Analyzed {len(patterns)} patterns")

    await db.close()
    print("\n✅ Advanced database test complete!")


if __name__ == "__main__":
    asyncio.run(test_database())
