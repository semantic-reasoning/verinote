"""Synthetic calls only: a run that failed or was cancelled before its first model call reads as zero calls only on its own progress marker."""
import json
import unittest
from unittest.mock import patch

from test_failed_run_detail import Base, Parallel, Sequential, extraction_response, ok
from test_large_dictionary_refusal import ServiceCase as OntologyCase
from test_large_dictionary_refusal import Stub, generated, generation_source

import verinote.service as service_module
from verinote.extraction_checkpoint import ExtractionCheckpoint
from verinote.service import Service
from verinote.usage import job_calls

KEY = 'e' * 32
MARKERS = {'extract': ('extraction-progress.json', 'approved-extraction-v1', 'source-spans-v1'),
           'ontology': ('dictionary-progress.json', 'source-spans-v1', 'approved-extraction-v1')}


def usage_of(service, store, run_id):
    usage = service.with_usage('runs', store.run(run_id))['usage']
    return usage['calls'], usage['unknown_call_count_jobs'], usage['details']


def cancel(service, run_id): service.cancel(run_id, 'running')


def fail(service, run_id): raise RuntimeError('before the first call')


class ExtractBeforeTheFirstCall(Base):
    def go(self, act, parallel=False):
        doc = self.document(2)
        real_refusal, holder, real_status = service_module.worst_refusal, {}, self.store.run_status
        def refusal(*args, **kwargs):
            act(holder['service'], holder['run_id'])
            return real_refusal(*args, **kwargs)
        def status(run_id, state, detail=''):
            holder['run_id'] = run_id
            return real_status(run_id, state, detail)
        caller = Parallel(lambda index, repairing: extraction_response(), 2, 10) if parallel else Sequential(ok)
        service = Service(self.store, self.root, caller, extraction_caller=caller) if parallel else Service(self.store, self.root, caller)
        holder['service'] = service
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        with patch.object(service_module, 'worst_refusal', refusal), patch.object(self.store, 'run_status', status):
            run = service.start(doc['id'], 'claude' if parallel else 'copilot', 'model' if parallel else 'chosen', expected_version=version,
                                expected_generation=generation, **({'extraction_concurrency': 2} if parallel else {}))
            service.pool.shutdown(wait=True)
        return service, self.store.run(run['id']), caller.calls

    def test_a_run_cancelled_during_the_size_check_shows_zero_calls(self):
        service, run, calls = self.go(cancel)
        self.assertEqual((run['status'], calls, usage_of(service, self.store, run['id'])), ('cancelled', [], (0, 0, [])))

    def test_a_run_that_fails_during_the_size_check_shows_zero_calls(self):
        service, run, calls = self.go(fail)
        self.assertEqual((run['status'], calls, usage_of(service, self.store, run['id'])), ('error', [], (0, 0, [])))

    def test_a_parallel_run_cancelled_or_failed_during_the_size_check_shows_zero_calls(self):
        for action, status in ((cancel, 'cancelled'), (fail, 'error')):
            with self.subTest(status=status):
                service, run, calls = self.go(action, parallel=True)
                self.assertEqual((run['status'], calls, usage_of(service, self.store, run['id'])), (status, [], (0, 0, [])))

    def test_a_failed_final_progress_write_leaves_the_call_count_unknown(self):
        real = ExtractionCheckpoint.write
        def write(checkpoint, name, value, limit):
            if name == 'extraction-progress.json' and value['status'] == 'error': return False
            return real(checkpoint, name, value, limit)
        with patch.object(ExtractionCheckpoint, 'write', write):
            service, run, calls = self.go(fail)
        self.assertIn('새 호출 0회', run['detail'])
        self.assertEqual((calls, usage_of(service, self.store, run['id'])[:2]), ([], (1, 1)))


class OntologyBeforeTheFirstCall(OntologyCase):
    def go(self, act):
        doc = self.register(generation_source(2))
        stub = Stub(generated)
        service = Service(self.store, self.root, stub)
        self.addCleanup(service.close)
        real = self.store.run_status
        def status(run_id, state, detail=''):
            result = real(run_id, state, detail)
            if detail.startswith('사전 생성 · 구간 1/'): act(service, run_id)
            return result
        with patch.object(self.store, 'run_status', status):
            job = service.start(doc['id'], 'copilot', 'auto', 'ontology', doc['version'], doc['generation'])
            service.pool.shutdown(wait=True)
        return service, self.store.run(job['id']), stub.calls

    def test_a_dictionary_run_cancelled_before_its_first_call_shows_zero_calls(self):
        service, run, calls = self.go(cancel)
        self.assertEqual((run['status'], calls, usage_of(service, self.store, run['id'])), ('cancelled', [], (0, 0, [])))

    def test_a_dictionary_run_that_fails_before_its_first_call_shows_zero_calls(self):
        service, run, calls = self.go(fail)
        self.assertEqual((run['status'], calls, usage_of(service, self.store, run['id'])), ('error', [], (0, 0, [])))


class ProgressMarkerAsZeroCalls(OntologyCase):
    def write_marker(self, stage, **change):
        filename, pipeline, _ = MARKERS[stage]
        folder = self.root / 'runs' / KEY
        folder.mkdir(parents=True, exist_ok=True)
        marker = {'schema_version': 1, 'pipeline': pipeline, 'run_id': KEY, 'calls_started': 0, 'status': 'error'} | change
        (folder / filename).write_text(json.dumps(marker), encoding='utf-8')

    def known(self, stage, status):
        owner = {'id': KEY, 'agent': 'copilot', 'model': '', 'stage': stage, 'status': status, 'detail': 'x'}
        return [call['call_count_known'] for call in job_calls(self.root, 'runs', owner)]

    def test_an_error_marker_with_no_calls_started_reads_as_zero_calls(self):
        for stage in MARKERS:
            self.write_marker(stage)
            for status in ('error', 'cancelled'):
                with self.subTest(stage=stage, status=status):
                    self.assertEqual(self.known(stage, status), [])

    def test_any_other_marker_or_owner_stays_unknown(self):
        for stage, (_, _, other_pipeline) in MARKERS.items():
            for label, change, status in (('running marker', {'status': 'running'}, 'error'), ('complete marker', {'status': 'complete'}, 'error'),
                                          ('a call started', {'calls_started': 1}, 'error'), ('another run', {'run_id': 'f' * 32}, 'error'),
                                          ('the other stage pipeline', {'pipeline': other_pipeline}, 'error'), ('interrupted owner', {}, 'interrupted')):
                with self.subTest(stage=stage, case=label):
                    self.write_marker(stage, **change)
                    self.assertEqual(self.known(stage, status), [False])

    def test_a_marker_without_schema_version_1_stays_unknown(self):
        for stage in MARKERS:
            for label, version in (('version 2', 2), ('boolean', True), ('string', '1')):
                with self.subTest(stage=stage, version=label):
                    self.write_marker(stage, schema_version=version)
                    self.assertEqual(self.known(stage, 'error'), [False])

    def test_no_marker_stays_unknown(self):
        for stage in MARKERS:
            with self.subTest(stage=stage):
                self.assertEqual(self.known(stage, 'error'), [False])

    def test_a_call_log_outranks_a_zero_marker(self):
        self.write_marker('extract')
        folder = self.root / 'runs' / KEY / '1'
        folder.mkdir(parents=True)
        (folder / 'usage.json').write_text('{}', encoding='utf-8')
        owner = {'id': KEY, 'agent': 'copilot', 'model': '', 'stage': 'extract', 'status': 'error', 'detail': 'x'}
        self.assertEqual([call['call_id'] for call in job_calls(self.root, 'runs', owner)], [f'runs/{KEY}/1'])


if __name__ == '__main__':
    unittest.main()
