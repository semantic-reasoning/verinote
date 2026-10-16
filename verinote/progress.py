"""Read-only analysis progress from validated telemetry; DB controls completion."""
import json
import re

from .documents import MAX_TEXT, bounded_spans
from .ontology import DICTIONARY_CHUNK_SIZE, MAX_DICTIONARY_CALLS
from .usage import safe_file


def analysis_progress(root, store, run_id, coverage=None):
    unknown = {'known': False, 'phase': 'unknown', 'percent': None,
               'completed_chars': None, 'total_chars': None, 'completed_chunks': None,
               'total_chunks': None, 'resumed_chars': 0}
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        return unknown
    with store.connect() as db:
        row = db.execute('SELECT r.*,v.text AS source FROM runs r JOIN versions v ON r.document_id=v.document_id AND r.version=v.version WHERE r.id=?', (run_id,)).fetchone()
    if not row:
        return unknown
    run = store.unpack_run(row)
    status, source = run['status'], run['source']
    if not 0 < len(source) <= MAX_TEXT or run['stage'] not in ('ontology', 'extract'):
        return unknown | {'phase': status}
    slices = (list(bounded_spans(source, DICTIONARY_CHUNK_SIZE)) if run['stage'] == 'ontology'
              else [(offset, source[offset:offset+8000]) for offset in range(0, len(source), 8000)])
    ends = {offset + len(chunk) for offset, chunk in slices}
    base = unknown | {'phase': status, 'total_chars': len(source), 'total_chunks': len(slices)}
    if status == 'success':
        return base | {'known': True, 'percent': 100, 'completed_chars': len(source), 'completed_chunks': len(slices)}
    if status == 'queued':
        return base | {'known': True, 'percent': 0, 'completed_chars': 0, 'completed_chunks': 0}
    try:
        resumed = 0
        if run['stage'] == 'ontology':
            raw = safe_file(root, f'runs/{run_id}/dictionary-progress.json', 8192)
            marker = json.loads(raw) if raw else None
            fields = {'schema_version', 'pipeline', 'run_id', 'calls_started', 'resumed_from', 'resumed_chars', 'completed_chars', 'total_chars', 'status'}
            if (not isinstance(marker, dict) or set(marker) != fields
                    or type(marker['schema_version']) is not int or marker['schema_version'] != 1
                    or marker['pipeline'] != 'source-spans-v1' or marker['run_id'] != run_id
                    or marker['status'] not in ('running', 'complete', 'error')
                    or any(type(marker[k]) is not int for k in ('calls_started', 'resumed_chars', 'completed_chars', 'total_chars'))
                    or marker['total_chars'] != len(source)
                    or not 0 <= marker['calls_started'] <= MAX_DICTIONARY_CALLS
                    or not 0 <= marker['resumed_chars'] <= marker['completed_chars'] <= len(source)
                    or marker['completed_chars'] not in ends | {0}
                    or marker['resumed_chars'] not in ends | {0}):
                return base
            previous = marker['resumed_from']
            if previous is not None and (not isinstance(previous, str) or not re.fullmatch(r'[a-f0-9]{32}', previous) or previous == run_id):
                return base
            if bool(marker['resumed_chars']) != bool(previous):
                return base
            completed, resumed = marker['completed_chars'], marker['resumed_chars']
            count = sum(end <= completed for end in ends)
        else:
            if coverage is None:
                from .extraction_coverage import read_report
                coverage = read_report(root, store, run_id)
            if not coverage.get('available'):
                return base
            # read_report has already checked ownership, exact offsets and accounting.
            completed = sum(c['length'] for c in coverage['chunks'])
            count = len(coverage['chunks'])
        phase = 'saving' if status == 'running' and completed == len(source) else status
        return base | {'known': True, 'phase': phase, 'percent': min(99, completed * 100 // len(source)),
                       'completed_chars': completed, 'completed_chunks': count, 'resumed_chars': resumed}
    except (ValueError, TypeError, KeyError, OSError, RecursionError):
        return base
