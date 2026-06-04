import threading
import time
import uuid
from dataclasses import dataclass


TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_REJECTED = "rejected"


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

        self.context = TaskContext(
            app=getattr(request, "app", None),
            request=request,
            group=self.group,
            name=self.name,
            metadata=self.metadata,
        )

        self._state_lock = threading.RLock()
        self._started = threading.Event()
        self._finished = threading.Event()
        self._cancel_requested = threading.Event()
        self._ready_to_run = threading.Event()
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
        self._thread.start()
        return self

    def cancel(self):
        self._cancel_requested.set()
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

    def _run(self):
        gate = self.manager._task_slot
        if gate is not None:
            deadline = None if self.timeout_seconds is None else (time.monotonic() + self.timeout_seconds)
            acquired = False
            while not acquired:
                if self.manager._closed.is_set() or self._cancel_requested.is_set():
                    self._set_state(status=TASK_CANCELLED, finished_at=time.time(), exception=TaskCancelledError("Task cancelled before start"))
                    self._finished.set()
                    self.manager._finalize_task(self)
                    return
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    self._set_state(status=TASK_REJECTED, finished_at=time.time(), exception=TaskRejectedError("Task capacity reached"))
                    self._finished.set()
                    self.manager._finalize_task(self)
                    return
                acquired = gate.acquire(timeout=0.1 if remaining is None else min(0.1, remaining))

        try:
            if self.manager._closed.is_set():
                raise TaskClosedError("Task manager is closed")
            if self._cancel_requested.is_set():
                raise TaskCancelledError("Task cancelled before start")

            self._set_state(status=TASK_RUNNING, started_at=time.time())
            self._started.set()

            if self.pass_handle:
                result = self.target(self, *self.args, **self.kwargs)
            else:
                result = self.target(*self.args, **self.kwargs)

            if self._cancel_requested.is_set():
                raise TaskCancelledError("Task cancelled")

            self._set_state(status=TASK_COMPLETED, result=result)
        except (TaskClosedError, TaskRejectedError) as exc:
            self._set_state(status=TASK_REJECTED, exception=exc)
        except TaskCancelledError as exc:
            self._set_state(status=TASK_CANCELLED, exception=exc)
        except Exception as exc:
            self._set_state(status=TASK_FAILED, exception=exc)
        finally:
            self._set_state(finished_at=time.time())
            self._finished.set()
            if gate is not None:
                gate.release()
            self.manager._finalize_task(self)


class TaskManager:
    def __init__(self, app=None, max_concurrent=0):
        self.app = app
        self.max_concurrent = max(0, int(max_concurrent or 0))
        self._task_slot = threading.BoundedSemaphore(self.max_concurrent) if self.max_concurrent > 0 else None
        self._lock = threading.RLock()
        self._tasks = {}
        self._by_group = {}
        self._closed = threading.Event()

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
        with self._lock:
            self._tasks[task.id] = task
            if task.group:
                self._by_group.setdefault(task.group, set()).add(task.id)
        task.start()
        return task

    submit = spawn

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
        task.cancel()
        return True

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

    def close(self, *, wait=True, timeout=None):
        self._closed.set()
        self.cancel_all()
        if wait:
            deadline = None if timeout is None else (time.time() + max(0.0, float(timeout)))
            for task in list(self._tasks.values()):
                if deadline is None:
                    task.join()
                    continue
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                task.join(timeout=remaining)
