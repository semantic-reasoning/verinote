"""The rerun sentence of an all-claims-excluded run, against what a rerun really calls."""
from test_failed_run_detail import Base, Parallel, Sequential
from test_quote_exclusions import CLAIM1, bad, reply, resp
from test_workflow import FACTS

from verinote.extraction_transport import ExtractionSpec
from verinote.service import Service

SENTENCE = '이 실행과 추출 조건(문서 버전·승인 사전·에이전트·모델·병렬 설정 등)이 같은 재실행이 모든 청크의 호환되는 복구 기록을 찾으면 새 호출 없이 같은 결과로 끝납니다.'
OTHER_MODEL = '다른 모델로 바꾸면 그 모델의 호환되는 복구 기록을 찾지 못한 청크는 새로 호출하고 찾은 청크는 검증된 청크를 재사용합니다.'


def all_claims_excluded(index, repairing): return reply(('claims.0.quote', '')) if repairing else resp([bad(CLAIM1)], FACTS[:1])


class Rerun(Base):
    def outcome(self, run): return run['status'], run['detail'].split(' · 검증 완료')[0]

    def named(self, doc, model, capacity):
        caller = Parallel(all_claims_excluded, capacity)
        caller.extraction_spec = ExtractionSpec('/fake/claude', model, capacity, 10, 1048576, 'null')
        service = Service(self.store, self.root, caller, extraction_caller=caller)
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        run = service.start(doc['id'], 'claude', model, expected_version=version, expected_generation=generation, extraction_concurrency=capacity)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), caller.calls

    def unregistered(self, doc):
        # Same agent and model as Base.parallel, so only the registration can separate the records.
        caller = Sequential(all_claims_excluded)
        service = Service(self.store, self.root, caller)
        self.addCleanup(service.close)
        version, generation = self.current(doc)
        run = service.start(doc['id'], 'claude', 'model', expected_version=version, expected_generation=generation)
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), caller.calls

    def test_same_conditions_make_no_call_and_end_alike(self):
        for label, go in (('sequential', lambda doc: self.sequential(doc, all_claims_excluded)),
                          ('parallel', lambda doc: self.parallel(doc, all_claims_excluded))):
            with self.subTest(label):
                doc = self.document()
                first, calls = go(doc)
                again, recalls = go(doc)
                self.assertEqual((len(calls), recalls), (6, []))
                self.assertIn(SENTENCE, first['detail'])
                self.assertEqual(self.outcome(again), self.outcome(first))

    def test_another_parallel_setting_or_another_registration_calls_every_chunk_again(self):
        for label, first, second, expected in (('capacity', lambda d: self.parallel(d, all_claims_excluded), lambda d: self.parallel(d, all_claims_excluded, capacity=1), [1, 1, 2, 2, 3, 3]),
                                               ('to unregistered', lambda d: self.parallel(d, all_claims_excluded), self.unregistered, [1, 1, 2, 2, 3, 3]),
                                               ('to registered', self.unregistered, lambda d: self.parallel(d, all_claims_excluded), [1, 1, 2, 2, 3, 3]),
                                               ('unregistered control', self.unregistered, self.unregistered, [])):
            with self.subTest(label):
                doc = self.document()
                first(doc)
                _, calls = second(doc)
                self.assertEqual(sorted(calls), expected)

    def test_eight_later_failed_runs_hide_the_record_so_the_sentence_is_conditional(self):
        doc = self.document()
        self.parallel(doc, all_claims_excluded)

        def stop(index, repairing): raise ValueError('stop')
        for _ in range(8): self.parallel(doc, stop, capacity=1)
        _, calls = self.parallel(doc, all_claims_excluded)
        self.assertEqual(len(calls), 6)

    def test_another_models_record_is_reused_only_when_compatible(self):
        doc = self.document()
        run, _ = self.named(doc, 'one', 2)
        self.assertIn(OTHER_MODEL, run['detail'])
        self.assertEqual(len(self.named(doc, 'two', 2)[1]), 6)
        self.assertEqual(len(self.named(doc, 'one', 1)[1]), 6)
        self.assertEqual(self.named(doc, 'one', 2)[1], [])
