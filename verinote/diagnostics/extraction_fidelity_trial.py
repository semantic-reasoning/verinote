"""One fresh bounded candidate trial. Only run() can dispatch provider calls."""
import copy
import platform
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path

from ..agents import CLI
from ..claude_worker import ExtractionPool, Manager, Worker
from ..extraction_ab import _identity, _known
from ..extraction_ab_runner import _command, _hash, file_index
from ..extraction_benchmark import (
    Budget,
    BudgetCaller,
    Limits,
    ReportWriter,
    Stopped,
    execute_document,
    sha,
)
from ..extraction_evaluation import DATA_ROOT, _read, load_case, load_manifest
from ..extraction_threshold_artifacts import verify_document
from ..extraction_transport import ExtractionSpec, ExtractionTransport
from ..store import digest
from ..usage import atomic_json
from .extraction_fidelity_artifacts import (
    LIMITS,
    MODEL,
    REPO,
    RESOLVED_MODEL,
    ROW,
    decision,
    plan,
    prompts,
    sidecar,
    sources,
    verify_manifest,
    verify_trial,
)
from .extraction_fidelity_candidate import CANDIDATE_ID, compile_prompt, template_hash

TAIL = 'final report/index/evaluation writes follow the last wall observation'


class TrialBudget(Budget):
    def __init__(self, *, clock=None):
        super().__init__(Limits(**LIMITS), clock=clock)
        self.reasons = []; self.source_hashes = {}; self.worker = None; self.binary = None

    def stop(self, reason):
        with self.lock:
            if reason not in self.reasons: self.reasons.append(reason)
            if self._stop_reason is None or self._stop_reason in ('call_failed', 'document_failed') and reason not in ('call_failed', 'document_failed'):
                self._stop_reason = reason

    def check_sources(self):
        if any(not (REPO/p).is_file() or (REPO/p).is_symlink() or _hash(REPO/p) != h for p, h in self.source_hashes.items()):
            self.stop('source_changed')
        if self.binary and _hash(Path(self.binary[0])) != self.binary[1]: self.stop('binary_changed')

    def check(self):
        self.check_sources()
        try: super().check()
        except Stopped as exc:
            self.stop(str(exc)); raise

    def finish(self, row, usage, **kwargs):
        super().finish(row, usage, **kwargs)
        if row['status'] != 'success': self.stop('call_failed')
        if not _known([row]): self.stop('usage_identity_unknown')
        if row['resolved_model'] != RESOLVED_MODEL: self.stop('model_changed_or_unknown')
        try: identity = _identity(row)
        except ValueError: identity = None
        if identity is None or row['worker_identity']['generation'] != 1:
            self.stop('worker_changed_or_unknown')
        elif self.worker is None: self.worker = copy.deepcopy(row['worker_identity'])
        elif row['worker_identity'] != self.worker: self.stop('worker_changed_or_unknown')


class SingleWorkerManager(Manager):
    """Trial-local lease owner: even a failed construction consumes its chance."""
    def __init__(self, stop):
        super().__init__()
        self.creation_attempted = False
        self.created_worker: Worker | None = None
        self.stop = stop

    @contextmanager
    def acquire(self, binary, model, deadline, timing):
        # Own the creation path rather than checking around Manager.acquire,
        # whose normal recovery policy can construct a replacement worker.
        while not self.lock.acquire(timeout=max(0, min(.05, deadline-time.monotonic()))):
            if self.closed.is_set():
                self.stop('worker_closed'); raise ValueError('trial worker closed')
            if time.monotonic() >= deadline:
                self.stop('worker_deadline'); raise TimeoutError('trial worker lease deadline')
        try:
            if self.closed.is_set():
                self.stop('worker_closed'); raise ValueError('trial worker closed')
            if time.monotonic() >= deadline:
                self.stop('worker_deadline'); raise TimeoutError('trial worker lease deadline')
            timing.mark('worker_acquired')
            if self.creation_attempted:
                worker = self.worker
                invalid = worker is None
                if worker is not None:
                    invalid = (worker.key != (binary, model) or worker.proc.poll() is not None or
                               worker.stopped.is_set() or worker is not self.created_worker)
                if invalid:
                    self.stop('worker_replacement_forbidden')
                    self.retire()
                    raise ValueError('trial forbids a second worker construction')
                timing.reused(True)
            else:
                # Latch BEFORE entering the constructor: exceptions must never
                # restore permission to create a second process.
                self.creation_attempted = True
                timing.reused(False)
                try:
                    self.worker = Worker(binary, model, timing)
                    self.created_worker = self.worker
                    self.worker.manager_closed = self.closed
                except BaseException:
                    self.stop('worker_creation_failed')
                    raise
            try:
                yield self.worker
            except BaseException:
                self.stop('worker_exchange_failed')
                self.retire()
                raise
        finally:
            self.lock.release()


def install_single_worker_guard(owner, budget):
    """Replace only a fresh, unexposed one-slot pool's unused manager."""
    if not isinstance(owner, ExtractionTransport) or not owner._owns_pool:
        raise ValueError('trial requires a fresh transport owner')
    pool = owner._pool
    if not isinstance(pool, ExtractionPool): raise ValueError('trial requires an extraction pool')
    with pool.condition:
        if (pool.capacity != 1 or len(pool.managers) != 1 or pool.closed.is_set() or
                pool.leased or pool.waiting or pool.generations != [0] or pool.worker_ids != [None] or
                owner._slot_id is not None or owner._delegate._claude is not pool):
            raise ValueError('trial pool is not virgin')
        old = pool.managers[0]
        if not old.lock.acquire(blocking=False): raise ValueError('trial manager is in use')
        try:
            if old.worker is not None or old.closed.is_set() or isinstance(old, SingleWorkerManager):
                raise ValueError('trial manager is not virgin')
            guarded = SingleWorkerManager(budget.stop)
            # Prevent a stale reference to the displaced virgin manager from
            # being used while installing the guard under the pool lock.
            old.closed.set()
            pool.managers[0] = guarded
        finally:
            old.lock.release()
    old.close()
    return guarded


class CandidateCaller:
    """Expected path/schedule only; sharing views never creates a new owner."""
    def __init__(self, caller, artifact, environment, budget, schedule=None):
        self.caller, self.artifact, self.environment, self.budget = caller, Path(artifact), environment, budget
        self.schedule = schedule if schedule is not None else []

    @property
    def identity(self): return self.caller.identity

    def close(self): self.caller.close()

    def limited_timeout(self, agent, seconds):
        return CandidateCaller(self.caller.limited_timeout(agent, min(seconds, 90)), self.artifact,
                               self.environment, self.budget, self.schedule)

    def complete(self, agent, model, prompt, folder):
        folder = Path(folder)
        relative = folder.relative_to(self.artifact/'runs').parts
        if (agent != 'claude' or model != MODEL or len(relative) not in (2, 3) or
                not re.fullmatch(r'[a-f0-9]{32}', relative[0]) or relative[1] not in ('1', '2', '3') or
                len(relative) == 3 and relative[2] != 'quote-repair'):
            self.budget.stop('unexpected_call'); raise Stopped('unexpected_call')
        run_id, chunk = relative[0], int(relative[1]); repair = len(relative) == 3
        initials = [i for i, repaired in self.schedule if not repaired]
        if ((chunk, repair) in self.schedule or not repair and chunk != len(initials)+1 or
                repair and (not self.schedule or self.schedule[-1] != (chunk, False)) or
                repair and sum(r for _, r in self.schedule) >= 2 or
                any((folder/name).exists() for name in ('run.json', 'prompt.txt', 'candidate.json', 'original-prompt.txt'))):
            self.budget.stop('unexpected_call'); raise Stopped('unexpected_call')
        self.schedule.append((chunk, repair))
        effective = prompt if repair else compile_prompt(prompt)
        folder.mkdir(parents=True, exist_ok=True)
        try:
            (folder/'original-prompt.txt').write_text(prompt, encoding='utf-8')
            record = sidecar(prompt, effective, chunk=chunk, repair=repair, run_id=run_id,
                             manifest_sha256=digest(self.environment))
            atomic_json(folder/'candidate.json', record)
            # The outer BudgetCaller reserved this request already. Recheck the
            # terminal budget after compilation/recording without reserving again.
            with self.budget.lock:
                self.budget.check_sources(); self.budget._check_terminal()
                remaining = self.budget.remaining()
                if remaining <= 0: self.budget.stop('time_limit'); raise Stopped('time_limit')
            return self.caller.limited_timeout(agent, min(90, remaining)).complete(agent, model, effective, folder)
        finally:
            try:
                record = sidecar(prompt, effective, chunk=chunk, repair=repair, run_id=run_id,
                                 manifest_sha256=digest(self.environment),
                                 call_id=_read(folder/'run.json').get('cli_call_id'))
                atomic_json(folder/'candidate.json', record)
            except (OSError, ValueError):
                self.budget.stop('candidate_record_failed'); raise


def run(output, *, allow_model_calls=False, owner_factory=None, clock=None, invocation=None):
    """Injected owners are always synthetic; ordinary calls need explicit opt-in."""
    synthetic = owner_factory is not None
    if not synthetic and allow_model_calls is not True: raise ValueError('explicit allow_model_calls=True is required')
    budget = TrialBudget(clock=clock)
    if not synthetic and _command(['git', 'status', '--porcelain'], REPO): raise ValueError('clean checkout required')
    registration_cli = CLI(timeout=90, persistent=False)
    spec = ExtractionSpec.capture(registration_cli, MODEL, 1)
    environment = {'schema_version': 1, 'synthetic': synthetic, 'candidate_id': CANDIDATE_ID,
                   'template_sha256': template_hash(), 'compiler_sha256': _hash(REPO/'verinote/diagnostics/extraction_fidelity_candidate.py'),
                   'transport': spec.to_dict(), 'binary_sha256': None if synthetic else _hash(Path(spec.binary)),
                   'cli_version': None, 'revision': None, 'tree': None, 'python': platform.python_version(),
                   'platform': platform.platform(), 'invocation': invocation or ['python_api', 'run'],
                   'source_hashes': {p: _hash(REPO/p) for p in sources()},
                   'dataset_manifest': load_manifest(DATA_ROOT/'v2'), 'case_sha256': sha(load_case('three', DATA_ROOT/'v2')),
                   'prompt_hashes': {k: digest(v.encode()) for k, v in prompts().items()}}
    if not synthetic:
        version = _command([spec.binary, '--version'])
        match = re.fullmatch(r'([0-9]+\.[0-9]+\.[0-9]+)(?: \(Claude Code\))?', version)
        if not match: raise ValueError('cannot pin Claude version')
        environment.update(cli_version=match[1], revision=_command(['git', 'rev-parse', 'HEAD'], REPO),
                           tree=_command(['git', 'rev-parse', 'HEAD^{tree}'], REPO))
        budget.binary = (spec.binary, environment['binary_sha256'])
    budget.source_hashes = environment['source_hashes']
    writer = ReportWriter(output); root = writer.folder
    shutil.copytree(DATA_ROOT/'v2', root/'fixtures')
    for relative in sources():
        dest = root/'implementation'/relative; dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(REPO/relative, dest)
    for name, value in [('plan', plan()), ('environment', environment), ('prompts', prompts()), ('limits', LIMITS), ('finalization', {'complete': False})]:
        atomic_json(root/(name+'.json'), value)
    row = dict(ROW, artifact='documents/three-1', status='running', quality=None, safety=None, document_timing=None,
               attempt_start=1, attempt_count=0)
    report = {'schema_version': 1, 'environment': environment, 'plan': plan(), 'limits': dict(LIMITS),
              'synthetic_responses': synthetic, 'documents': [row], 'attempts': budget.attempts,
              'status': 'running', 'stop_reason': None, 'stop_reasons': [], 'cleanup_errors': 0,
              'budget': budget.summary(), 'final_write_tail': TAIL}
    owner = None
    def save():
        report.update(budget=budget.summary(), stop_reason=budget.reason, stop_reasons=list(budget.reasons))
        try: writer.write(copy.deepcopy(report))
        except Exception:
            budget.stop('report_write_failed'); raise
    def close(resource):
        if resource is None: return
        try: resource.close()
        except Exception:
            report['cleanup_errors'] += 1; budget.stop('cleanup_error')
    save(); verify_manifest(root, report)
    artifact = root/row['artifact']; artifact.mkdir(parents=True)
    def make_caller(registered):
        nonlocal owner
        if registered != spec: raise ValueError('registered transport mismatch')
        budget.check(); owner = (owner_factory or ExtractionTransport)(spec)
        if owner.identity != spec: raise ValueError('owner identity mismatch')
        install_single_worker_guard(owner, budget)
        caller = CandidateCaller(owner.view(), artifact, environment, budget)
        return BudgetCaller(caller, budget, save, identity={'owner_id': 1, 'case_id': 'three', 'repetition': 1, 'document_key': ROW['key']})
    result = {'verdict': 'inconclusive', 'reasons': ['incomplete_trial']}
    try:
        execute_document(artifact, load_case('three', root/'fixtures'), make_caller, budget, row, close,
                         agent='claude', model=MODEL, extraction_policy='chunk_threshold_v1', registration_cli=registration_cli)
        if row['status'] != 'success' or not (row['quality'] or {}).get('passed'): budget.stop('document_failed')
    except KeyboardInterrupt: budget.stop('interrupted')
    except Exception: budget.stop(budget.reason or 'runner_error')
    finally:
        close(owner); row['attempt_count'] = len(budget.attempts)
        try:
            row['safety'] = verify_document(artifact, row, budget.attempts, load_case('three', root/'fixtures'), MODEL, spec)
        except (OSError, ValueError, KeyError, TypeError):
            budget.stop('document_evidence_unknown')
            row['safety'] = {'complete': False, 'passed': None, 'violations': []}
        try:
            budget.check_sources()
            if budget.remaining() <= 0: budget.stop('time_limit')
            row['artifact_files'] = file_index(artifact)
            report['status'] = 'stopped' if budget.reason else 'completed'; save()
            index = file_index(root, excluded=('index.json', 'evaluation.json'))
            atomic_json(root/'index.json', index)
            # Complete raw hashing, DB and prompt/repair reconstruction precede
            # the last observation. Reclassify cached verification after it.
            safety, production = verify_trial(root, report)
            report['budget'] = budget.summary()
            if report['budget']['elapsed_seconds'] >= LIMITS['seconds']: budget.stop('time_limit')
            report.update(status='stopped' if budget.reason else 'completed', stop_reason=budget.reason,
                          stop_reasons=list(budget.reasons), budget=report['budget'] | {'stop_reason': budget.reason})
            result = decision(report, safety, production)
            writer.write(copy.deepcopy(report))
            index['report.json'] = {'bytes': (root/'report.json').stat().st_size, 'sha256': _hash(root/'report.json')}
            atomic_json(root/'evaluation.json', result)
            atomic_json(root/'finalization.json', {'complete': True})
            index['finalization.json'] = {'bytes': (root/'finalization.json').stat().st_size, 'sha256': _hash(root/'finalization.json')}
            atomic_json(root/'index.json', index)
        except Exception:
            budget.stop('finalization_failed')
            report.update(status='stopped', stop_reason=budget.reason, stop_reasons=list(budget.reasons), budget=budget.summary())
            result = {'verdict': 'inconclusive', 'reasons': list(budget.reasons)}
    return report, result
