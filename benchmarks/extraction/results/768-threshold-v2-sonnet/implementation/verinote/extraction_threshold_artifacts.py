"""Read-only v2 document evidence verification, also used before cold timing ends."""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .extraction_ab import _attempts
from .extraction_benchmark import (
    SYNTHETIC_REVIEW_REASON_V2,
    call_metrics,
    quality_report,
    resolved_model,
)
from .extraction_evaluation import _read
from .extraction_threshold_ab import validate_policy
from .extraction_timing import read_extraction_timing
from .store import Store, canonical, digest
from .usage import decode_transport


class EvidenceStore(Store):
    def __init__(self, path):
        self.path = str(Path(path).resolve())

    @contextmanager
    def connect(self, write=False):
        if write: raise ValueError('evidence is read-only')
        db = sqlite3.connect(Path(self.path).as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try: yield db
        finally: db.close()


def verify_document(artifact, row, attempts, case, model, spec):
    """Verify summaries against DB/checkpoint/provider output, then return safety."""
    artifact = Path(artifact)
    values = _attempts(row, attempts)
    if row.get('run_id') is None:
        if values or list(artifact.rglob('usage.json')): raise ValueError('unregistered_calls')
        return {'complete': False, 'passed': None, 'violations': []}
    store = EvidenceStore(artifact/'state.sqlite3')
    run = store.run(row['run_id']); doc = store.document(run['document_id'], run['version'])
    options = validate_policy(row)
    if (run['stage'] != 'extract' or run['agent'] != 'claude' or run['model'] != model
            or run['version'] != 1 or row.get('document_id') != run['document_id']
            or doc['text'] != case['source'] or store.raw(run['document_id'], 1)[0] != case['source'].encode()
            or doc['sha256'] != digest(case['source'].encode()) or run['ontology']['payload'] != case['dictionary']
            or run['ontology']['sha256'] != digest(case['dictionary'])
            or type(run['ontology'].get('extraction_contract')) is not int or run['ontology']['extraction_contract'] != 2
            or options != run['extraction_options'] or options['transport'] != spec.to_dict()):
        raise ValueError('document_contract')
    with store.connect() as db:
        if (db.execute('SELECT COUNT(*) FROM documents').fetchone()[0] != 1
                or db.execute('SELECT COUNT(*) FROM versions').fetchone()[0] != 1
                or db.execute('SELECT COUNT(*) FROM runs').fetchone()[0] != 2
                or db.execute('SELECT COUNT(*) FROM questions').fetchone()[0] != 0):
            raise ValueError('fresh_store_contract')
        events = db.execute('SELECT action,actor,reason,revision FROM events WHERE item_id=? ORDER BY revision', (run['ontology']['id'],)).fetchall()
    if (len(events) != 2 or (events[0]['action'], events[0]['actor'], events[0]['revision']) != ('created', 'agent:claude', 1)
            or dict(events[1]) != {'action': 'approved', 'actor': 'Synthetic fixture',
            'reason': SYNTHETIC_REVIEW_REASON_V2, 'revision': run['ontology']['revision']}):
        raise ValueError('synthetic_approval_provenance')
    candidates = [i for i in doc['items'] if i['kind'] != 'ontology']
    published = bool(candidates) or run['status'] == 'success'
    violations = []
    if doc['current_version'] != 1 or doc['generation'] != 1:
        if published: violations.append('publication_contract')
        else: raise ValueError('document_changed_without_publication')
    dictionaries = [i for i in doc['items'] if i['kind'] == 'ontology']
    dictionary_current = (len(dictionaries) == 1 and dictionaries[0]['id'] == run['ontology']['id']
                          and dictionaries[0]['revision'] == run['ontology']['revision']
                          and digest(dictionaries[0]['payload']) == run['ontology']['sha256']
                          and dictionaries[0]['status'] == 'approved')
    if not dictionary_current:
        if published: violations.append('dictionary_publication_contract')
        else: raise ValueError('dictionary_changed_without_publication')
    if any(i['status'] != 'pending' for i in candidates): violations.append('human_approval_contract')
    if row['status'] != run['status'] or row['quality'] != quality_report(case, artifact, run):
        raise ValueError('quality_mismatch')
    if run['status'] == 'success':
        checkpoint = _read(artifact/'runs'/run['id']/'extraction-checkpoint.json')
        if checkpoint['contract']['extraction_options'] != options: raise ValueError('checkpoint_contract')
        expected_facts = {canonical(f) for output in checkpoint['outputs'] for f in output['facts']}
        actual_facts = {canonical(i['payload']) for i in candidates if i['kind'] == 'fact'}
        pages = [i for i in candidates if i['kind'] == 'page']
        expected_claims = [c for output in checkpoint['outputs'] for c in output['claims']]
        if (expected_facts != actual_facts or len([i for i in candidates if i['kind'] == 'fact']) != len(expected_facts)
                or any(i['kind'] not in ('fact', 'page') for i in candidates)
                or len(pages) != 1 or pages[0]['payload']['claims'] != expected_claims
                or any(i['run_id'] != run['id'] for i in candidates)):
            violations.append('candidate_publication')
    elif candidates: violations.append('partial_publication')
    timing = read_extraction_timing(artifact, store, run['id'])
    expected_timing = ({'available': True, 'summary': timing['summary'], 'status': timing['status'], 'error_code': timing['error_code']}
                       if timing['available'] else {'available': False})
    if row['document_timing'] != expected_timing: raise ValueError('timing_mismatch')
    expected_usage = set()
    for attempt in values:
        if attempt['admission_state'] != 'finished': continue
        folder = artifact/'runs'/run['id']/str(attempt['chunk'])
        if attempt['repair']: folder /= 'quote-repair'
        expected_usage.add((folder/'usage.json').relative_to(artifact).as_posix())
        metrics = call_metrics(folder, 'claude', run['model'])
        if any(attempt.get(k) != v for k, v in metrics.items()): raise ValueError('call_metrics_mismatch')
        if metrics['usage_complete']:
            _, parsed, _ = decode_transport('claude', json.dumps(_read(folder/'stdout.txt')))
            if (parsed['tokens'] != metrics['tokens'] or parsed['reported_usd'] != metrics['reported_usd']
                    or resolved_model(parsed['resolved_model']) != metrics['resolved_model']):
                raise ValueError('provider_usage_mismatch')
        observation = 'unknown'; timing = metrics['cli_timing']; identity = metrics['worker_identity']
        if timing and timing['schema_version'] == 2 and identity and metrics['worker_scope'] == 'extraction_pool':
            if timing['process_reused'] is True: observation = 'reused'
            elif timing['process_reused'] is False and timing['offsets_seconds']['spawn_returned'] is not None:
                observation = 'started' if identity['generation'] == 1 else 'restarted'
        if attempt.get('process_observation') != observation: raise ValueError('lifecycle_mismatch')
        meta = _read(folder/'run.json')
        if attempt['status'] != ('success' if meta.get('status') == 'success' else 'error'): raise ValueError('call_status_mismatch')
    if {p.relative_to(artifact).as_posix() for p in artifact.rglob('usage.json')} != expected_usage:
        raise ValueError('unaccounted_calls')
    return {'complete': True, 'passed': not violations, 'violations': violations}
