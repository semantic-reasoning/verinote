"""Advisory relation breadth, derived from a validated dictionary without loss."""
import json

from .documents import bounded_spans
from .ontology import DICTIONARY_CHUNK_SIZE, LIMITS, validate_dictionary
from .store import digest
from .usage import safe_file

DEFAULT_THRESHOLD = 20
POLICY = 1


def validate_threshold(value):
    if type(value) is not int or not 1 <= value <= LIMITS['entities']:
        raise ValueError('관계 타입 경고 기준은 1~200 정수여야 합니다.')
    return value


def type_warnings(dictionary, threshold=DEFAULT_THRESHOLD, *, basis='current_configuration'):
    threshold = validate_threshold(threshold)
    items = [{'relation_id': row['id'], 'field': field, 'count': len(set(row[field]))}
             for row in dictionary['relations'] for field in ('subject_types', 'object_types')
             if len(set(row[field])) > threshold]
    return {'available': True, 'threshold': threshold, 'basis': basis, 'items': items,
            'relation_count': len({row['relation_id'] for row in items}), 'field_count': len(items)}


def read_run_warnings(root, store, run_id, *, include_items=True):
    """Historical thresholds come from the checked execution checkpoint only."""
    unavailable = {'available': False, 'basis': 'execution_configuration', 'items': [],
                   'reason': '관계 타입 경고 기록을 확인할 수 없습니다. 경고 0건을 뜻하지 않습니다.'}
    try:
        from .dictionary_pipeline import CHECKPOINT_LIMIT
        run = store.run(run_id)
        if run['stage'] != 'ontology':
            return unavailable
        with store.connect() as db:
            row = db.execute('SELECT text,sha256 FROM versions WHERE document_id=? AND version=?',
                             (run['document_id'], run['version'])).fetchone()
        if row is None:
            return unavailable
        raw = safe_file(root, f'runs/{run_id}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        checkpoint = json.loads(raw) if raw else None
        if (not isinstance(checkpoint, dict) or checkpoint.get('run_id') != run_id
                or type(checkpoint.get('schema_version')) is not int or checkpoint['schema_version'] not in (1, 2, 3)
                or type(checkpoint.get('offset')) is not int or not isinstance(checkpoint.get('contract'), dict)):
            return unavailable
        contract, offset = checkpoint['contract'], checkpoint['offset']
        for key, expected in {'document_id': run['document_id'], 'version': run['version'],
                              'source_sha256': row['sha256'], 'text_sha256': digest(row['text'].encode()),
                              'agent': run['agent'], 'model': run['model']}.items():
            if type(contract.get(key)) is not type(expected) or contract[key] != expected:
                return unavailable
        if ('type_warning_policy' in contract or 'relation_type_warning_threshold' in contract):
            if type(contract.get('type_warning_policy')) is not int or contract['type_warning_policy'] != POLICY:
                return unavailable
            threshold = validate_threshold(contract.get('relation_type_warning_threshold'))
        else:
            threshold = DEFAULT_THRESHOLD  # Legacy strict checkpoints admitted at most 20.
        if offset not in {start + len(chunk) for start, chunk in bounded_spans(row['text'], DICTIONARY_CHUNK_SIZE)}:
            return unavailable
        if run['status'] == 'success' and offset != len(row['text']):
            return unavailable
        dictionary = validate_dictionary(checkpoint.get('dictionary'), row['text'][:offset])
        result = type_warnings(dictionary, threshold, basis='execution_configuration')
        result.update(processed_chars=offset, total_chars=len(row['text']), partial=offset < len(row['text']),
                      details_loaded=include_items)
        if not include_items:
            result['items'] = []
        return result
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return unavailable
