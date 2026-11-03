"""Bounded initial-call window; the consumer alone validates and accepts chunks."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ChunkFailure(Exception):
    def __init__(self, index, cause):
        self.index, self.cause = index, cause
        super().__init__(str(cause))


@dataclass
class ChunkTask:
    index: int
    offset: int
    text: str
    payload: dict
    prompt: str
    folder: Path
    deadline: float
    output: Any = None
    done: bool = False
    wait: object = None


class ChunkScheduler:
    def __init__(self, tasks, capacity, prepare, call, begin_call, timing):
        self.tasks = iter(tasks)
        self.capacity, self.prepare, self.call = capacity, prepare, call
        self.begin_call, self.timing = begin_call, timing
        self.condition = threading.Condition(threading.RLock())
        self.executor = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix='verinote-chunk')
        self.pending = []
        self.failure = None
        self.closed = False

    def fail(self, index, exc):
        with self.condition:
            if self.failure is None:
                self.failure = exc if isinstance(exc, ChunkFailure) else ChunkFailure(index, exc)
            self.condition.notify_all()
            return self.failure

    def check(self, task=None):
        if self.failure is not None: raise self.failure
        if self.closed: raise RuntimeError('추출 스케줄러가 닫혔습니다.')
        if task is not None and time.monotonic() >= task.deadline:
            raise self.fail(task.index, TimeoutError('청크 실행 시간 제한 초과'))

    def admit(self, task):
        with self.condition:
            self.check(task)
            self.begin_call()

    def _run(self, task):
        try:
            output = self.call(task, task.prompt, task.folder)
            with self.condition:
                task.output = output
                task.wait = self.timing.section('order_wait', task.index)
                task.wait.__enter__()
                task.done = True
                self.condition.notify_all()
        except Exception as exc:
            self.fail(task.index, exc)

    def __iter__(self):
        return self

    def __next__(self):
        with self.condition:
            self.check()
            while len(self.pending) < self.capacity:
                raw = next(self.tasks, None)
                if raw is None: break
                task = self.prepare(raw)
                self.timing.begin_chunk(task.index)
                self.pending.append(task)
                try:
                    self.admit(task)
                    self.executor.submit(self._run, task)
                except Exception as exc:
                    raise self.fail(task.index, exc) from exc
            if not self.pending: raise StopIteration
            task = self.pending[0]
            while not task.done:
                self.check(task)
                self.condition.wait(max(0, task.deadline - time.monotonic()))
            self.check(task)
            if task.wait is not None:
                task.wait.__exit__(None, None, None)
                task.wait = None
            return task

    @contextmanager
    def accepting(self, task):
        # A later worker cannot report failure between the final check and acceptance.
        with self.condition:
            self.check(task)
            yield
            self.check(task)
            if not self.pending or self.pending[0] is not task:
                raise RuntimeError('청크 수락 순서 오류')
            self.pending.pop(0)

    def repair(self, task, prompt, folder):
        self.admit(task)
        try:
            return self.call(task, prompt, folder)
        except Exception as exc:
            raise self.fail(task.index, exc) from exc

    def __enter__(self): return self

    def __exit__(self, kind, exc, tb):
        if exc is not None: self.fail(self.pending[0].index if self.pending else 0, exc)
        with self.condition:
            self.closed = True
        self.executor.shutdown(wait=True)
        with self.condition:
            for task in self.pending:
                if task.wait is not None: task.wait.__exit__(None, None, None)
                self.timing.end_chunk('error', index=task.index)
            self.pending.clear()
            if self.failure is not None: raise self.failure
