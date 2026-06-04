"""Database read replicas and SQLite3 optimization utilities."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class SQLite3OptimizationConfig:
    """Configuration for SQLite3 optimizations."""
    
    # WAL mode - enables multiple readers
    journal_mode: str = "WAL"
    
    # Memory for query optimization
    cache_size: int = 10000  # Pages (negative for MB)
    
    # Synchronization level (FULL=safer, NORMAL=faster)
    synchronous: str = "NORMAL"
    
    # Temp store location
    temp_store: str = "MEMORY"
    
    # Query timeout
    busy_timeout: float = 10.0
    
    # Enable memory-mapped I/O
    mmap_size: int = 30000000  # 30MB
    
    # Optimization settings
    optimize: bool = True
    automatic_indexing: bool = True
    query_only: bool = False
    
    # Foreign keys enforcement
    foreign_keys: bool = True
    
    # Checkpoint settings for WAL
    wal_autocheckpoint: int = 1000  # Pages before auto-checkpoint


class DatabaseReplica:
    """Represents a read-only replica of the database."""
    
    def __init__(
        self,
        db_path: str,
        *,
        timeout: float = 10.0,
        detect_types: int = 0,
    ):
        self.db_path = db_path
        self.timeout = timeout
        self.detect_types = detect_types
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
    
    def connect(self) -> sqlite3.Connection:
        """Get or create read-only connection."""
        with self._lock:
            if self._conn is None:
                self._conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro",
                    timeout=self.timeout,
                    detect_types=self.detect_types,
                    uri=True,
                    check_same_thread=False,
                )
                self._conn.row_factory = sqlite3.Row
                # Read-only optimization
                self._conn.execute("PRAGMA query_only = ON")
            return self._conn
    
    def close(self):
        """Close the replica connection."""
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None
    
    def execute(self, sql: str, params=None):
        """Execute a read-only query."""
        conn = self.connect()
        if params is None:
            params = ()
        with self._lock:
            return conn.execute(sql, tuple(params))
    
    def fetchone(self, sql: str, params=None):
        """Fetch one result."""
        with self._lock:
            return self.execute(sql, params).fetchone()
    
    def fetchall(self, sql: str, params=None):
        """Fetch all results."""
        with self._lock:
            return self.execute(sql, params).fetchall()
    
    def scalar(self, sql: str, params=None, default=None):
        """Fetch a scalar value."""
        with self._lock:
            row = self.fetchone(sql, params)
            if row is None:
                return default
            return row[0]


class DatabaseReplicaPool:
    """Pool of read-only database replicas with round-robin distribution."""
    
    def __init__(
        self,
        db_path: str,
        replica_count: int = 3,
        timeout: float = 10.0,
        detect_types: int = 0,
    ):
        self.db_path = db_path
        self.replica_count = replica_count
        self.timeout = timeout
        self.detect_types = detect_types
        
        self._replicas = [
            DatabaseReplica(
                db_path,
                timeout=timeout,
                detect_types=detect_types,
            )
            for _ in range(replica_count)
        ]
        self._current_index = 0
        self._lock = threading.RLock()
    
    def get_replica(self) -> DatabaseReplica:
        """Get the next replica in round-robin fashion."""
        with self._lock:
            replica = self._replicas[self._current_index]
            self._current_index = (self._current_index + 1) % len(self._replicas)
            return replica
    
    def execute(self, sql: str, params=None):
        """Execute query on a replica."""
        replica = self.get_replica()
        return replica.execute(sql, params)
    
    def fetchone(self, sql: str, params=None):
        """Fetch one result from a replica."""
        replica = self.get_replica()
        return replica.fetchone(sql, params)
    
    def fetchall(self, sql: str, params=None):
        """Fetch all results from a replica."""
        replica = self.get_replica()
        return replica.fetchall(sql, params)
    
    def scalar(self, sql: str, params=None, default=None):
        """Fetch scalar from a replica."""
        replica = self.get_replica()
        return replica.scalar(sql, params, default)
    
    def close_all(self):
        """Close all replica connections."""
        for replica in self._replicas:
            replica.close()


class OptimizedDatabase:
    """SQLite3 database with built-in optimizations."""
    
    def __init__(
        self,
        path: str = ":memory:",
        *,
        shared_cache: bool = False,
        timeout: float = 10.0,
        pragmas: dict | None = None,
        detect_types: int = 0,
        debug: bool = False,
        optimization_config: SQLite3OptimizationConfig | None = None,
        enable_replicas: bool = True,
        replica_count: int = 3,
    ):
        self.debug = debug
        self._lock = threading.RLock()
        self._tx_depth = threading.local()
        self.optimization_config = optimization_config or SQLite3OptimizationConfig()
        
        # Connection setup
        if shared_cache:
            dsn = "file::memory:?cache=shared" if path == ":memory:" else path
            self._conn = sqlite3.connect(
                dsn,
                check_same_thread=False,
                timeout=timeout,
                detect_types=detect_types,
                uri=True,
            )
        else:
            self._conn = sqlite3.connect(
                path,
                check_same_thread=False,
                timeout=timeout,
                detect_types=detect_types,
            )
        
        self._conn.row_factory = sqlite3.Row
        
        # Apply SQLite3 optimizations
        self._apply_optimizations(timeout, pragmas)
        
        # Setup read replicas if enabled
        self._replicas: Optional[DatabaseReplicaPool] = None
        if enable_replicas and path != ":memory:":
            self._replicas = DatabaseReplicaPool(
                path,
                replica_count=replica_count,
                timeout=timeout,
                detect_types=detect_types,
            )
    
    def _apply_optimizations(self, timeout: float, custom_pragmas: dict | None):
        """Apply SQLite3 optimization pragmas."""
        config = self.optimization_config
        
        base_pragmas = {
            "journal_mode": config.journal_mode,
            "cache_size": -config.cache_size,  # Negative = MB
            "synchronous": config.synchronous,
            "temp_store": config.temp_store,
            "busy_timeout": int(timeout * 1000),
            "mmap_size": config.mmap_size,
            "foreign_keys": 1 if config.foreign_keys else 0,
            "query_only": 1 if config.query_only else 0,
            "automatic_index": 1 if config.automatic_indexing else 0,
        }
        
        if custom_pragmas:
            base_pragmas.update(custom_pragmas)
        
        with self._lock:
            for key, value in base_pragmas.items():
                self._log(f"PRAGMA {key}={value}")
                self._conn.execute(f"PRAGMA {key}={value}")
            
            # WAL checkpoint settings
            if config.journal_mode.upper() == "WAL":
                self._log(f"PRAGMA wal_autocheckpoint={config.wal_autocheckpoint}")
                self._conn.execute(f"PRAGMA wal_autocheckpoint={config.wal_autocheckpoint}")
            
            # Run optimization if enabled
            if config.optimize:
                self._log("PRAGMA optimize")
                self._conn.execute("PRAGMA optimize")
    
    def _log(self, *parts):
        if self.debug:
            print("[DB]", *parts)
    
    def _get_tx_depth(self) -> int:
        return getattr(self._tx_depth, "value", 0)
    
    def _set_tx_depth(self, value: int):
        self._tx_depth.value = value
    
    def in_transaction(self) -> bool:
        return self._get_tx_depth() > 0
    
    def set_pragma(self, name: str, value):
        """Set a PRAGMA value."""
        from .orm import validate_identifier
        validate_identifier(name)
        sql = f"PRAGMA {name}={value}"
        with self._lock:
            self._log(sql)
            self._conn.execute(sql)
    
    def get_pragma(self, name: str):
        """Get a PRAGMA value."""
        from .orm import validate_identifier
        validate_identifier(name)
        sql = f"PRAGMA {name}"
        with self._lock:
            self._log(sql)
            cur = self._conn.execute(sql)
            row = cur.fetchone()
            return None if row is None else row[0]
    
    def execute(self, sql: str, params=None, *, commit: bool | None = None):
        """Execute a query (write or read)."""
        from .orm import _is_write_statement
        
        if params is None:
            params = ()
        if commit is None:
            commit = _is_write_statement(sql)
        
        with self._lock:
            self._log("SQL:", sql.strip(), "params=", params)
            cur = self._conn.execute(sql, tuple(params))
            if commit and not self.in_transaction():
                self._conn.commit()
            return cur
    
    def executemany(
        self,
        sql: str,
        seq_of_params,
        *,
        commit: bool | None = None,
    ) -> int:
        """Execute many queries."""
        from .orm import _is_write_statement
        
        if commit is None:
            commit = _is_write_statement(sql)
        
        with self._lock:
            self._log("SQL many:", sql.strip())
            cur = self._conn.executemany(sql, [tuple(p) for p in seq_of_params])
            if commit and not self.in_transaction():
                self._conn.commit()
            return cur.rowcount
    
    def fetchone(self, sql: str, params=None):
        """Fetch one row."""
        return self.execute(sql, params).fetchone()
    
    def fetchall(self, sql: str, params=None):
        """Fetch all rows."""
        return self.execute(sql, params).fetchall()
    
    def scalar(self, sql: str, params=None, default=None):
        """Fetch a scalar value."""
        row = self.fetchone(sql, params)
        if row is None:
            return default
        return row[0]
    
    def read_replica_fetchone(self, sql: str, params=None):
        """Execute query on a read replica."""
        if self._replicas is None:
            return self.fetchone(sql, params)
        return self._replicas.fetchone(sql, params)
    
    def read_replica_fetchall(self, sql: str, params=None):
        """Execute query on a read replica."""
        if self._replicas is None:
            return self.fetchall(sql, params)
        return self._replicas.fetchall(sql, params)
    
    def read_replica_scalar(self, sql: str, params=None, default=None):
        """Execute query on a read replica."""
        if self._replicas is None:
            return self.scalar(sql, params, default)
        return self._replicas.scalar(sql, params, default)
    
    def checkpoint(self, mode: str = "RESTART"):
        """Perform WAL checkpoint."""
        with self._lock:
            self._log(f"PRAGMA wal_checkpoint({mode})")
            self._conn.execute(f"PRAGMA wal_checkpoint({mode})")
    
    def vacuum(self):
        """Vacuum the database."""
        with self._lock:
            self._log("VACUUM")
            self._conn.execute("VACUUM")
    
    def transaction(self):
        """Get a transaction context manager."""
        from .orm import Transaction
        return Transaction(self)
    
    def close(self):
        """Close the database and all replicas."""
        with self._lock:
            if self._replicas:
                self._replicas.close_all()
            self._conn.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


__all__ = [
    "SQLite3OptimizationConfig",
    "DatabaseReplica",
    "DatabaseReplicaPool",
    "OptimizedDatabase",
]
