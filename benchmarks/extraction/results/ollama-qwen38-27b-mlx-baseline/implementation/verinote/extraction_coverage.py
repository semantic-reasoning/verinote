"""Bounded model self-review. Accounting consistency is not semantic completeness."""
import copy
import json
import re
from pathlib import Path

from .ontology import MAX_FACTS
from .usage import atomic_json, safe_file

MAX_REPORT_BYTES = 2 * 1024 * 1024
STATUSES = {'extracted', 'no_evidence', 'not_representable', 'limit_reached'}
GAP_CODES = {'missing_entity', 'missing_relation', 'type_mismatch', 'qualifier_not_representable'}
MAX_REPAIR_BYTES = 64 * 1024


def integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def indices(value, size):
    if (not isinstance(value, list) or len(value) > size
            or any(not integer(i, 0, size - 1) for i in value)
            or len(set(value)) != len(value)):
        raise ValueError('추출 점검: 중복 없는 범위 내 정수 인덱스가 필요합니다.')
    return value


def validate_accounting(value, dictionary):
    """Called after ordinary shape validation, before any quote-repair request."""
    from .service import text
    predicates = {r['id'] for r in dictionary['relations']}
    rows, gaps = value.get('coverage'), value.get('dictionary_gaps')
    if not isinstance(rows, list) or len(rows) != len(predicates):
        raise ValueError('추출 점검 coverage: 승인 관계마다 정확히 한 행이 필요합니다.')
    if not isinstance(gaps, list) or len(gaps) > 12:
        raise ValueError('추출 점검 dictionary_gaps: 최대 12개 배열이 필요합니다.')
    seen, covered = set(), set()
    facts, claims = value['facts'], value['claims']
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'predicate', 'fact_indices', 'claim_indices', 'status', 'reason'}:
            raise ValueError('추출 점검 coverage 필드 오류')
        predicate, status = row['predicate'], row['status']
        if not isinstance(predicate, str) or predicate not in predicates or predicate in seen:
            raise ValueError('추출 점검: 승인 관계가 누락·중복되거나 알 수 없는 관계입니다.')
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('추출 점검 status 오류')
        text(row['reason'], '추출 점검 reason', 120)
        fi = indices(row['fact_indices'], len(facts))
        ci = indices(row['claim_indices'], len(claims))
        if bool(fi) != (status == 'extracted'):
            raise ValueError('추출 점검: extracted 상태와 fact_indices가 일치하지 않습니다.')
        if status == 'not_representable' and not ci:
            raise ValueError('추출 점검: 표현 제약의 근거 claim_indices가 필요합니다.')
        if status == 'limit_reached' and len(facts) != MAX_FACTS:
            raise ValueError('추출 점검: 사실 한도에 도달하지 않았습니다.')
        if any(facts[i]['predicate'] != predicate or i in covered for i in fi):
            raise ValueError('추출 점검: fact_indices의 관계가 다르거나 중복됩니다.')
        covered.update(fi)
        seen.add(predicate)
    if covered != set(range(len(facts))):
        raise ValueError('추출 점검: 모든 사실을 같은 관계의 coverage에 포함해야 합니다.')
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap) != {'code', 'detail', 'claim_indices'}:
            raise ValueError('추출 점검 dictionary_gaps 필드 오류')
        if not isinstance(gap['code'], str) or gap['code'] not in GAP_CODES:
            raise ValueError('추출 점검 dictionary_gaps code 오류')
        text(gap['detail'], '추출 점검 detail', 160)
        if not indices(gap['claim_indices'], len(claims)):
            raise ValueError('추출 점검: 사전 제약의 근거 claim_indices가 필요합니다.')
    return copy.deepcopy({'coverage': rows, 'dictionary_gaps': gaps})


def repair_missing_indices(value, dictionary):
    """Normalize only fresh, shape-preflighted output; persisted data stays strict."""
    result = copy.deepcopy(value)
    changes = []
    rows = result.get('coverage')
    predicates = {r['id'] for r in dictionary['relations']}
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if (not isinstance(row, dict) or row.get('status') != 'extracted'
                    or not isinstance(row.get('predicate'), str) or row['predicate'] not in predicates):
                continue
            before = indices(row.get('fact_indices'), len(result['facts']))
            expected = [i for i, fact in enumerate(result['facts']) if fact['predicate'] == row['predicate']]
            if set(before) < set(expected):
                after = before + [i for i in expected if i not in before]
                row['fact_indices'] = after
                changes.append({'row': index, 'predicate': row['predicate'], 'before': before, 'after': after})
    # No partial repair may hide another malformed or contradictory field.
    validate_accounting(result, dictionary)
    return result, changes


def write_repair_audit(root, run, doc, chunk, offset, before, after, changes):
    """Mandatory provenance for a mechanical repair, not acceptance of its facts."""
    from .store import digest
    try:
        if not 1 <= len(changes) <= MAX_FACTS:
            raise ValueError('coverage repair count')
        folder = Path(root)
        for part in (None, 'runs', run['id'], str(chunk)):
            if part is not None:
                folder = folder / part
            if folder.is_symlink():
                raise ValueError('coverage repair symlink')
        path = folder / 'coverage-repair.json'
        if path.is_symlink() or path.with_suffix('.json.tmp').is_symlink():
            raise ValueError('coverage repair symlink')
        audit = {'schema_version': 1, 'identity': identity(run), 'chunk': chunk, 'offset': offset,
                 'source_sha256': doc['sha256'], 'text_sha256': digest(doc['text'].encode()),
                 'before_sha256': digest(before), 'after_sha256': digest(after),
                 'scope': 'mechanical_indices_only_not_chunk_acceptance', 'changes': changes}
        raw = safe_file(folder, 'run.json', 16384)
        try:
            meta = json.loads(raw) if raw else None
        except (ValueError, TypeError, RecursionError):
            meta = None
        if isinstance(meta, dict):
            for key in ('cli_call_id', 'provider_call_id'):
                call_id = meta.get(key)
                if isinstance(call_id, str) and 1 <= len(call_id) <= 200:
                    audit[key] = call_id
        if len(json.dumps(audit, ensure_ascii=False, indent=2).encode()) > MAX_REPAIR_BYTES:
            raise ValueError('coverage repair size')
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(path, audit)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('추출 점검 인덱스 보정 기록 저장 실패: 이번 청크는 확정하지 않았습니다.') from exc


def identity(run):
    pin = run['ontology']
    return {'run_id': run['id'], 'document_id': run['document_id'], 'version': run['version'],
            'dictionary_id': pin['id'], 'revision': pin['revision'], 'sha256': pin['sha256'],
            'extraction_contract': pin.get('extraction_contract', 0)}


def new_report(run, source_length):
    return {'schema_version': 1, 'identity': identity(run), 'source_length': source_length, 'chunks': []}


def chunk_report(output, offset, length):
    return {'offset': offset, 'length': length, 'claim_count': len(output['claims']),
            'fact_predicates': [f['predicate'] for f in output['facts']],
            'coverage': copy.deepcopy(output['coverage']), 'dictionary_gaps': copy.deepcopy(output['dictionary_gaps'])}


def write_report(root, report):
    """Optional telemetry; never let its failure roll back candidate publication."""
    try:
        if len(json.dumps(report, ensure_ascii=False, indent=2).encode()) > MAX_REPORT_BYTES:
            return False
        folder = Path(root)
        if folder.is_symlink():
            return False
        for part in ('runs', report['identity']['run_id']):
            folder = folder / part
            if folder.is_symlink():
                return False
        if any((folder / name).is_symlink() for name in ('extraction-coverage.json', 'extraction-coverage.json.tmp')):
            return False
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / 'extraction-coverage.json', report)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_report(root, store, run_id):
    unavailable = {'available': False, 'warning': '관계별 추출 점검 기록을 사용할 수 없습니다. 누락이 없다는 뜻은 아닙니다.'}
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        return unavailable
    # Recheck DB ownership even when the caller holds a stale run object after deletion.
    with store.connect() as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            return unavailable
        run = store.unpack_run(row)
        source = db.execute('SELECT length(text) FROM versions WHERE document_id=? AND version=?',
                            (run['document_id'], run['version'])).fetchone()
        pages = db.execute("SELECT payload FROM items WHERE run_id=? AND kind='page'", (run_id,)).fetchall()
        saved = db.execute("SELECT count(*) FROM items WHERE run_id=? AND kind='fact'", (run_id,)).fetchone()[0]
    if (not source or run['stage'] != 'extract' or not run['ontology']
            or type(run['ontology'].get('extraction_contract')) is not int
            or run['ontology']['extraction_contract'] not in (1, 2)):
        return unavailable
    raw = safe_file(root, f'runs/{run_id}/extraction-coverage.json', MAX_REPORT_BYTES)
    if raw is None:
        return unavailable
    try:
        report = json.loads(raw)
        if (not isinstance(report, dict) or set(report) != {'schema_version', 'identity', 'source_length', 'chunks'}
                or type(report['schema_version']) is not int or report['schema_version'] != 1
                or report['identity'] != identity(run)
                or not isinstance(report['identity'], dict)
                or any(type(report['identity'].get(k)) is not int for k in ('version', 'revision', 'extraction_contract'))
                or not integer(report['source_length'], 1, 128000) or report['source_length'] != source[0]
                or not isinstance(report['chunks'], list) or len(report['chunks']) > 16):
            return unavailable
        total_chunks = (source[0] + 7999) // 8000
        if len(report['chunks']) > total_chunks:
            return unavailable
        claims, emitted, predicates, gaps, capped = 0, 0, set(), 0, False
        for i, chunk in enumerate(report['chunks']):
            if (not isinstance(chunk, dict) or set(chunk) != {'offset', 'length', 'claim_count', 'fact_predicates', 'coverage', 'dictionary_gaps'}
                    or not integer(chunk['offset'], 0, 127999) or chunk['offset'] != i * 8000
                    or not integer(chunk['length'], 1, 8000) or chunk['length'] != min(8000, source[0] - i * 8000)
                    or not integer(chunk['claim_count'], 1, 12)
                    or not isinstance(chunk['fact_predicates'], list) or len(chunk['fact_predicates']) > MAX_FACTS
                    or any(not isinstance(p, str) for p in chunk['fact_predicates'])):
                return unavailable
            validate_accounting(chunk | {'facts': [{'predicate': p} for p in chunk['fact_predicates']],
                                         'claims': [None] * chunk['claim_count']}, run['ontology']['payload'])
            # Indices in reports are local; page claims concatenate without deduplication.
            chunk['claim_base'] = claims
            claims += chunk['claim_count']
            emitted += len(chunk['fact_predicates'])
            predicates.update(chunk['fact_predicates'])
            gaps += len(chunk['dictionary_gaps'])
            capped |= len(chunk['fact_predicates']) == MAX_FACTS
        if saved > emitted or (run['status'] == 'success' and (len(report['chunks']) != total_chunks
                or len(pages) != 1 or len(json.loads(pages[0][0])['claims']) != claims)):
            return unavailable
        return {'available': True, 'status': run['status'], 'published': run['status'] == 'success',
                'total_chunks': total_chunks, 'validated_chunks': len(report['chunks']),
                'emitted_fact_count': emitted, 'saved_fact_count': saved if run['status'] == 'success' else 0,
                'extracted_predicates': len(predicates), 'approved_predicates': len(run['ontology']['payload']['relations']),
                'gap_count': gaps, 'capped': capped, 'chunks': report['chunks']}
    except (ValueError, TypeError, KeyError, RecursionError):
        return unavailable
