"""Synthetic callers only: a question that ended before its first model call reads as zero calls on its own call manifest, or, for a
question without one, on the queued-cancel text; an ended question whose dispatched or failed manifest entry has no readable call log
reads as call-count-unknown, never as a confirmed call or as zero."""
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import test_workflow as workflow
from test_assistant import Fake, answer
from test_usage import record

from verinote.service import Service
from verinote.store import Store
from verinote.usage import MAX_METADATA, UNCONFIRMED, atomic_json, job_calls, summarize

ROOT = Path(__file__).resolve().parents[1]
QUESTION = '주문에서 원장까지 경로는?'
KEY = 'b' * 32
NO_LOG = '호출 계획 목록에 읽을 수 있는 호출 로그가 없는 항목이 있어, 그 호출이 시작됐는지와 사용량을 확인할 수 없습니다.'


class Logged(Fake):
    """Writes a usage.json into the call folder before answering, as the transports write their first call log before the request."""
    def complete(self, agent, model, prompt, folder):
        Path(folder).mkdir(parents=True, exist_ok=True)
        atomic_json(Path(folder) / 'usage.json', record())
        return super().complete(agent, model, prompt, folder)


def tails(calls):
    return [(c['call_id'].rsplit('/', 1)[1], c['call_count_known']) for c in calls]


class Base(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    extract = workflow.Workflow.extract
    approve = workflow.Workflow.approve

    def ask(self, fake, *patches):
        self.approve(self.extract())
        service = Service(self.store, self.root, fake)
        self.addCleanup(service.close)
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        job = service.ask({'question': QUESTION})
        service.pool.wait_idle()
        for p in patches: p.stop()
        stored = self.store.question(job['id'])
        usage = service.with_usage('questions', stored)['usage']
        self.usage = usage
        return stored, [c['status'] for c in stored['call_manifest']], (usage['calls'], usage['unknown_call_count_jobs'])

    def at_first_dispatch(self, action):
        """Runs action just before the first save whose job holds a dispatched call record."""
        real, done = Store.save_question, []
        def save(store, job):
            if not done and any(c['status'] == 'dispatched' for c in job.get('call_manifest', [])):
                done.append(True); action(store, job)
            return real(store, job)
        return patch.object(Store, 'save_question', save)


class BeforeTheFirstCall(Base):
    def test_a_question_whose_assistant_setup_raises_stores_an_empty_manifest_and_shows_zero_calls(self):
        fake = Fake([answer()])
        stored, manifest, usage = self.ask(fake, patch('verinote.assistant.meaning_context', side_effect=ValueError('setup')))
        self.assertEqual((stored['status'], fake.calls, manifest, usage), ('error', [], [], (0, 0)))

    def test_a_question_whose_submit_raises_stores_an_empty_manifest_and_shows_zero_calls(self):
        self.approve(self.extract())
        service = Service(self.store, self.root, Fake([answer()]))
        self.addCleanup(service.close)
        with patch.object(service.pool, 'submit', side_effect=RuntimeError('thread')), self.assertRaises(RuntimeError):
            service.ask({'question': QUESTION})
        [stored] = self.store.questions()
        usage = service.with_usage('questions', stored)['usage']
        self.assertEqual((stored['status'], stored['call_manifest'], usage['calls'], usage['unknown_call_count_jobs']), ('error', [], 0, 0))

    def test_a_cancel_committed_before_the_first_dispatched_save_leaves_a_planned_record_and_shows_zero_calls(self):
        fake = Fake([answer()])
        stored, manifest, usage = self.ask(fake, self.at_first_dispatch(lambda store, job: store.cancel_question(job['id'], 'running')))
        self.assertEqual((stored['status'], fake.calls, manifest, usage), ('cancelled', [], ['planned'], (0, 0)))

    def test_a_failed_first_dispatched_save_leaves_the_record_planned_and_shows_zero_calls(self):
        def refuse(store, job): raise OSError('disk')
        fake = Fake([answer()])
        stored, manifest, usage = self.ask(fake, self.at_first_dispatch(refuse))
        self.assertEqual((stored['status'], stored['error'], fake.calls, manifest, usage), ('error', 'disk', [], ['planned'], (0, 0)))

    def test_a_question_interrupted_by_a_restart_before_its_first_call_shows_zero_calls(self):
        job = self.store.create_question(QUESTION, 'codex', '', self.store.snapshot()['id'])
        self.store.save_question(job | {'status': 'running'})
        self.store.recover()
        stored = self.store.question(job['id'])
        self.assertEqual((stored['status'], stored['call_manifest'], job_calls(self.root, 'questions', stored)), ('interrupted', [], []))

    def test_a_cancel_committed_after_a_logged_first_call_was_dispatched_shows_that_call_as_confirmed(self):
        entered, release = threading.Event(), threading.Event()
        def hold(prompt):
            entered.set()
            if not release.wait(20): raise TimeoutError('gate')
            return answer()
        self.addCleanup(release.set)
        self.approve(self.extract())
        fake = Logged([hold])
        service = Service(self.store, self.root, fake)
        self.addCleanup(service.close)
        job = service.ask({'question': QUESTION})
        self.assertTrue(entered.wait(5))
        service.cancel_question(job['id'], 'running')
        release.set()
        service.pool.wait_idle()
        stored = self.store.question(job['id'])
        usage = service.with_usage('questions', stored)['usage']
        self.assertEqual((stored['status'], len(fake.calls), [c['status'] for c in stored['call_manifest']], tails(usage['details']),
                          usage['calls'], usage['unknown_call_count_jobs']), ('cancelled', 1, ['dispatched'], [('llm-1', True)], 1, 0))

    def test_a_question_whose_first_call_fails_after_writing_its_log_shows_one_confirmed_call(self):
        def fail(prompt): raise RuntimeError('provider')
        fake = Logged([fail])
        stored, manifest, usage = self.ask(fake)
        self.assertEqual((stored['status'], len(fake.calls), manifest, tails(self.usage['details']), usage),
                         ('error', 1, ['failed'], [('llm-1', True)], (1, 0)))


class WithoutACallLog(Base):
    """An ended question's dispatched or failed manifest entry with no readable run.json or usage.json is not counted as a call; the
    question gets one call-count-unknown entry instead."""
    def test_a_question_whose_first_call_fails_without_a_log_shows_an_unknown_call_count(self):
        def fail(prompt): raise RuntimeError('provider')
        fake = Fake([fail])
        stored, manifest, usage = self.ask(fake)
        self.assertEqual((stored['status'], len(fake.calls), manifest, tails(self.usage['details']), usage),
                         ('error', 1, ['failed'], [('unknown', False)], (1, 1)))
        self.assertEqual(self.usage['details'][0]['warnings'], [UNCONFIRMED])

    def test_a_dispatched_save_that_commits_and_then_raises_with_every_later_save_failing_shows_an_unknown_call_count(self):
        real, done = Store.save_question, []
        def save(store, job):
            if done: raise OSError('later')
            if any(c['status'] == 'dispatched' for c in job.get('call_manifest', [])):
                real(store, job); done.append(True); raise OSError('after commit')
            return real(store, job)
        fake = Fake([answer()])
        stored, manifest, usage = self.ask(fake, patch.object(Store, 'save_question', save))
        self.assertEqual((stored['status'], fake.calls, manifest, tails(self.usage['details']), usage),
                         ('error', [], ['dispatched'], [('unknown', False)], (1, 1)))
        self.assertEqual(self.usage['details'][0]['warnings'], [UNCONFIRMED])

    def test_a_restart_after_a_dispatched_record_was_stored_shows_an_unknown_call_count(self):
        for status, ended in (('running', 'interrupted'), ('cancelling', 'cancelled')):
            with self.subTest(status=status):
                job = self.store.create_question(QUESTION, 'codex', '', self.store.snapshot()['id'])
                self.store.save_question(job | {'status': status, 'call_manifest': [
                    {'path': 'llm-1', 'stage': 'question', 'round': 1, 'status': 'dispatched'}]})
                self.store.recover()
                stored = self.store.question(job['id'])
                calls = job_calls(self.root, 'questions', stored)
                usage = summarize(calls)
                self.assertEqual((stored['status'], tails(calls), usage['calls'], usage['unknown_call_count_jobs']),
                                 (ended, [('unknown', False)], 1, 1))
                self.assertEqual(calls[0]['warnings'], [UNCONFIRMED])

    def test_detail_usage_report_and_rendered_usage_of_a_logged_and_a_logless_call_agree(self):
        """A logged complete llm-1 and a logless dispatched split-analysis entry, then a restart: the question detail and the usage report
        summary both count one confirmed call and one unknown job; renderUsage from app.js shows the headline, the unknown entry under the
        question stage label, and the warning sentence."""
        job = self.store.create_question(QUESTION, 'codex', '', self.store.snapshot()['id'])
        (self.root / 'questions' / job['id'] / 'llm-1').mkdir(parents=True)
        atomic_json(self.root / 'questions' / job['id'] / 'llm-1' / 'usage.json', record())
        self.store.save_question(job | {'status': 'running', 'call_manifest': [
            {'path': 'llm-1', 'stage': 'question', 'round': 1, 'status': 'complete'},
            {'path': 'context-1-1', 'stage': 'analysis_leaf', 'round': 2, 'status': 'dispatched'}]})
        self.store.recover()
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        detail = service.with_usage('questions', self.store.question(job['id']))['usage']
        report = service.usage_report()
        self.assertEqual([(c['call_id'].rsplit('/', 1)[1], c['stage'], c['call_count_known']) for c in detail['details']],
                         [('llm-1', 'question', True), ('unknown', 'question', False)])
        self.assertEqual([(u['calls'], u['unknown_call_count_jobs']) for u in (detail, report['summary'])], [(2, 1), (2, 1)])
        self.assertEqual([(g['stage'], g['usage']['calls'], g['usage']['unknown_call_count_jobs']) for g in report['groups']], [('question', 2, 1)])
        app = (ROOT / 'verinote/web/app.js').read_text()
        script = ('const esc=String;' + app[app.index('function usageStage('):app.index('function downloadJSON(')]
                  + f'\nprocess.stdout.write(renderUsage({json.dumps(detail)}));')
        html = subprocess.check_output(['node'], input=script, text=True)
        self.assertIn('<p>확인된 호출 1회 · 호출 횟수 미확인 작업 1개', html)
        self.assertIn(f'<strong>질문</strong> · unknown · 모델 식별 안 됨<p>{NO_LOG}</p>', html)


class UsageReport(Base):
    """Store.usage_owners projects the owner Service.usage_report passes to job_calls; these read that projection."""
    def owner(self, key):
        return next(o for o in self.store.usage_owners()[0] if o['id'] == key)

    def test_the_usage_report_owner_of_a_question_whose_setup_raised_reads_as_zero_calls(self):
        stored, _, _ = self.ask(Fake([answer()]), patch('verinote.assistant.meaning_context', side_effect=ValueError('setup')))
        self.assertEqual(job_calls(self.root, 'questions', self.owner(stored['id'])), [])

    def test_the_usage_report_owner_of_a_question_queued_cancelled_without_a_manifest_reads_as_zero_calls(self):
        job = self.store.create_question(QUESTION, 'agy', '', self.store.snapshot()['id'])
        job.pop('call_manifest', None)  # a question created before #870
        self.store.save_question(job)
        self.store.cancel_question(job['id'], 'queued')
        self.assertNotIn('call_manifest', self.store.question(job['id']))
        self.assertEqual(job_calls(self.root, 'questions', self.owner(job['id'])), [])

    def test_the_usage_report_counts_the_declared_split_processing_calls_of_a_question(self):
        job = self.store.create_question(QUESTION, 'agy', '', self.store.snapshot()['id'])
        paths = [('context-1-1', 'analysis_leaf'), ('context-1-decision', 'question_synthesis')]
        for path, _ in paths:
            (self.root / 'questions' / job['id'] / path).mkdir(parents=True)
            atomic_json(self.root / 'questions' / job['id'] / path / 'usage.json', record())
        self.store.save_question(job | {'status': 'success', 'call_manifest': [
            {'path': path, 'stage': stage, 'round': 1, 'status': 'complete'} for path, stage in paths]})
        calls = job_calls(self.root, 'questions', self.owner(job['id']))
        self.assertEqual([(c['call_id'].rsplit('/', 1)[1], c['call_count_known']) for c in calls],
                         [('context-1-1', True), ('context-1-decision', True)])
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        report = service.usage_report()['summary']
        self.assertEqual((report['calls'], report['unknown_call_count_jobs']), (2, 0))

    def test_the_run_list_totals_table_labels_each_question_stage_of_a_usage_report_in_korean(self):
        """Builds the usage report of a question declaring one call per question stage usage.job_calls accepts, renders loadRuns from
        app.js with it, and records the stage cell of each totals row."""
        job = self.store.create_question(QUESTION, 'agy', '', self.store.snapshot()['id'])
        paths = [('llm-1', 'question'), ('context-1-1', 'analysis_leaf'), ('context-1-3', 'analysis_merge'),
                 ('context-1-decision', 'question_synthesis')]
        labels = {'question': '질문', 'analysis_leaf': '질문 분할 분석', 'analysis_merge': '질문 이진 종합', 'question_synthesis': '질문 최종 결정'}
        for path, _ in paths:
            (self.root / 'questions' / job['id'] / path).mkdir(parents=True)
            atomic_json(self.root / 'questions' / job['id'] / path / 'usage.json', record())
        self.store.save_question(job | {'status': 'success', 'call_manifest': [
            {'path': path, 'stage': stage, 'round': 1, 'status': 'complete'} for path, stage in paths]})
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        report = service.usage_report()
        self.assertEqual(sorted(g['stage'] for g in report['groups']), sorted(stage for _, stage in paths))
        app = (ROOT / 'verinote/web/app.js').read_text()
        script = ("const elements=new Map();const $=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',onclick:null});return elements.get(s)};\n"
                  "const esc=String,providerById=()=>null,downloadJSON=()=>{},renderUsage=()=>'',renderRun=r=>r.id,watchRun=()=>{},loadCleanup=async()=>{};\n"
                  + app[app.index('function usageStage('):app.index('function usageNumber(')]
                  + app[app.index('async function loadRuns()'):app.index('function renderRun(')] + "\n"
                  f"const report={json.dumps(report)};\n"
                  "global.api=async path=>path==='/api/usage'?report:[];\n"
                  "loadRuns().then(()=>console.log(JSON.stringify([...$('#usage-overview').innerHTML.matchAll(/<\\/td><td>([^<]*)<\\/td>/g)].map(m=>m[1]))));\n")
        cells = json.loads(subprocess.check_output(['node'], input=script, text=True))
        self.assertEqual(len(cells), len(paths))
        for stage, cell in zip([g['stage'] for g in report['groups']], cells, strict=True):
            with self.subTest(stage=stage):
                self.assertEqual(cell, labels[stage])


class ManifestRule(unittest.TestCase):
    def calls(self, **owner):
        return [c['call_count_known'] for c in job_calls(Path('/nonexistent'), 'questions', {'id': KEY, 'agent': 'codex', 'model': ''} | owner)]

    def test_an_ended_question_whose_manifest_holds_only_planned_records_reads_as_zero_calls(self):
        for status in ('error', 'cancelled', 'interrupted'):
            for manifest in ([], [{'path': 'llm-1', 'stage': 'question', 'status': 'planned'}]):
                with self.subTest(status=status, manifest=manifest):
                    self.assertEqual(self.calls(status=status, call_manifest=manifest, error='x'), [])

    def test_a_question_without_a_manifest_list_stays_unknown(self):
        for manifest in ({}, None, 'planned'):
            with self.subTest(manifest=manifest):
                self.assertEqual(self.calls(status='error', call_manifest=manifest, error='x'), [False])
        self.assertEqual(self.calls(status='error', error='x'), [False])

    def test_a_manifest_entry_other_than_a_planned_record_keeps_the_count_unknown(self):
        for entry in ('planned', {'path': 'llm-1', 'stage': 'question'}, {'path': 'llm-1', 'stage': 'question', 'status': 'cached'},
                      {'path': '../x', 'stage': 'question', 'status': 'dispatched'}):
            with self.subTest(entry=entry):
                self.assertEqual(self.calls(status='error', call_manifest=[entry], error='x'), [False])

    def test_a_success_or_running_question_with_an_empty_manifest_is_not_read_as_zero_calls(self):
        for status in ('success', 'running', 'cancelling'):
            with self.subTest(status=status):
                self.assertEqual(self.calls(status=status, call_manifest=[], error=''), [False])

    def test_a_repeated_or_invalid_manifest_path_is_read_once_or_not_at_all(self):
        """A running owner, so each logless entry below that passes the path guards is counted as a call."""
        manifest = [{'path': 'context-1-1', 'stage': 'analysis_leaf', 'status': 'failed'},
                    {'path': 'context-1-1', 'stage': 'analysis_leaf', 'status': 'failed'},
                    {'path': '../outside', 'stage': 'analysis_leaf', 'status': 'failed'},
                    {'path': 'x', 'stage': 'question', 'status': 'dispatched'}]
        owner = {'id': KEY, 'agent': 'codex', 'model': '', 'status': 'running', 'error': '', 'call_manifest': manifest}
        self.assertEqual(tails(job_calls(Path('/nonexistent'), 'questions', owner)), [('context-1-1', True)])


class UnconfirmedRule(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def owner(self, status, entries, key=KEY):
        return {'id': key, 'agent': 'codex', 'model': '', 'status': status, 'error': 'x',
                'call_manifest': [{'path': p, 'stage': stage, 'round': 1, 'status': s} for p, stage, s in entries]}

    def test_an_ended_dispatched_or_failed_entry_with_either_readable_log_stays_a_confirmed_call(self):
        cases = [(s, e, n) for s in ('error', 'cancelled', 'interrupted') for e in ('dispatched', 'failed') for n in ('run.json', 'usage.json')]
        for i, (status, entry, name) in enumerate(cases):
            with self.subTest(status=status, entry=entry, name=name):
                key = f'{i:032x}'
                (self.root / 'questions' / key / 'llm-1').mkdir(parents=True)
                atomic_json(self.root / 'questions' / key / 'llm-1' / name, record())
                self.assertEqual(tails(job_calls(self.root, 'questions', self.owner(status, [('llm-1', 'question', entry)], key))),
                                 [('llm-1', True)])

    def test_an_ended_entry_whose_only_log_is_a_symlink_or_over_the_size_limit_reads_as_unknown(self):
        target = self.root / 'elsewhere.json'
        atomic_json(target, record())
        for i, kind in enumerate(('symlink', 'oversized')):
            with self.subTest(kind=kind):
                key = f'{i:032x}'
                folder = self.root / 'questions' / key / 'llm-1'
                folder.mkdir(parents=True)
                if kind == 'symlink':
                    (folder / 'usage.json').symlink_to(target)
                else:
                    (folder / 'usage.json').write_text(' ' * (MAX_METADATA + 1))
                self.assertTrue((folder / 'usage.json').is_file())
                self.assertEqual(tails(job_calls(self.root, 'questions', self.owner('error', [('llm-1', 'question', 'dispatched')], key))),
                                 [('unknown', False)])

    def test_a_logless_dispatched_entry_of_a_question_still_in_progress_or_successful_stays_a_call(self):
        for status in ('queued', 'running', 'cancelling', 'success'):
            with self.subTest(status=status):
                self.assertEqual(tails(job_calls(self.root, 'questions', self.owner(status, [('llm-1', 'question', 'dispatched')]))),
                                 [('llm-1', True)])

    def test_an_ended_logless_complete_entry_stays_a_call(self):
        for status in ('error', 'cancelled', 'interrupted'):
            with self.subTest(status=status):
                self.assertEqual(tails(job_calls(self.root, 'questions', self.owner(status, [('llm-1', 'question', 'complete')]))),
                                 [('llm-1', True)])

    def test_two_logless_entries_of_different_stages_make_one_unknown_entry_under_the_question_stage(self):
        calls = job_calls(self.root, 'questions', self.owner('error', [('context-1-1', 'analysis_leaf', 'failed'),
                                                                        ('llm-2', 'question', 'dispatched')]))
        self.assertEqual([(c['call_id'].rsplit('/', 1)[1], c['stage'], c['call_count_known'], c['warnings']) for c in calls],
                         [('unknown', 'question', False, [UNCONFIRMED])])
