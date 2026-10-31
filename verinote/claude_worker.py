"""One reusable Claude stream process; every application call has fresh context."""
import atexit
import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import weakref
from contextlib import contextmanager
from uuid import uuid4

_MANAGERS: weakref.WeakSet['Manager'] = weakref.WeakSet()


def _shutdown():
    for manager in list(_MANAGERS):
        manager.close()


atexit.register(_shutdown)


def command(binary, model):
    return [binary, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--verbose', '--tools', '', '--strict-mcp-config', '--no-session-persistence',
            '--prompt-suggestions', 'false', '--no-chrome',
            '--setting-sources', '', '--settings', '{"disableAllHooks":true}',
            *(['--model', model] if model else [])]


class Worker:
    def __init__(self, binary, model, timing):
        self.worker_id = uuid4().hex
        self.extraction_identity: dict | None = None
        self.manager_closed: threading.Event | None = None
        self.key = (binary, model)
        self.session = None
        self.buffer = b''
        self.events: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=64)  # At most 256 KiB queued, including idle output.
        self.stopped = threading.Event()
        self.threads = []
        self.directory = tempfile.TemporaryDirectory(prefix='verinote-claude-')
        env = os.environ.copy()
        env.pop('OPENROUTER_API_KEY', None)
        options = {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)} if os.name == 'nt' else {'start_new_session': True}
        try:
            timing.mark('spawn_requested')
            self.proc = subprocess.Popen(command(binary, model), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         cwd=self.directory.name, env=env, bufsize=0, **options)
            timing.mark('spawn_returned')
        except BaseException:
            self.directory.cleanup()
            raise
        try:
            for stream, name in ((self.proc.stdout, 'out'), (self.proc.stderr, 'err')):
                thread = threading.Thread(target=self._read, args=(stream, name), daemon=True)
                thread.start()
                self.threads.append(thread)
        except BaseException:
            self.close()
            raise

    def _read(self, stream, name):
        try:
            while not self.stopped.is_set():
                data = os.read(stream.fileno(), 4096)
                while not self.stopped.is_set():
                    try:
                        self.events.put((name, data), timeout=.05)
                        break
                    except queue.Full:
                        pass
                if not data:
                    break
        except (OSError, ValueError):
            pass

    def close(self):
        from .agents import _kill_process_tree
        self.stopped.set()
        # Also reap descendants after the group leader has exited.
        _kill_process_tree(self.proc, timeout=2)
        for thread in self.threads:
            thread.join(timeout=1)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()
        self.directory.cleanup()


class Exchange:
    def __init__(self, worker, deadline, closed, maximum, stream, stderr, timing):
        self.worker, self.deadline, self.closed = worker, deadline, closed
        self.maximum, self.stream, self.stderr, self.timing = maximum, stream, stderr, timing
        self.count = 0
        self.inference = False

    def remaining(self):
        if self.closed.is_set():
            raise ValueError('Claude worker가 닫혔습니다.')
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError('CLI 실행 시간 제한 초과')
        return left

    def send(self, content):
        raw = (json.dumps({'type': 'user', 'message': {'role': 'user', 'content': content}}, ensure_ascii=False) + '\n').encode()
        done = threading.Event()
        errors = []

        def write():
            try:
                view = memoryview(raw)
                while view:
                    size = self.worker.proc.stdin.write(view)
                    if not size:
                        raise BrokenPipeError('Claude stdin closed')
                    view = view[size:]
            except (OSError, ValueError) as exc:
                errors.append(exc)
            finally:
                done.set()

        self.remaining()
        thread = threading.Thread(target=write, daemon=True)
        thread.start()
        self.worker.threads.append(thread)
        try:
            while not done.wait(min(.05, self.remaining())):
                pass
            self.remaining()
            if errors:
                raise ValueError('Claude 입력 스트림이 종료되었습니다.') from None
        finally:
            if done.is_set():
                thread.join()
                self.worker.threads.remove(thread)

    def receive(self):
        worker = self.worker
        while True:
            self.remaining()
            if b'\n' in worker.buffer:
                line, worker.buffer = worker.buffer.split(b'\n', 1)
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ValueError('Claude 스트림 JSON 형식 오류') from None
                if not isinstance(event, dict):
                    raise ValueError('Claude 스트림 객체가 필요합니다.')
                return event
            try:
                name, data = worker.events.get(timeout=min(.05, self.remaining()))
            except queue.Empty:
                if worker.proc.poll() is not None:
                    raise ValueError('Claude 프로세스가 응답 전에 종료되었습니다.') from None
                continue
            if not data:
                if name == 'out':
                    raise ValueError('Claude 출력 스트림이 종료되었습니다.')
                continue
            self.count += len(data)
            if self.count > self.maximum:
                raise ValueError('CLI 응답 크기 제한 초과')
            if name == 'err':
                self.stderr.write(data)
            else:
                self.stream.write(data)
                worker.buffer += data
                if self.inference:
                    self.timing.mark('first_stdout')

    def terminal(self, *, reset=False):
        old_session = self.worker.session
        reset_from = None
        while True:
            event = self.receive()
            kind = event.get('type')
            if kind == 'conversation_reset':
                if reset_from is not None:
                    raise ValueError('중복된 Claude 대화 초기화 응답입니다.')
                reset_from = event.get('session_id')
                if (not reset or not isinstance(reset_from, str) or not reset_from
                        or (old_session is not None and reset_from != old_session)
                        or not event.get('new_conversation_id')):
                    raise ValueError('Claude 대화 초기화 응답이 일치하지 않습니다.')
            elif kind == 'result':
                session = event.get('session_id')
                if not isinstance(session, str) or not session:
                    raise ValueError('Claude 응답 세션이 없습니다.')
                if reset:
                    stats = event.get('usage')
                    if (reset_from is None or session == reset_from or event.get('is_error') is not False
                            or event.get('subtype') != 'success' or event.get('total_cost_usd') != 0
                            or not isinstance(stats, dict)
                            or any(type(stats.get(k)) is not int or stats[k] != 0 for k in
                                   ('input_tokens', 'output_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                            or event.get('modelUsage', {}) != {}):
                        raise ValueError('Claude 초기화 확인 실패: stream-json /clear 지원 버전을 확인하세요.')
                    self.worker.session = session
                elif session != old_session:
                    raise ValueError('Claude 응답 세션이 일치하지 않습니다.')
                return event
            elif kind not in ('system', 'assistant', 'user', 'rate_limit_event', 'stream_event'):
                raise ValueError('지원하지 않는 Claude 스트림 이벤트입니다.')
            elif reset and kind in ('assistant', 'user', 'stream_event'):
                raise ValueError('Claude 초기화 중 예기치 않은 모델 출력입니다.')
            elif not reset and event.get('session_id', old_session) != old_session:
                raise ValueError('Claude 스트림 세션이 일치하지 않습니다.')


class Manager:
    """Shared by CLI copies. Only the lock owner may retire an active worker."""
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.worker = None
        _MANAGERS.add(self)

    @contextmanager
    def acquire(self, binary, model, deadline, timing):
        while not self.lock.acquire(timeout=max(0, min(.05, deadline - time.monotonic()))):
            if self.closed.is_set():
                raise ValueError('Claude worker가 닫혔습니다.')
            if time.monotonic() >= deadline:
                raise TimeoutError('CLI 실행 시간 제한 초과 (worker 대기)')
        try:
            if self.closed.is_set():
                raise ValueError('Claude worker가 닫혔습니다.')
            if time.monotonic() >= deadline:
                raise TimeoutError('CLI 실행 시간 제한 초과 (worker 대기)')
            timing.mark('worker_acquired')
            if self.worker is not None and (self.worker.key != (binary, model) or self.worker.proc.poll() is not None or self.worker.stopped.is_set()):
                self.retire()
            timing.reused(self.worker is not None)
            if self.worker is None:
                self.worker = Worker(binary, model, timing)
                self.worker.manager_closed = self.closed
            try:
                yield self.worker
            except BaseException:
                self.retire()
                raise
        finally:
            self.lock.release()

    def retire(self):
        if self.worker is not None:
            worker = self.worker
            worker.stopped.set()
            worker.close()
            self.worker = None

    def close(self):
        self.closed.set()  # Active exchange sees this without waiting for the lock.
        with self.lock:
            self.retire()

    def __del__(self):
        self.close()


class ExtractionPool:
    """Dedicated bounded managers; lease and exchange share one absolute deadline."""
    def __init__(self, binary, model, capacity=2):
        if type(capacity) is not int or capacity not in (1, 2): raise ValueError('추출 worker 수는 1 또는 2여야 합니다.')
        self.binary, self.model, self.capacity = binary, model, capacity
        self.pool_id = uuid4().hex
        self.closed = threading.Event()
        self.condition = threading.Condition()
        self.managers = [Manager() for _ in range(capacity)]
        self.leased = set()
        self.waiting = 0
        self.generations = [0] * capacity
        self.worker_ids: list[str | None] = [None] * capacity

    @contextmanager
    def acquire(self, binary, model, deadline, timing, *, slot_id=None):
        if slot_id is not None and (type(slot_id) is not int or not 1 <= slot_id <= self.capacity):
            raise ValueError('추출 worker slot 오류')
        if (binary, model) != (self.binary, self.model): raise ValueError('고정 추출 worker 조건이 다릅니다.')
        slot = None
        with self.condition:
            self.waiting += 1
            try:
                while True:
                    if self.closed.is_set() or any(manager.closed.is_set() for manager in self.managers):
                        raise ValueError('추출 worker가 닫혔습니다.')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise TimeoutError('CLI 실행 시간 제한 초과 (추출 worker 대기)')
                    slots = range(self.capacity) if slot_id is None else (slot_id-1,)
                    slot = next((i for i in slots if i not in self.leased), None)
                    if slot is not None:
                        self.leased.add(slot)
                        break
                    self.condition.wait(timeout=min(.05, remaining))
            finally:
                self.waiting -= 1
        try:
            with self.managers[slot].acquire(binary, model, deadline, timing) as worker:
                with self.condition:
                    if self.worker_ids[slot] != worker.worker_id:
                        self.generations[slot] += 1
                        self.worker_ids[slot] = worker.worker_id
                    # The worker is exclusive to this lease; no shared current-slot pointer.
                    worker.extraction_identity = {'pool_id': self.pool_id, 'slot_id': slot+1,
                                                  'worker_id': worker.worker_id, 'generation': self.generations[slot]}
                yield worker
        finally:
            with self.condition:
                self.leased.remove(slot)
                self.condition.notify_all()

    def _mark_closed(self):
        # Caller holds condition; signal all active exchanges before joining any manager.
        self.closed.set()
        for manager in self.managers: manager.closed.set()
        self.condition.notify_all()

    def _close_managers(self):
        error = None
        for manager in self.managers:
            try: manager.close()
            except Exception as exc:
                if error is None: error = exc
        if error is not None: raise error

    def retire_if_idle(self):
        """Atomic admission closure for identity replacement, not a racy idle probe."""
        with self.condition:
            if self.leased or self.waiting: raise ValueError('사용 중인 추출 worker 조건은 변경할 수 없습니다.')
            self._mark_closed()
        self._close_managers()

    def close(self):
        with self.condition: self._mark_closed()
        self._close_managers()
