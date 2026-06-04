import time
import unittest

from wsbuilder import App
from wsbuilder.cache import SQLiteMemoryCache, install_cache


class TestSQLiteMemoryCache(unittest.TestCase):
    def setUp(self):
        self.cache = SQLiteMemoryCache(cleanup_interval_seconds=0)

    def tearDown(self):
        self.cache.close()

    def test_set_get_ttl_and_pop(self):
        self.assertTrue(self.cache.set("user:1", {"name": "Alice"}, ttl=0.05))
        self.assertEqual(self.cache.get("user:1"), {"name": "Alice"})
        self.assertGreater(self.cache.ttl("user:1"), 0)

        time.sleep(0.08)
        self.assertIsNone(self.cache.get("user:1"))
        self.assertEqual(self.cache.ttl("user:1"), -2)

        self.cache.set("k", "v")
        self.assertEqual(self.cache.pop("k"), "v")
        self.assertIsNone(self.cache.get("k"))

    def test_add_replace_and_delete_many(self):
        self.assertTrue(self.cache.add("k1", "v1"))
        self.assertFalse(self.cache.add("k1", "v2"))
        self.assertEqual(self.cache.get("k1"), "v1")

        self.assertTrue(self.cache.replace("k1", "v3"))
        self.assertEqual(self.cache.get("k1"), "v3")
        self.assertFalse(self.cache.replace("missing", "x"))

        self.cache.set("k2", "v2")
        removed = self.cache.delete_many(["k1", "k2", "k3"])
        self.assertEqual(removed, 2)
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNone(self.cache.get("k2"))

    def test_incr_decr_numeric(self):
        self.assertEqual(self.cache.incr("counter"), 1)
        self.assertEqual(self.cache.incr("counter", amount=4), 5)
        self.assertEqual(self.cache.decr("counter", amount=2), 3)

        self.cache.set("not-number", "hello")
        with self.assertRaises(TypeError):
            self.cache.incr("not-number")

    def test_tags_and_invalidation(self):
        self.cache.set("k1", {"v": 1}, tags=["team:a", "env:dev"])
        self.cache.set("k2", {"v": 2}, tags=["team:a"])
        self.cache.set("k3", {"v": 3}, tags=["team:b"])

        self.assertEqual(set(self.cache.get_tags("k1")), {"env:dev", "team:a"})

        removed = self.cache.invalidate_tag("team:a")
        self.assertEqual(removed, 2)
        self.assertIsNone(self.cache.get("k1"))
        self.assertIsNone(self.cache.get("k2"))
        self.assertEqual(self.cache.get("k3"), {"v": 3})

    def test_namespaces_and_metrics_snapshot(self):
        self.cache.set("token", "n1", namespace="ns-1")
        self.cache.set("token", "n2", namespace="ns-2")
        self.assertEqual(self.cache.get("token", namespace="ns-1"), "n1")
        self.assertEqual(self.cache.get("token", namespace="ns-2"), "n2")
        self.assertEqual(self.cache.count(namespace="ns-1"), 1)
        self.assertEqual(self.cache.count(namespace="ns-2"), 1)

        metrics = self.cache.metrics_snapshot()
        self.assertIn("cache", metrics)
        self.assertEqual(metrics["cache"]["storage"]["entries_total"], 2)
        self.assertGreaterEqual(metrics["cache"]["counters"]["gets"], 2)

    def test_max_entries_eviction(self):
        cache = SQLiteMemoryCache(max_entries=2, cleanup_interval_seconds=0)
        try:
            cache.set("a", 1)
            time.sleep(0.01)
            cache.set("b", 2)
            time.sleep(0.01)
            cache.set("c", 3)
            self.assertEqual(cache.count(), 2)
            self.assertIsNone(cache.get("a"))
            self.assertEqual(cache.get("b"), 2)
            self.assertEqual(cache.get("c"), 3)
        finally:
            cache.close()

    def test_install_cache_on_app(self):
        app = App()
        cache = install_cache(app)
        try:
            self.assertIs(app.cache, cache)
            self.assertTrue(cache.set("hello", "world"))
            self.assertEqual(cache.get("hello"), "world")
        finally:
            cache.close()


if __name__ == "__main__":
    unittest.main()
