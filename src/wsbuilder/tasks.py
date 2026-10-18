import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_REJECTED = "rejected"
_TERMINAL_STATUSES = {
    TASK_COMPLETED,
    TASK_FAILED,
    TASK_CANCELLED,
    TASK_REJECTED,
}

# How many finished tasks stay queryable through the manager. Without a bound a
# long-running server that spawns a task per request keeps every handle (and its
# result) alive for the whole process lifetime.
DEFAULT_MAX_FINISHED_TASKS = 512
DEFAULT_MAX_PENDING_TASKS = 512


class TaskError(Exception):
    pass


class TaskClosedError(TaskError):
    pass


class TaskCancelledError(TaskError):
    pass


class TaskRejectedError(TaskError):
    pass


@dataclass(slots=True)
class TaskContext:
    app: object | None = None
    request: object | None = None
    group: str = ""
    name: str = ""
    metadata: dict | None = None


class TaskHandle:
    def __init__(
        self,
        manager,
        target,
        args=(),
        kwargs=None,
        *,
        name=None,
        group="",
        metadata=None,
        daemon=True,
        pass_handle=False,
        request=None,
        timeout_seconds=None,
    ):
        self.manager = manager
        self.target = target
        self.args = tuple(args or ())
        self.kwargs = dict(kwargs or {})
        self.name = str(name or getattr(target, "__name__", "task"))
        self.group = str(group or "")
        self.metadata = dict(metadata or {})
        self.daemon = bool(daemon)
        self.pass_handle = bool(pass_handle)
        self.request = request
        self.timeout_seconds = None if timeout_seconds is None else max(0.0, float(timeout_seconds))
        self.id = str(uuid.uuid4())

        if request is not None:
            self.metadata.setdefault("request_method", getattr(request, "method", None))
            self.metadata.setdefault("request_path", getattr(request, "path", None))
            self.metadata.setdefault("request_client", getattr(request, "client", None))

        request_app = getattr(request, "app", None)
        self.context = TaskContext(
            app=request_app if request_app is not None else getattr(manager, "app", None),
            request=request,
            group=self.group,
            name=self.name,
            metadata=self.metadata,
        )

        self._state_lock = threading.RLock()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._cancel_requested = threading.Event()
        self._submitted = False
        self._deadline = None
        self._started_at = None
        self._finished_at = None
        self._result = None
        self._exception = None
        self._status = TASK_PENDING
        self._thread = threading.Thread(target=self._run, name=f"wsbuilder-task-{self.name}-{self.id}", daemon=self.daemon)

    @property
    def status(self):
        with self._state_lock:
            return self._status

    @property
    def started_at(self):
        with self._state_lock:
            return self._started_at

    @property
    def finished_at(self):
        with self._state_lock:
            return self._finished_at

    @property
    def result(self):
        with self._state_lock:
            return self._result

    @property
    def exception(self):
        with self._state_lock:
            return self._exception

    @property
    def started(self):
        return self._started

    @property
    def finished(self):
        return self._finished

    @property
    def cancel_event(self):
        return self._cancel_requested

    @property
    def cancelled(self):
        return self._cancel_requested.is_set()

    @property
    def running(self):
        return self.status == TASK_RUNNING

    def start(self):
        with self._state_lock:
            if self._submitted:
                raise RuntimeError("Task has already been submitted")
            self._submitted = True
        try:
            if self.manager.max_concurrent:
                self.manager._enqueue_task(self)
            else:
                self._thread.start()
        except BaseException:
            with self._state_lock:
                self._submitted = False
            raise
        return self

    def cancel(self):
        with self._state_lock:
            if self._status in _TERMINAL_STATUSES:
                return False
            self._cancel_requested.set()
        self.manager._cancel_pending_task(self)
        return True

    def wait(self, timeout=None):
        return self._finished.wait(timeout)

    def join(self, timeout=None):
        self.wait(timeout=timeout)
        return self

    def get(self, timeout=None):
        ok = self.wait(timeout=timeout)
        if not ok:
            raise TimeoutError(f"Task {self.id} did not finish within timeout")
        with self._state_lock:
            if self._status == TASK_CANCELLED:
                raise TaskCancelledError(f"Task {self.id} was cancelled")
            if self._exception is not None:
                raise self._exception
            return self._result

    def snapshot(self):
        with self._state_lock:
            return {
                "id": self.id,
                "name": self.name,
                "group": self.group,
                "status": self._status,
                "daemon": self.daemon,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "has_result": self._result is not None,
                "has_exception": self._exception is not None,
                "cancelled": self._cancel_requested.is_set(),
                "metadata": dict(self.metadata),
            }

    def _set_state(self, status=None, started_at=None, finished_at=None, result=None, exception=None):
        with self._state_lock:
            if status is not None:
                self._status = status
            if started_at is not None:
                self._started_at = started_at
            if finished_at is not None:
                self._finished_at = finished_at
            if result is not None or self._result is None:
                self._result = result
            if exception is not None:
                self._exception = exception

    def _release_execution_references(self):
        with self._state_lock:
            self.target = None
            self.args = ()
            self.kwargs = {}
            self.request = None
            self.context.request = None

    def _finish_and_finalize(self):
        # Register the task with the manager before waking waiters, so anything
        # that inspects the manager after ``wait()`` sees settled bookkeeping.
        try:
            self.manager._finalize_task(self)
        finally:
            self._finished.set()

    def _run(self):
        try:
            if self._cancel_requested.is_set():
                raise TaskCancelledError("Task cancelled before start")
            if self.manager._closed.is_set():
                raise TaskClosedError("Task manager is closed")

            self._set_state(status=TASK_RUNNING, started_at=time.time())
            self._started.set()

            target = self.target
            if self.pass_handle:
                result = target(self, *self.args, **self.kwargs)
            else:
                result = target(*self.args, **self.kwargs)

            with self._state_lock:
                if self._cancel_requested.is_set():
                    self._set_state(
                        status=TASK_CANCELLED,
                        exception=TaskCancelledError("Task cancelled"),
                    )
                else:
                    self._set_state(status=TASK_COMPLETED, result=result)
        except (TaskClosedError, TaskRejectedError) as exc:
            self._set_state(status=TASK_REJECTED, exception=exc)
        except TaskCancelledError as exc:
            self._set_state(status=TASK_CANCELLED, exception=exc)
        except BaseException as exc:
            with self._state_lock:
                if self._cancel_requested.is_set():
                    self._set_state(
                        status=TASK_CANCELLED,
                        exception=TaskCancelledError("Task cancelled"),
                    )
                else:
                    self._set_state(status=TASK_FAILED, exception=exc.with_traceback(None))
        finally:
            self._set_state(finished_at=time.time())
            self._release_execution_references()
            self._finish_and_finalize()


class TaskManager:
    def __init__(
        self,
        app=None,
        max_concurrent=0,
        max_finished_tasks=DEFAULT_MAX_FINISHED_TASKS,
        *,
        max_pending_tasks=DEFAULT_MAX_PENDING_TASKS,
    ):
        self.app = app
        self.max_concurrent = max(0, int(max_concurrent or 0))
        self.max_finished_tasks = max(0, int(max_finished_tasks or 0))
        self.max_pending_tasks = max(0, int(max_pending_tasks or 0))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._pending = deque()
        self._running = {}
        self._dispatcher = None
        self._dispatcher_exiting = False
        self._non_daemon_guard = None
        self._guard_exiting = False
        self._tasks = {}
        self._by_group = {}
        self._finished_order = deque()
        self._closed = threading.Event()
        self._evicted_finished_total = 0

    def spawn(
        self,
        target,
        *args,
        name=None,
        group="",
        metadata=None,
        kwargs=None,
        daemon=True,
        pass_handle=False,
        request=None,
        timeout_seconds=None,
    ):
        with self._lock:
            if self._closed.is_set():
                raise TaskClosedError("Task manager is closed")
            task = TaskHandle(
                self,
                target,
                args=args,
                kwargs=kwargs,
                name=name,
                group=group,
                metadata=metadata,
                daemon=daemon,
                pass_handle=pass_handle,
                request=request,
                timeout_seconds=timeout_seconds,
            )
            self._tasks[task.id] = task
            if task.group:
                self._by_group.setdefault(task.group, set()).add(task.id)
            try:
                task.start()
            except BaseException:
                self._tasks.pop(task.id, None)
                if task.group:
                    group_ids = self._by_group.get(task.group)
                    if group_ids is not None:
                        group_ids.discard(task.id)
                        if not group_ids:
                            self._by_group.pop(task.group, None)
                raise
        return task

    submit = spawn

    def _enqueue_task(self, task):
        with self._condition:
            if self._closed.is_set():
                raise TaskClosedError("Task manager is closed")
            self._reap_workers_locked()
            capacity = self.max_concurrent + self.max_pending_tasks
            if len(self._running) + len(self._pending) >= capacity:
                raise TaskRejectedError("Task capacity reached")
            task._deadline = (
                None if task.timeout_seconds is None
                else time.monotonic() + task.timeout_seconds
            )
            self._pending.append(task)
            try:
                if not task.daemon:
                    self._ensure_guard_locked()
                if self._dispatcher is not None and self._dispatcher_exiting:
                    self._dispatcher.join()
                    self._dispatcher = None
                if self._dispatcher is None:
                    self._dispatcher_exiting = False
                    self._dispatcher = threading.Thread(
                        target=self._dispatch,
                        name="wsbuilder-task-dispatcher",
                        daemon=True,
                    )
                    try:
                        self._dispatcher.start()
                    except BaseException:
                        self._dispatcher = None
                        raise
            except BaseException:
                self._pending.remove(task)
                self._condition.notify_all()
                raise
            self._condition.notify_all()

    def _ensure_guard_locked(self):
        if self._non_daemon_guard is not None and self._guard_exiting:
            self._non_daemon_guard.join()
            self._non_daemon_guard = None
        if self._non_daemon_guard is None:
            self._guard_exiting = False
            self._non_daemon_guard = threading.Thread(
                target=self._guard_non_daemon_tasks,
                name="wsbuilder-task-guard",
                daemon=False,
            )
            try:
                self._non_daemon_guard.start()
            except BaseException:
                self._non_daemon_guard = None
                raise

    def _guard_non_daemon_tasks(self):
        with self._condition:
            while any(not task.daemon for task in self._pending):
                self._condition.wait()
            # A submitter can join this thread under the manager lock after
            # this flag: returning from this block needs no further manager lock.
            self._guard_exiting = True

    def _reap_workers_locked(self):
        for task_id, task in list(self._running.items()):
            if task.finished.is_set():
                # The finished event is the last action of the task. Joining
                # here ensures the old thread exits before its slot is reused.
                task._thread.join()
                del self._running[task_id]

    def _finish_pending_locked(self, task, status, exception):
        task._set_state(status=status, finished_at=time.time(), exception=exception)
        task._release_execution_references()
        task._finish_and_finalize()

    def _cancel_pending_task(self, task):
        with self._condition:
            try:
                self._pending.remove(task)
            except ValueError:
                pass
            else:
                self._finish_pending_locked(
                    task, TASK_CANCELLED, TaskCancelledError("Task cancelled before start"),
                )
            self._condition.notify_all()

    def _dispatch(self):
        with self._condition:
            while True:
                self._reap_workers_locked()
                now = time.monotonic()
                for task in list(self._pending):
                    cancelled = self._closed.is_set() or task.cancelled
                    expired = task._deadline is not None and task._deadline <= now
                    if cancelled or expired:
                        self._pending.remove(task)
                        if cancelled:
                            task._cancel_requested.set()
                            self._finish_pending_locked(
                                task, TASK_CANCELLED, TaskCancelledError("Task cancelled before start"),
                            )
                        else:
                            self._finish_pending_locked(
                                task, TASK_REJECTED, TaskRejectedError("Task capacity wait timed out"),
                            )
                while self._pending and len(self._running) < self.max_concurrent:
                    task = self._pending.popleft()
                    self._running[task.id] = task
                    try:
                        task._thread.start()
                    except BaseException as exc:
                        del self._running[task.id]
                        self._finish_pending_locked(task, TASK_FAILED, exc.with_traceback(None))
                self._condition.notify_all()
                if not self._pending and not self._running:
                    self._dispatcher_exiting = True
                    return
                deadlines = [
                    task._deadline for task in self._pending
                    if task._deadline is not None
                ]
                timeout = 0.05
                if deadlines:
                    timeout = min(timeout, max(0.0, min(deadlines) - time.monotonic()))
                self._condition.wait(timeout)

    def get(self, task_id):
        with self._lock:
            return self._tasks.get(str(task_id))

    def list(self, *, group=None, status=None):
        with self._lock:
            tasks = list(self._tasks.values())
        rows = [task.snapshot() for task in tasks]
        if group is not None:
            rows = [row for row in rows if row["group"] == str(group)]
        if status is not None:
            rows = [row for row in rows if row["status"] == str(status)]
        rows.sort(key=lambda row: row["started_at"] or row["finished_at"] or 0.0, reverse=True)
        return rows

    def cancel(self, task_id):
        task = self.get(task_id)
        if not task:
            return False
        return task.cancel()

    def cancel_group(self, group):
        ids = []
        with self._lock:
            ids = list(self._by_group.get(str(group), set()))
        cancelled = 0
        for task_id in ids:
            if self.cancel(task_id):
                cancelled += 1
        return cancelled

    def cancel_all(self):
        with self._lock:
            ids = list(self._tasks.keys())
        cancelled = 0
        for task_id in ids:
            if self.cancel(task_id):
                cancelled += 1
        return cancelled

    def wait(self, task_id, timeout=None):
        task = self.get(task_id)
        if not task:
            return False
        return task.wait(timeout=timeout)

    def result(self, task_id, timeout=None):
        task = self.get(task_id)
        if not task:
            raise KeyError(task_id)
        return task.get(timeout=timeout)

    def snapshot(self):
        rows = self.list()
        counts = {
            TASK_PENDING: 0,
            TASK_RUNNING: 0,
            TASK_COMPLETED: 0,
            TASK_FAILED: 0,
            TASK_CANCELLED: 0,
            TASK_REJECTED: 0,
        }
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {
            "enabled": True,
            "closed": self._closed.is_set(),
            "max_concurrent": self.max_concurrent,
            "max_finished_tasks": self.max_finished_tasks,
            "max_pending_tasks": self.max_pending_tasks,
            "finished_evicted_total": self._evicted_finished_total,
            "total": len(rows),
            "counts": counts,
            "tasks": rows,
        }

    def _finalize_task(self, task):
        with self._lock:
            if task.group:
                group_ids = self._by_group.get(task.group)
                if group_ids is not None:
                    group_ids.discard(task.id)
                    if not group_ids:
                        self._by_group.pop(task.group, None)
            self._tasks[task.id] = task
            self._finished_order.append(task.id)
            self._evict_finished_locked()
            self._condition.notify_all()

    def _evict_finished_locked(self):
        if self.max_finished_tasks <= 0:
            return
        while len(self._finished_order) > self.max_finished_tasks:
            evicted_id = self._finished_order.popleft()
            if self._tasks.pop(evicted_id, None) is not None:
                self._evicted_finished_total += 1

    def close(self, *, wait=True, timeout=None):
        with self._lock:
            self._closed.set()
            tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        current_thread = threading.current_thread()
        # A worker waiting for another closing worker creates a cycle. Calls
        # from a task request cancellation and return without joining peers.
        called_from_task = any(task._thread is current_thread for task in tasks)
        if wait and not called_from_task:
            deadline = None if timeout is None else (time.monotonic() + max(0.0, float(timeout)))
            for task in tasks:
                if deadline is None:
                    task.join()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                task.join(timeout=remaining)
            for thread in (self._dispatcher, self._non_daemon_guard):
                if thread is None:
                    continue
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                thread.join(remaining)
