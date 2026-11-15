import json
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from unittest.mock import patch

import wsbuilder.tasks as tasks_module
from wsbuilder import (
    TASK_CANCELLED,
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_REJECTED,
    TASK_RUNNING,
    App,
    Request,
    TaskCancelledError,
    TaskManager,
    TaskRejectedError,
)


def _req(path, client=("127.0.0.1", 1234)):
    return Request(
        method="GET",
        path=path,
        query_string="",
        headers={},
        body=b"",
        client=client,
        tls={},
    )


class TestTasks(unittest.TestCase):
    def setUp(self):
        self.app = App()

    def tearDown(self):
        self.app.close()

    def test_view_can_spawn_task_through_request_app(self):
        started = threading.Event()
        release = threading.Event()

        @self.app.view("/launch")
        def launch(request):
            self.assertIs(request.app, self.app)

            def worker():
                started.set()
                release.wait(1.0)
                return {"ok": True}

            task = request.app.tasks.spawn(worker, name="launch-task", group="jobs", request=request)
            return json.dumps({"task_id": task.id})

        response = self.app.dispatch(_req("/launch"))
        payload = json.loads(response.body.decode("utf-8"))
        task_id = payload["task_id"]

        self.assertTrue(started.wait(1.0))
        task = self.app.tasks.get(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task.status, TASK_RUNNING)

        release.set()
        self.assertTrue(task.wait(1.0))
        self.assertEqual(task.status, TASK_COMPLETED)
        self.assertEqual(task.result, {"ok": True})

        snapshot = self.app.tasks.snapshot()
        self.assertEqual(snapshot["counts"][TASK_COMPLETED], 1)
        self.assertEqual(snapshot["total"], 1)

    def test_task_manager_limits_concurrency_with_semaphore(self):
        manager = TaskManager(app=self.app, max_concurrent=1)
        release = threading.Event()
        first_started = threading.Event()
        second_started = threading.Event()

        def worker(label, marker):
            marker.set()
            release.wait(1.0)
            return label

        first = manager.spawn(worker, "first", first_started, name="first")
        second = manager.spawn(worker, "second", second_started, name="second")

        self.assertTrue(first_started.wait(1.0))
        time.sleep(0.05)
        self.assertFalse(second_started.is_set())
        self.assertEqual(second.status, "pending")

        release.set()
        self.assertEqual(first.get(1.0), "first")
        self.assertEqual(second.get(1.0), "second")
        self.assertEqual(first.status, TASK_COMPLETED)
        self.assertEqual(second.status, TASK_COMPLETED)

    def test_task_cancellation_is_cooperative(self):
        started = threading.Event()
        release = threading.Event()

        def worker(task):
            started.set()
            while not task.cancel_event.wait(0.01):
                if release.is_set():
                    break
            raise TaskCancelledError("stopped")

        task = self.app.tasks.spawn(worker, pass_handle=True, name="cancelled")
        self.assertTrue(started.wait(1.0))
        self.assertTrue(task.cancel())
        self.assertTrue(task.wait(1.0))
        self.assertEqual(task.status, TASK_CANCELLED)
        with self.assertRaises(TaskCancelledError):
            task.get(0.1)

    def test_context_uses_manager_app_and_releases_execution_inputs(self):
        manager = TaskManager(app=self.app)
        request = _req("/context")
        task = manager.spawn(
            lambda value: value,
            "done",
            request=request,
            kwargs={},
        )

        self.assertEqual(task.get(1.0), "done")
        self.assertIs(task.context.app, self.app)
        self.assertIsNone(task.context.request)
        self.assertIsNone(task.request)
        self.assertIsNone(task.target)
        self.assertEqual(task.args, ())
        self.assertEqual(task.kwargs, {})
        self.assertTrue(task.snapshot()["has_result"])
        manager.close()

    def test_cancel_completed_task_is_a_noop(self):
        task = self.app.tasks.spawn(lambda: "done")
        self.assertEqual(task.get(1.0), "done")

        self.assertFalse(task.cancel())
        self.assertFalse(self.app.tasks.cancel(task.id))
        self.assertEqual(task.status, TASK_COMPLETED)
        self.assertFalse(task.cancelled)

    def test_spawn_and_close_are_atomic(self):
        manager = TaskManager(app=self.app)
        constructor_entered = threading.Event()
        release_constructor = threading.Event()
        close_started = threading.Event()
        close_finished = threading.Event()
        spawned = []
        errors = []
        original_handle = tasks_module.TaskHandle

        class BlockingTaskHandle(original_handle):
            def __init__(self, *args, **kwargs):
                constructor_entered.set()
                release_constructor.wait(1.0)
                super().__init__(*args, **kwargs)

        def spawn_task():
            try:
                spawned.append(manager.spawn(lambda: "ok"))
            except Exception as exc:
                errors.append(exc)

        def close_manager():
            close_started.set()
            manager.close(wait=True, timeout=1.0)
            close_finished.set()

        with patch.object(tasks_module, "TaskHandle", BlockingTaskHandle):
            spawn_thread = threading.Thread(target=spawn_task)
            close_thread = threading.Thread(target=close_manager)
            spawn_thread.start()
            self.assertTrue(constructor_entered.wait(1.0))
            close_thread.start()
            self.assertTrue(close_started.wait(1.0))
            self.assertFalse(close_finished.wait(0.05))
            release_constructor.set()
            spawn_thread.join(1.0)
            close_thread.join(1.0)

        self.assertFalse(spawn_thread.is_alive())
        self.assertFalse(close_thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(spawned), 1)
        self.assertTrue(spawned[0].finished.is_set())
        self.assertTrue(manager.snapshot()["closed"])

    def test_task_can_close_its_manager_without_waiting_for_itself(self):
        manager = TaskManager(app=self.app)
        close_returned = threading.Event()

        def close_from_task():
            manager.close(wait=True)
            close_returned.set()

        task = manager.spawn(close_from_task)
        self.assertTrue(task.wait(1.0))
        self.assertTrue(close_returned.is_set())
        self.assertEqual(task.status, TASK_CANCELLED)


if __name__ == "__main__":
    unittest.main()


class TestTaskRetention(unittest.TestCase):
    def test_finished_tasks_are_evicted_once_the_history_is_full(self):
        manager = TaskManager(max_finished_tasks=5)
        self.addCleanup(manager.close)

        handles = [manager.spawn(lambda value=index: value) for index in range(40)]
        for handle in handles:
            handle.wait(timeout=5.0)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["max_finished_tasks"], 5)
        self.assertEqual(snapshot["total"], 5)
        self.assertEqual(snapshot["finished_evicted_total"], 35)
        self.assertIsNone(manager.get(handles[0].id))
        self.assertIsNotNone(manager.get(handles[-1].id))

    def test_evicted_tasks_are_dropped_oldest_first(self):
        manager = TaskManager(max_finished_tasks=2)
        self.addCleanup(manager.close)

        finished = []
        for index in range(4):
            handle = manager.spawn(lambda value=index: value)
            handle.wait(timeout=5.0)
            finished.append(handle)

        retained = {row["id"] for row in manager.list()}
        self.assertEqual(retained, {finished[2].id, finished[3].id})

    def test_history_can_be_disabled_to_keep_every_task(self):
        manager = TaskManager(max_finished_tasks=0)
        self.addCleanup(manager.close)

        handles = [manager.spawn(lambda value=index: value) for index in range(20)]
        for handle in handles:
            handle.wait(timeout=5.0)

        self.assertEqual(manager.snapshot()["total"], 20)
        self.assertEqual(manager.snapshot()["finished_evicted_total"], 0)

    def test_group_index_does_not_retain_evicted_tasks(self):
        manager = TaskManager(max_finished_tasks=1)
        self.addCleanup(manager.close)

        for index in range(6):
            manager.spawn(lambda value=index: value, group="batch").wait(timeout=5.0)

        self.assertEqual(manager._by_group, {})
        self.assertEqual(len(manager._tasks), 1)


class TestBoundedTasks(unittest.TestCase):
    def test_pending_tasks_use_no_threads_and_full_capacity_is_rejected(self):
        manager = TaskManager(max_concurrent=1, max_pending_tasks=2)
        release = threading.Event()
        self.addCleanup(manager.close)
        self.addCleanup(release.set)
        first = manager.spawn(lambda: release.wait(2.0))
        self.assertTrue(first.started.wait(1.0))
        queued = [manager.spawn(lambda: "queued", group="waiting") for _ in range(2)]
        with self.assertRaises(TaskRejectedError):
            manager.spawn(lambda: "overflow")
        self.assertEqual(manager.snapshot()["max_pending_tasks"], 2)
        self.assertEqual(manager.snapshot()["counts"]["pending"], 2)
        self.assertTrue(all(task._thread.ident is None for task in queued))
        self.assertTrue(queued[0].cancel())
        self.assertTrue(queued[0].finished.is_set())
        self.assertIsNone(queued[0].target)
        self.assertNotIn(queued[0].id, manager._by_group.get("waiting", set()))
        with self.assertRaises(TaskCancelledError):
            queued[0].get()
        replacement = manager.spawn(lambda: "replacement")
        release.set()
        self.assertTrue(first.wait(1.0))
        self.assertEqual(queued[1].get(1.0), "queued")
        self.assertEqual(replacement.get(1.0), "replacement")

    def test_thread_limit_holds_across_short_tasks_and_preserves_daemon(self):
        manager = TaskManager(max_concurrent=2)
        self.addCleanup(manager.close)
        observed = []
        lock = threading.Lock()

        def worker():
            live = sum(
                thread.name.startswith("wsbuilder-task-bound-check-")
                for thread in threading.enumerate()
            )
            with lock:
                observed.append((live, threading.current_thread().daemon))

        tasks = [
            manager.spawn(worker, name="bound-check", daemon=False)
            for _ in range(40)
        ]
        for task in tasks:
            self.assertTrue(task.wait(2.0))
        self.assertEqual(len(observed), 40)
        self.assertLessEqual(max(count for count, _ in observed), 2)
        self.assertFalse(any(daemon for _, daemon in observed))

    def test_pending_timeout_expires_while_running_task_is_blocked(self):
        manager = TaskManager(max_concurrent=1, max_pending_tasks=1)
        release = threading.Event()
        self.addCleanup(manager.close)
        self.addCleanup(release.set)
        first = manager.spawn(lambda: release.wait(2.0))
        self.assertTrue(first.started.wait(1.0))
        pending = manager.spawn(lambda: "never", timeout_seconds=0.02)
        self.assertTrue(pending.wait(1.0))
        self.assertEqual(pending.status, TASK_REJECTED)
        self.assertIsNone(pending._thread.ident)
        self.assertIsNone(pending.target)
        with self.assertRaises(TaskRejectedError):
            pending.get()
        replacement = manager.spawn(lambda: "ok")
        release.set()
        self.assertEqual(replacement.get(1.0), "ok")

    def test_close_without_wait_finishes_pending_handles_and_stops_helpers(self):
        manager = TaskManager(max_concurrent=1)
        release = threading.Event()
        self.addCleanup(manager.close)
        self.addCleanup(release.set)
        running = manager.spawn(lambda: release.wait(2.0))
        self.assertTrue(running.started.wait(1.0))
        pending = manager.spawn(lambda: "never", daemon=False, group="waiting")
        manager.close(wait=False)
        self.assertTrue(pending.finished.is_set())
        self.assertEqual(pending.status, TASK_CANCELLED)
        self.assertIsNone(pending.target)
        self.assertNotIn("waiting", manager._by_group)
        release.set()
        manager.close(wait=True, timeout=1.0)
        self.assertFalse(manager._dispatcher.is_alive())
        self.assertFalse(manager._non_daemon_guard.is_alive())

    def test_workers_can_close_concurrently_without_wait_cycles(self):
        for concurrency in (0, 2):
            with self.subTest(max_concurrent=concurrency):
                manager = TaskManager(max_concurrent=concurrency)
                barrier = threading.Barrier(2, timeout=1.0)
                returned = []

                def worker():
                    barrier.wait()
                    manager.close(wait=True)
                    returned.append(True)

                handles = [manager.spawn(worker) for _ in range(2)]
                for handle in handles:
                    self.assertTrue(handle.wait(1.0))
                    self.assertEqual(handle.status, TASK_CANCELLED)
                self.assertEqual(len(returned), 2)
                manager.close(timeout=1.0)

    def test_base_exception_finishes_the_handle_and_releases_capacity(self):
        manager = TaskManager(max_concurrent=1, max_pending_tasks=0)
        self.addCleanup(manager.close)

        def stop():
            raise SystemExit(3)

        failed = manager.spawn(stop)
        self.assertTrue(failed.wait(1.0))
        self.assertEqual(failed.status, TASK_FAILED)
        with self.assertRaises(SystemExit):
            failed.get()
        self.assertEqual(manager.spawn(lambda: "next").get(1.0), "next")

    def test_worker_start_failure_finalizes_handle_and_releases_capacity(self):
        manager = TaskManager(max_concurrent=1, max_pending_tasks=0)
        self.addCleanup(manager.close)
        original_start = threading.Thread.start

        def start(thread):
            if thread.name.startswith("wsbuilder-task-start-fails-"):
                raise RuntimeError("worker could not start")
            return original_start(thread)

        with patch.object(threading.Thread, "start", start):
            failed = manager.spawn(lambda: "never", name="start-fails", daemon=False)
            self.assertTrue(failed.wait(1.0))
            self.assertEqual(failed.status, TASK_FAILED)
            self.assertIsNone(failed.target)
            with self.assertRaisesRegex(RuntimeError, "worker could not start"):
                failed.get()
            self.assertEqual(manager.spawn(lambda: "next").get(1.0), "next")

    def test_non_daemon_pending_task_keeps_the_process_alive(self):
        script = textwrap.dedent("""
            import threading
            import time
            from wsbuilder import TaskManager

            manager = TaskManager(max_concurrent=1)
            started = threading.Event()
            def daemon_job():
                started.set()
                time.sleep(0.15)
            manager.spawn(daemon_job)
            assert started.wait(1.0)
            manager.spawn(lambda: print('pending-finished'), daemon=False)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=3.0,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "pending-finished")

    def test_cancelled_or_expired_non_daemon_pending_task_does_not_hold_process(self):
        for cancel in (True, False):
            with self.subTest(cancel=cancel):
                script = textwrap.dedent(f"""
                    import threading
                    import time
                    from wsbuilder import TaskManager

                    manager = TaskManager(max_concurrent=1)
                    started = threading.Event()
                    def daemon_job():
                        started.set()
                        time.sleep(10.0)
                    manager.spawn(daemon_job)
                    assert started.wait(1.0)
                    pending = manager.spawn(lambda: print('unexpected'), daemon=False,
                                            timeout_seconds=0.02 if not {cancel!r} else None)
                    if {cancel!r}:
                        pending.cancel()
                    assert pending.wait(1.0)
                """)
                result = subprocess.run(
                    [sys.executable, "-c", script], capture_output=True, text=True, timeout=3.0,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
