import json
import threading
import time
import unittest
from unittest.mock import patch

import wsbuilder.tasks as tasks_module
from wsbuilder import App, Request, TaskCancelledError, TaskManager, TASK_CANCELLED, TASK_COMPLETED, TASK_RUNNING


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
