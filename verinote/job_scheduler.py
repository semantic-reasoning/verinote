"""Stage-bounded jobs with document leases held through the whole worker call."""
import threading
from concurrent.futures import Future
from dataclasses import dataclass, field


@dataclass(eq=False)
class Job:
    fn: object
    args: tuple
    kwargs: dict
    lane: str
    document: str | None
    key: tuple
    future: Future = field(default_factory=Future)


class JobScheduler:
    capacities = {'ontology': 2, 'extract': 2, 'question': 1}

    def __init__(self):
        self.condition = threading.Condition()
        self.pending = []
        self.active = []
        self.workers = []
        self.closed = False

    def submit(self, fn, /, *args, **kwargs):
        value = args[0] if args and isinstance(args[0], dict) else {}
        lane = value.get('stage', 'question')
        if lane not in self.capacities: raise ValueError('알 수 없는 작업 단계')
        document = value.get('document_id') if lane != 'question' else None
        job = Job(fn, args, kwargs, lane, document, ('run' if document else 'question', value.get('id')))
        with self.condition:
            if self.closed: raise RuntimeError('작업 대기열이 닫혔습니다.')
            # Start workers BEFORE enqueueing. A thread-start failure cannot leave an orphan job.
            while len(self.workers) < sum(self.capacities.values()):
                worker = threading.Thread(target=self._work, name=f'verinote-extract-{len(self.workers)}', daemon=True)
                worker.start()
                self.workers.append(worker)
            self.pending.append(job)
            job.future.add_done_callback(lambda _: self._wake())
            self.condition.notify_all()
        return job.future

    def _wake(self):
        with self.condition: self.condition.notify_all()

    def contains(self, kind, key):
        with self.condition:
            return any(j.key == (kind, key) for j in self.pending + self.active)

    def context(self, key, kind='run'):
        """Snapshot of lane contenders and a document lease; no database access. A question has no document, so it has no document
        blockers: it waits only for the questions in its own lane."""
        with self.condition:
            pending = [j for j in self.pending if not j.future.cancelled()]
            jobs = self.active + pending
            target = next((j for j in jobs if j.key == (kind, key)), None)
            if target is None: return None
            earlier = pending[:pending.index(target)] if target in pending else []
            contenders = [j.key for j in self.active + earlier if j is not target and j.lane == target.lane]
            blockers = [] if target.document is None else [j.key for j in self.active if j is not target and j.document == target.document]
            return {'lane': target.lane, 'capacity': self.capacities[target.lane],
                    'contenders': contenders, 'document_blockers': blockers}

    def _take(self):
        remaining = [j for j in self.pending if not j.future.cancelled()]
        if len(remaining) != len(self.pending):
            self.pending[:] = remaining
            self.condition.notify_all()
        for job in self.pending:
            if sum(j.lane == job.lane for j in self.active) >= self.capacities[job.lane]: continue
            if job.document is not None and any(j.document == job.document for j in self.active): continue
            self.pending.remove(job)
            if not job.future.set_running_or_notify_cancel(): return self._take()
            self.active.append(job)
            return job
        return None

    def _work(self):
        while True:
            with self.condition:
                while (job := self._take()) is None:
                    if self.closed and not self.pending: return
                    self.condition.wait()
            error: BaseException | None = None
            try:
                result = job.fn(*job.args, **job.kwargs)
            except BaseException as exc:
                error = exc
            else:
                error = None
            finally:
                with self.condition:
                    self.active.remove(job)
                    self.condition.notify_all()
            # User callbacks can call submit/shutdown; never invoke them under the scheduler lock.
            if error is None: job.future.set_result(result)
            else: job.future.set_exception(error)

    def wait_idle(self):
        """Wait for all registered work without closing admission (unlike a no-op job)."""
        with self.condition:
            while self.pending or self.active: self.condition.wait()

    def shutdown(self, wait=True, *, cancel_futures=False):
        if cancel_futures: raise ValueError('작업은 Service.cancel을 통해 취소하세요.')
        with self.condition:
            self.closed = True
            workers = list(self.workers)
            self.condition.notify_all()
        if wait:
            for worker in workers:
                if worker is not threading.current_thread(): worker.join()
