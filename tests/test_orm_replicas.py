"""Tests for database replicas and SQLite3 optimizations."""

import os
import tempfile
import threading
import time
from wsbuilder.db_replicas import (
    DatabaseReplica,
    DatabaseReplicaPool,
    OptimizedDatabase,
    SQLite3OptimizationConfig,
)
from wsbuilder.orm import Database, Model, TextField, IntegerField


class TestUser(Model):
    """Test model for replica tests."""
    __tablename__ = "test_users"
    
    id = IntegerField(primary_key=True, auto_increment=True)
    name = TextField()
    email = TextField()


def test_sqlite3_optimization_config():
    """Test SQLite3 optimization config."""
    config = SQLite3OptimizationConfig()
    assert config.journal_mode == "WAL"
    assert config.cache_size == 10000
    assert config.synchronous == "NORMAL"
    assert config.temp_store == "MEMORY"
    assert config.mmap_size == 30000000
    assert config.foreign_keys is True


def test_database_replica_read_only():
    """Test that read-only replica works."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Create and populate database
        db = Database(db_path, enable_wal=True)
        TestUser.create_table(db)
        user = TestUser.create(db, name="Alice", email="alice@example.com")
        db.close()
        
        # Read from replica
        replica = DatabaseReplica(db_path)
        rows = replica.fetchall("SELECT * FROM test_users")
        assert len(rows) == 1
        assert rows[0]["name"] == "Alice"
        replica.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_database_replica_pool():
    """Test replica pool with round-robin distribution."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Create and populate database
        db = Database(db_path, enable_wal=True)
        TestUser.create_table(db)
        for i in range(5):
            TestUser.create(db, name=f"User{i}", email=f"user{i}@example.com")
        db.close()
        
        # Test pool
        pool = DatabaseReplicaPool(db_path, replica_count=3)
        
        # Get replicas in round-robin
        replicas_used = []
        for _ in range(6):
            replica = pool.get_replica()
            replicas_used.append(id(replica))
        
        # Should cycle through replicas
        assert replicas_used[0] == replicas_used[3]  # Same replica at position 0 and 3
        assert replicas_used[1] == replicas_used[4]
        assert replicas_used[2] == replicas_used[5]
        
        # Test query on pool
        count = pool.scalar("SELECT COUNT(*) FROM test_users")
        assert count == 5
        
        pool.close_all()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_database_with_wal_enabled():
    """Test Database with WAL enabled."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = Database(db_path, enable_wal=True, enable_replicas=False)
        
        # Check WAL is enabled
        journal_mode = db.get_pragma("journal_mode")
        assert journal_mode.upper() == "WAL"
        
        TestUser.create_table(db)
        user = TestUser.create(db, name="Bob", email="bob@example.com")
        
        # Verify data was written
        found = TestUser.get(db, id=user.id)
        assert found.name == "Bob"
        
        db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_database_with_replicas():
    """Test Database with replicas enabled."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Create with replicas
        db = Database(db_path, enable_wal=True, enable_replicas=True, replica_count=2)
        
        TestUser.create_table(db)
        user1 = TestUser.create(db, name="Charlie", email="charlie@example.com")
        user2 = TestUser.create(db, name="Diana", email="diana@example.com")
        
        # Read from main connection
        all_users = TestUser.objects(db).all()
        assert len(all_users) == 2

        replica_calls = {"fetchall": 0, "scalar": 0}
        original_fetchall = db.read_replica_fetchall
        original_scalar = db.read_replica_scalar

        def tracked_fetchall(sql, params=None):
            replica_calls["fetchall"] += 1
            return original_fetchall(sql, params)

        def tracked_scalar(sql, params=None, default=None):
            replica_calls["scalar"] += 1
            return original_scalar(sql, params, default)

        db.read_replica_fetchall = tracked_fetchall
        db.read_replica_scalar = tracked_scalar

        replica_rows = TestUser.objects(db).using("replica").order_by("-id").values("id", "name", "email")
        assert [row["name"] for row in replica_rows] == ["Diana", "Charlie"]
        assert replica_calls["fetchall"] == 1

        replica_count = TestUser.objects(db).using("replica").count()
        assert replica_count == 2
        assert replica_calls["scalar"] == 1

        try:
            TestUser.objects(db).using("replica").filter(id=user1.id).update(name="Eve")
            raise AssertionError("Replica querysets must be read-only")
        except RuntimeError:
            pass

        # Read from replica
        count = db.read_replica_scalar("SELECT COUNT(*) FROM test_users")
        assert count == 2
        
        rows = db.read_replica_fetchall("SELECT * FROM test_users")
        assert len(rows) == 2
        
        db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_database_checkpoint():
    """Test WAL checkpoint functionality."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = Database(db_path, enable_wal=True)
        TestUser.create_table(db)
        
        # Insert some data
        for i in range(100):
            TestUser.create(db, name=f"User{i}", email=f"user{i}@example.com")
        
        # Checkpoint should not raise
        db.checkpoint("RESTART")
        
        # Verify data still there
        count = db.scalar("SELECT COUNT(*) FROM test_users")
        assert count == 100
        
        db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_database_optimize():
    """Test database optimization."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        db = Database(db_path)
        TestUser.create_table(db)
        
        # Insert and delete data
        user_ids = []
        for i in range(50):
            user = TestUser.create(db, name=f"User{i}", email=f"user{i}@example.com")
            user_ids.append(user.id)
        
        # Delete half
        for uid in user_ids[:25]:
            TestUser.objects(db).filter(id=uid).delete()
        
        # Optimize should not raise
        db.optimize()
        
        # Verify remaining data
        count = db.scalar("SELECT COUNT(*) FROM test_users")
        assert count == 25
        
        db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


def test_concurrent_reads_with_replicas():
    """Test concurrent reads using replicas."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Setup
        db = Database(db_path, enable_wal=True, enable_replicas=True, replica_count=3)
        TestUser.create_table(db)
        
        for i in range(20):
            TestUser.create(db, name=f"User{i}", email=f"user{i}@example.com")
        
        for _ in range(5):
            results = []
            lock = threading.Lock()

            def reader_thread():
                """Thread that reads from replica."""
                count = db.read_replica_scalar("SELECT COUNT(*) FROM test_users")
                with lock:
                    results.append(count)

            # Spawn multiple reader threads
            threads = [threading.Thread(target=reader_thread) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All should have read 20 users
            assert len(results) == 10
            assert all(r == 20 for r in results)
        
        db.close()
    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)
        wal_path = f"{db_path}-wal"
        if os.path.exists(wal_path):
            os.unlink(wal_path)
        shm_path = f"{db_path}-shm"
        if os.path.exists(shm_path):
            os.unlink(shm_path)


if __name__ == "__main__":
    # Run tests
    test_sqlite3_optimization_config()
    print("✓ test_sqlite3_optimization_config passed")
    
    test_database_replica_read_only()
    print("✓ test_database_replica_read_only passed")
    
    test_database_replica_pool()
    print("✓ test_database_replica_pool passed")
    
    test_database_with_wal_enabled()
    print("✓ test_database_with_wal_enabled passed")
    
    test_database_with_replicas()
    print("✓ test_database_with_replicas passed")
    
    test_database_checkpoint()
    print("✓ test_database_checkpoint passed")
    
    test_database_optimize()
    print("✓ test_database_optimize passed")
    
    test_concurrent_reads_with_replicas()
    print("✓ test_concurrent_reads_with_replicas passed")
    
    print("\nAll tests passed! ✓")
