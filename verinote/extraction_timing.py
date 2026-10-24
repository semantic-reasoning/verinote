"""Optional same-process orchestration timing for approved-dictionary CLI runs."""
import copy
import json
import math
import re
import time
from contextlib import contextmanager

from .cli_timing import read_cli_timing
from .documents import chunks
from .usage import AGENTS, safe_file

MAX_BYTES = 128 * 1024
MAX_SPANS = 96
KINDS = {'restore', 'chunk', 'request', 'validation_with_bookkeeping', 'repair',
         'repair_request', 'repair_validation_with_bookkeeping', 'publish'}


def error_kind(exc):
    for _ in range(8):
        if isinstance(exc, TimeoutError): return 'timeout'
        exc = getattr(exc, '__cause__', None)
        if exc is None: break
    return 'error'


class ExtractionTiming:
    def __init__(self, root, *, submitted=True, clock=None):
        self.root, self.clock = root, clock or time.monotonic_ns
        self.disabled, self.base, self.last = False, None, None
        self.value = {'schema_version': 1, 'identity': None, 'agent': None, 'model': None,
                      'submitted_observed': submitted, 'worker_started': None, 'worker_finished': None,
                      'status': 'error', 'error_code': 'error', 'resumed_from': None, 'chunks': [], 'spans': []}
        self.active_chunk = None
        if submitted: self._now()

    def _now(self):
        if self.disabled: return None
        try:
            tick = self.clock()
            if type(tick) is not int or (self.last is not None and tick < self.last): raise ValueError('clock')
            if self.base is None: self.base = tick
            self.last = tick
            return (tick - self.base) / 1e9
        except Exception:
            self.disabled = True
            return None

    def bind(self, run):
        try:
            self.value.update(identity={'run_id': run['id'], 'document_id': run['document_id'],
                                        'version': run['version'], 'source_sha256': None},
                              agent=run['agent'], model=run['model'])
        except Exception: self.disabled = True

    def worker_start(self):
        self.value['worker_started'] = self._now()

    def configure(self, doc):
        try:
            self.value['identity']['source_sha256'] = doc['sha256']
            self.value['chunks'] = [{'index': i, 'offset': offset, 'length': len(text), 'reused': False}
                                    for i, (offset, text) in enumerate(chunks(doc['text']), 1)]
        except Exception: self.disabled = True

    def reuse(self, count, previous):
        try:
            for row in self.value['chunks'][:count]: row['reused'] = True
            self.value['resumed_from'] = previous
        except Exception: self.disabled = True

    def _begin(self, kind, index):
        try:
            start = self._now()
            if start is None: return None
            if len(self.value['spans']) >= MAX_SPANS: raise ValueError('span limit')
            span = {'kind': kind, 'chunk': index, 'start': start, 'end': None, 'status': 'success', 'cli_call_id': None}
            self.value['spans'].append(span)
            return span
        except Exception:
            self.disabled = True
            return None

    def _end(self, span, status):
        try:
            if span is not None: span.update(end=self._now(), status=status)
        except Exception: self.disabled = True

    @contextmanager
    def section(self, kind, index=None):
        span, status = self._begin(kind, index), 'success'
        try:
            yield
        except BaseException as exc:
            status = error_kind(exc)
            raise
        finally:
            self._end(span, status)

    @contextmanager
    def call(self, index, repair=False):
        span, status = self._begin('repair_request' if repair else 'request', index), 'success'
        try:
            yield
        except BaseException as exc:
            status = error_kind(exc)
            raise
        finally:
            self._end(span, status)
            try:
                if span is not None:
                    report = read_cli_timing(self.root, self._folder(index, repair))
                    if (report['available'] and report['agent'] == self.value['agent']
                            and report['model'] == self.value['model'] and report['phase'] is None):
                        span['cli_call_id'] = report['cli_call_id']
            except Exception: pass  # Missing call linkage is independent of document timing.

    def _folder(self, index, repair):
        return f'runs/{self.value["identity"]["run_id"]}/{index}' + ('/quote-repair' if repair else '')

    def begin_chunk(self, index): self.active_chunk = self._begin('chunk', index)

    def end_chunk(self, status='success'):
        self._end(self.active_chunk, status); self.active_chunk = None

    def outcome(self, status, code=None): self.value.update(status=status, error_code=code)

    def finish(self):
        try:
            self.value['worker_finished'] = self._now()
            if self.disabled: return
            validate_record(self.value)
            from .http_records import write_record
            folder = self.root / 'runs' / self.value['identity']['run_id']
            write_record(folder, 'extraction-timing.json', self.value, MAX_BYTES)
        except Exception: pass


class _NoTiming:
    def bind(self, run): pass
    def worker_start(self): pass
    def configure(self, doc): pass
    def reuse(self, count, previous): pass
    def begin_chunk(self, index): pass
    def end_chunk(self, status='success'): pass
    def outcome(self, status, code=None): pass
    def finish(self): pass

    @contextmanager
    def section(self, kind, index=None): yield
    @contextmanager
    def call(self, index, repair=False): yield


def create_timing(root, agent, stage, *, submitted=True):
    if stage != 'extract' or agent not in AGENTS - {'openrouter'}: return _NoTiming()
    try: return ExtractionTiming(root, submitted=submitted)
    except Exception: return _NoTiming()


def _number(n):
    return type(n) in (int, float) and 0 <= n <= 10**9 and math.isfinite(n)


def _id(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{32}', value) is not None


def validate_record(value):
    keys = {'schema_version', 'identity', 'agent', 'model', 'submitted_observed', 'worker_started',
            'worker_finished', 'status', 'error_code', 'resumed_from', 'chunks', 'spans'}
    if not isinstance(value, dict) or set(value) != keys or type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('timing schema')
    identity = value['identity']
    if (not isinstance(identity, dict) or set(identity) != {'run_id', 'document_id', 'version', 'source_sha256'}
            or not _id(identity['run_id']) or not _id(identity['document_id'])
            or type(identity['version']) is not int or identity['version'] < 1
            or not isinstance(identity['source_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', identity['source_sha256'])
            or value['agent'] not in AGENTS - {'openrouter'} or not isinstance(value['model'], str)
            or len(value['model']) > 150 or value['model'].startswith('-')
            or not re.fullmatch(r'[\w./:@+\[\],= -]*', value['model'])
            or type(value['submitted_observed']) is not bool
            or not _number(value['worker_started']) or not _number(value['worker_finished'])
            or value['worker_started'] > value['worker_finished']
            or value['status'] not in ('success', 'error') or value['error_code'] not in (None, 'error', 'timeout')
            or (value['status'] == 'success') != (value['error_code'] is None)):
        raise ValueError('timing identity/state')
    rows, spans = value['chunks'], value['spans']
    if not isinstance(rows, list) or not 1 <= len(rows) <= 16 or not isinstance(spans, list) or len(spans) > MAX_SPANS:
        raise ValueError('timing size')
    offset, new_seen = 0, False
    for index, row in enumerate(rows, 1):
        if (not isinstance(row, dict) or set(row) != {'index', 'offset', 'length', 'reused'}
                or type(row['index']) is not int or row['index'] != index
                or type(row['offset']) is not int or row['offset'] != offset
                or type(row['length']) is not int or not 1 <= row['length'] <= 8000
                or type(row['reused']) is not bool or (row['reused'] and new_seen)):
            raise ValueError('timing chunk')
        new_seen |= not row['reused']; offset += row['length']
    if any(row['reused'] for row in rows) != (value['resumed_from'] is not None) or (value['resumed_from'] is not None and not _id(value['resumed_from'])):
        raise ValueError('timing resume')
    seen, last_start = set(), value['worker_started']
    for span in spans:
        if (not isinstance(span, dict) or set(span) != {'kind', 'chunk', 'start', 'end', 'status', 'cli_call_id'}
                or not isinstance(span['kind'], str) or span['kind'] not in KINDS
                or not _number(span['start']) or not _number(span['end'])
                or not last_start <= span['start'] <= span['end'] <= value['worker_finished']
                or span['status'] not in ('success', 'error', 'timeout')):
            raise ValueError('timing span')
        last_start = span['start']
        if span['kind'] in ('restore', 'publish'):
            if span['chunk'] is not None: raise ValueError('timing parent')
        elif (type(span['chunk']) is not int or not 1 <= span['chunk'] <= len(rows)
              or rows[span['chunk']-1]['reused']): raise ValueError('timing span chunk')
        key = (span['kind'], span['chunk'])
        if key in seen: raise ValueError('timing duplicate')
        seen.add(key)
        if span['kind'] in ('request', 'repair_request'):
            if span['cli_call_id'] is not None and not _id(span['cli_call_id']): raise ValueError('timing call id')
        elif span['cli_call_id'] is not None: raise ValueError('timing unexpected call id')
    parents = {'request': 'chunk', 'validation_with_bookkeeping': 'chunk', 'repair': 'validation_with_bookkeeping',
               'repair_request': 'repair', 'repair_validation_with_bookkeeping': 'repair'}
    by_key = {(span['kind'], span['chunk']): span for span in spans}
    for span in spans:
        if span['kind'] in parents:
            parent = by_key.get((parents[span['kind']], span['chunk']))
            if (parent is None or not parent['start'] <= span['start'] <= span['end'] <= parent['end']
                    or (parent['status'] == 'success' and span['status'] != 'success')):
                raise ValueError('timing nesting')
    for kinds in (('restore', 'chunk', 'publish'), ('request', 'validation_with_bookkeeping'),
                  ('repair_request', 'repair_validation_with_bookkeeping')):
        groups: dict[int | None, list] = {None: []} if 'chunk' in kinds else {i: [] for i in range(1, len(rows)+1)}
        for span in spans:
            if span['kind'] in kinds: groups[None if 'chunk' in kinds else span['chunk']].append(span)
        for group in groups.values():
            ordered = sorted(group, key=lambda s: s['start'])
            if any(a['end'] > b['start'] for a, b in zip(ordered, ordered[1:], strict=False)): raise ValueError('timing overlap')
    executed = [s for s in spans if s['kind'] == 'chunk']
    expected_indices = [r['index'] for r in rows if not r['reused']][:len(executed)]
    if [s['chunk'] for s in executed] != expected_indices or any(s['status'] != 'success' for s in executed[:-1]):
        raise ValueError('timing sequential chunks')
    if sum(s['kind'] == 'repair_request' for s in spans) > 2: raise ValueError('timing repair cap')
    if not value['submitted_observed'] and value['worker_started'] != 0:
        raise ValueError('timing execution origin')
    if ('restore', None) not in by_key:
        raise ValueError('timing restore missing')
    if value['status'] == 'success':
        if ('publish', None) not in by_key or by_key['publish', None]['status'] != 'success': raise ValueError('timing publish')
        for row in rows:
            if not row['reused'] and any(by_key.get((kind, row['index']), {}).get('status') != 'success'
                                         for kind in ('chunk', 'request', 'validation_with_bookkeeping')):
                raise ValueError('timing incomplete success')
    return value


def summarize_record(value):
    spans = value['spans']
    def difference(total, *parts):
        if total is None: return None
        remainder = total - math.fsum(p for p in parts if p is not None)
        return remainder if remainder >= 0 else None
    def duration(kind, index=None):
        selected = [s['end']-s['start'] for s in spans if s['kind'] == kind and (index is None or s['chunk'] == index)]
        return math.fsum(selected) if selected else None
    result = {'queue_seconds': value['worker_started'] if value['submitted_observed'] else None,
              'total_seconds': value['worker_finished'] if value['submitted_observed'] else None,
              'worker_seconds': value['worker_finished']-value['worker_started'],
              'restore_seconds': duration('restore'), 'publish_seconds': duration('publish'),
              'new_call_attempts': sum(s['kind'] in ('request', 'repair_request') for s in spans), 'chunks': []}
    for row in value['chunks']:
        index = row['index']; total = duration('chunk', index)
        if row['reused']:
            result['chunks'].append(row | {'total_seconds': None, 'new_call_attempts': 0})
            continue
        request, validation, repair = duration('request', index), duration('validation_with_bookkeeping', index), duration('repair', index)
        repair_call, repair_validation = duration('repair_request', index), duration('repair_validation_with_bookkeeping', index)
        leaves = {'request_seconds': request, 'validation_with_bookkeeping_seconds': difference(validation, repair),
                  'repair_request_seconds': repair_call, 'repair_validation_with_bookkeeping_seconds': repair_validation,
                  'repair_other_seconds': difference(repair, repair_call, repair_validation)}
        incomplete = ((validation is not None and leaves['validation_with_bookkeeping_seconds'] is None)
                      or (repair is not None and leaves['repair_other_seconds'] is None))
        other = None if incomplete else difference(total, *leaves.values())
        result['chunks'].append(row | {'total_seconds': total, **leaves, 'other_seconds': other,
                                      'new_call_attempts': sum(s['chunk'] == index and s['kind'] in ('request', 'repair_request') for s in spans)})
    top = sum(s['end']-s['start'] for s in spans if s['kind'] in ('restore', 'chunk', 'publish'))
    result['worker_other_seconds'] = difference(result['worker_seconds'], top)
    return result


def read_extraction_timing(root, store, run_id):
    unavailable = {'available': False, 'reason': '문서 추출 시간 기록을 확인할 수 없습니다.'}
    try:
        run = store.run(run_id)
        if run['stage'] != 'extract' or run['agent'] == 'openrouter': return unavailable
        raw = safe_file(root, f'runs/{run_id}/extraction-timing.json', MAX_BYTES)
        value = validate_record(json.loads(raw) if raw else None)
        with store.connect() as db:
            doc = db.execute('SELECT text,sha256 FROM versions WHERE document_id=? AND version=?', (run['document_id'], run['version'])).fetchone()
        if (doc is None or value['identity'] != {'run_id': run_id, 'document_id': run['document_id'], 'version': run['version'], 'source_sha256': doc['sha256']}
                or value['agent'] != run['agent'] or value['model'] != run['model'] or value['status'] != run['status']
                or [(r['offset'], r['length']) for r in value['chunks']] != [(o, len(t)) for o, t in chunks(doc['text'])]):
            return unavailable
        result = copy.deepcopy(value)
        for span in result['spans']:
            if span['kind'] in ('request', 'repair_request'):
                folder = f'runs/{run_id}/{span["chunk"]}' + ('/quote-repair' if span['kind'] == 'repair_request' else '')
                link = read_cli_timing(root, folder)
                span['cli_link_available'] = bool(link['available'] and span['cli_call_id'] is not None and link['cli_call_id'] == span['cli_call_id']
                                                  and link['agent'] == value['agent'] and link['model'] == value['model'] and link['phase'] is None)
        return {'available': True, **result, 'summary': summarize_record(value)}
    except (OSError, ValueError, TypeError, KeyError, RecursionError): return unavailable
