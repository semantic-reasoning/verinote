"""Bounded, resumable evidence-exclusion audit; missing is never zero."""
import copy
import json
from pathlib import Path

from .dictionary_filter import (
    DEPENDENT,
    FILTER_POLICY,
    REASONS,
    SUPPORTED_FILTER_POLICIES,
)
from .documents import bounded_spans
from .ontology import DICTIONARY_CHUNK_SIZE, LIMITS
from .usage import atomic_json, safe_file

MAX_DETAILS = 500
MAX_REPORT_BYTES = 512 * 1024


def identity(run, doc):
    return {'run_id': run['id'], 'document_id': run['document_id'],
            'version': run['version'], 'source_sha256': doc['sha256']}


def new_report(run, doc):
    return {'schema_version': 1, 'identity': identity(run, doc), 'chunks': [], 'items': []}


def add_chunk(report, offset, length, excluded):
    result = copy.deepcopy(report)
    counts = {kind: sum(row['kind'] == kind for row in excluded) for kind in LIMITS}
    dependent = sum(row['code'] in DEPENDENT for row in excluded)
    result['chunks'].append({'offset': offset, 'length': length, 'counts': counts,
                             'direct': len(excluded) - dependent, 'dependent': dependent})
    remaining = MAX_DETAILS - len(result['items'])
    result['items'].extend(dict(row, chunk=len(result['chunks'])) for row in excluded[:remaining])
    return result


def validate_report(value, run, doc, accepted=None, *, filter_policy=FILTER_POLICY):
    if type(filter_policy) is not int or filter_policy not in SUPPORTED_FILTER_POLICIES:
        raise ValueError('알 수 없는 사전 제외 정책입니다.')
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'identity', 'chunks', 'items'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['identity'], dict) or type(value['identity'].get('version')) is not int
            or value['identity'] != identity(run, doc)):
        raise ValueError('사전 제외 기록의 실행·원문 정보가 일치하지 않습니다.')
    chunks, items = value['chunks'], value['items']
    slices = list(bounded_spans(doc['text'], DICTIONARY_CHUNK_SIZE))
    if not isinstance(chunks, list) or len(chunks) > len(slices) or not isinstance(items, list):
        raise ValueError('사전 제외 기록의 구간 형식 오류')
    total = 0
    for row, (offset, chunk) in zip(chunks, slices, strict=False):
        if (not isinstance(row, dict) or set(row) != {'offset', 'length', 'counts', 'direct', 'dependent'}
                or type(row['offset']) is not int or row['offset'] != offset
                or type(row['length']) is not int or row['length'] != len(chunk)
                or not isinstance(row['counts'], dict) or set(row['counts']) != set(LIMITS)
                or any(type(row['counts'][kind]) is not int or not 0 <= row['counts'][kind] <= limit for kind, limit in LIMITS.items())
                or any(type(row[k]) is not int or row[k] < 0 for k in ('direct', 'dependent'))
                or row['direct'] + row['dependent'] != sum(row['counts'].values())):
            raise ValueError('사전 제외 기록의 집계 오류')
        total += row['direct'] + row['dependent']
    processed = sum(row['length'] for row in chunks)
    if accepted is not None and processed != accepted:
        raise ValueError('사전 제외 기록과 복구 위치가 일치하지 않습니다.')
    if len(items) != min(total, MAX_DETAILS):
        raise ValueError('사전 제외 기록의 상세 개수 오류')
    seen = set()
    previous = (0, 0, 0)
    detailed = [{kind: 0 for kind in LIMITS} for _ in chunks]
    dependencies = [0 for _ in chunks]
    for row in items:
        if (not isinstance(row, dict) or set(row) != {'chunk', 'kind', 'row', 'identifier', 'code'}
                or type(row['chunk']) is not int or not 1 <= row['chunk'] <= len(chunks)
                or not isinstance(row['kind'], str) or row['kind'] not in LIMITS
                or type(row['row']) is not int or not 1 <= row['row'] <= LIMITS[row['kind']]
                or not isinstance(row['identifier'], str) or not 1 <= len(row['identifier']) <= 80
                or not isinstance(row['code'], str) or row['code'] not in REASONS):
            raise ValueError('사전 제외 기록의 상세 형식 오류')
        if filter_policy == 1 and row['code'] == 'alias_existing_entity':
            raise ValueError('이전 정책에 없는 사전 제외 사유입니다.')
        if filter_policy < 3 and row['code'] in {'alias_existing_target', 'relation_undeclared_type'}:
            raise ValueError('이전 정책에 없는 사전 제외 사유입니다.')
        position = (row['chunk'], list(LIMITS).index(row['kind']), row['row'])
        expected_kind = {'entity_evidence': 'entities', 'alias_evidence': 'aliases',
                         'excluded_entity': 'aliases', 'excluded_type': 'relations',
                         'alias_existing_entity': 'aliases', 'alias_existing_target': 'aliases',
                         'relation_undeclared_type': 'relations'}.get(row['code'])
        if expected_kind is not None and row['kind'] != expected_kind:
            raise ValueError('사전 제외 사유와 항목 종류가 일치하지 않습니다.')
        if position in seen or position < previous:
            raise ValueError('사전 제외 기록의 상세 순서 오류')
        previous = position; seen.add(position)
        detailed[row['chunk'] - 1][row['kind']] += 1
        dependencies[row['chunk'] - 1] += row['code'] in DEPENDENT
    for index, row in enumerate(chunks):
        if (any(detailed[index][kind] > row['counts'][kind] for kind in LIMITS)
                or dependencies[index] > row['dependent']
                or sum(detailed[index].values()) - dependencies[index] > row['direct']):
            raise ValueError('사전 제외 상세와 집계가 일치하지 않습니다.')
        if total <= MAX_DETAILS and (detailed[index] != row['counts'] or dependencies[index] != row['dependent']):
            raise ValueError('사전 제외 상세가 누락되었습니다.')
    return copy.deepcopy(value)


def public_report(report, include_items=True):
    counts = {kind: sum(row['counts'][kind] for row in report['chunks']) for kind in LIMITS}
    total = sum(counts.values())
    return {'available': True, 'total_count': total, 'counts': counts,
            'direct_count': sum(row['direct'] for row in report['chunks']),
            'dependent_count': sum(row['dependent'] for row in report['chunks']),
            'truncated_count': total - len(report['items']), 'details_loaded': include_items,
            'processed_chars': sum(row['length'] for row in report['chunks']),
            'items': [row | {'reason': REASONS[row['code']]} for row in report['items']] if include_items else []}


def write_report(root, report):
    try:
        if len(json.dumps(report, ensure_ascii=False).encode()) > MAX_REPORT_BYTES:
            return False
        folder = Path(root) / 'runs' / report['identity']['run_id']
        if Path(root).is_symlink() or folder.parent.is_symlink() or folder.is_symlink():
            return False
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / 'dictionary-exclusions.json', report)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_report(root, store, run_id, *, document_id=None, version=None, include_items=True):
    unavailable: dict = {'available': False, 'total_count': None, 'items': [], 'details_loaded': False,
                   'reason': '사전 제외 기록을 확인할 수 없습니다. 제외가 없었다는 뜻은 아닙니다.'}
    try:
        run = store.run(run_id)
        if (run['stage'] != 'ontology' or (document_id is not None and run['document_id'] != document_id)
                or (version is not None and run['version'] != version)):
            return unavailable
        with store.connect() as db:
            row = db.execute('SELECT text,sha256 FROM versions WHERE document_id=? AND version=?',
                             (run['document_id'], run['version'])).fetchone()
        if row is None:
            return unavailable
        doc = dict(row)
    except (OSError, ValueError, KeyError, TypeError):
        return unavailable
    try:
        raw = safe_file(root, f'runs/{run_id}/dictionary-exclusions.json', MAX_REPORT_BYTES)
        value = validate_report(json.loads(raw) if raw else None, run, doc,
                                len(doc['text']) if run['status'] == 'success' else None)
        return public_report(value, include_items)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    # A checkpoint can retain a known prefix count when the optional detail file
    # is missing. Do not present a partial prefix as a complete run-wide total.
    try:
        from .dictionary_pipeline import CHECKPOINT_LIMIT
        raw = safe_file(root, f'runs/{run_id}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        checkpoint = json.loads(raw) if raw else None
        if (not isinstance(checkpoint, dict) or checkpoint.get('run_id') != run_id
                or type(checkpoint.get('schema_version')) is not int or checkpoint['schema_version'] != 2
                or type(checkpoint.get('offset')) is not int
                or not isinstance(checkpoint.get('contract'), dict)
                or type(checkpoint['contract'].get('filter_policy')) is not int
                or checkpoint['contract']['filter_policy'] not in SUPPORTED_FILTER_POLICIES):
            return unavailable
        value = validate_report(checkpoint.get('exclusions'), run, doc, checkpoint['offset'],
                                filter_policy=checkpoint['contract']['filter_policy'])
        summary = public_report(value, False)
        return summary | unavailable | {'total_count': summary['total_count'], 'partial': True}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return unavailable
