"""Unpaid extraction retry and shared per-chunk deadline regressions."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_workflow as workflow
from test_workflow import (
    DICTIONARY,
    FACTS,
    SOURCE,
    approved_dictionary,
    extraction_response,
)

from verinote.agents import CLI, CLI_PROVIDERS
from verinote.extraction_checkpoint import CHECKPOINT_LIMIT
from verinote.job_transport import JobTransport, capture_options
from verinote.progress import analysis_progress
from verinote.service import Service
from verinote.store import Store
from verinote.transports import ExecutionOptions
from verinote.usage import atomic_json, job_calls


class FakeCLI(CLI):
    def __init__(self, outputs, **kwargs):
        super().__init__(**kwargs)
        self.outputs = iter(outputs)
        self.calls = []

    def complete(self, agent, model, prompt, folder, *, phase=None):
        self.calls.append({'folder': folder, 'timeout': self.effective_timeout(agent),
                           'payload': json.loads(prompt.split('입력(JSON):\n')[1])})
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output(self) if callable(output) else copy.deepcopy(output)


class ExtractionRecovery(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def document(self, count=2):
        source = SOURCE.ljust(8000) * (count - 1) + SOURCE
        doc = self.store.register('retry', 'retry.md', source.encode(), source)
        approved_dictionary(self.store, doc['id'])
        return doc

    def execute(self, doc, outputs, *, agent='copilot', model='chosen', cli=None):
        cli = cli if cli is not None else FakeCLI(outputs)
        service = Service(self.store, self.root, cli)
        run = service.start(doc['id'], agent, model, expected_version=doc['version'],
                            expected_generation=doc['generation'])
        service.pool.shutdown(wait=True)
        return self.store.run(run['id']), cli.calls

    def checkpoint(self, run):
        return self.root / 'runs' / run['id'] / 'extraction-checkpoint.json'

    def test_repeated_failures_resume_failed_chunk_and_publish_once(self):
        doc = self.document(3)
        first, calls = self.execute(doc, [extraction_response(), TimeoutError('timeout')])
        self.assertEqual(first['status'], 'error', first['detail'])
        self.assertEqual([c['payload']['offset'] for c in calls], [0, 8000])
        self.assertIn('청크 2', first['detail'])
        self.assertEqual(len(json.loads(self.checkpoint(first).read_text(encoding='utf-8'))['outputs']), 1)
        self.assertEqual([item['kind'] for item in self.store.document(doc['id'])['items']], ['ontology'])

        progress = []
        def second_chunk(cli):
            run_id = cli.calls[-1]['folder'].parent.name
            progress.append(analysis_progress(self.root, self.store, run_id))
            return extraction_response()
        second, calls = self.execute(doc, [second_chunk, TimeoutError('timeout again')])
        self.assertEqual(second['status'], 'error', second['detail'])
        self.assertEqual([c['payload']['offset'] for c in calls], [8000, 16000])
        self.assertEqual(progress[0]['resumed_chars'], 8000)
        self.assertIn('1청크 재사용', second['detail'])
        self.assertEqual(len(json.loads(self.checkpoint(second).read_text(encoding='utf-8'))['outputs']), 2)

        final, calls = self.execute(doc, [extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([c['payload']['offset'] for c in calls], [16000])
        self.assertIn('2청크 재사용', final['detail'])
        items = [item for item in self.store.document(doc['id'])['items'] if item['kind'] != 'ontology']
        self.assertEqual(len(items), 3)
        self.assertTrue(all(item['status'] == 'pending' and item['run_id'] == final['id'] for item in items))
        self.assertEqual(len(next(item for item in items if item['kind'] == 'page')['payload']['claims']), 3)
        self.assertEqual(self.store.snapshot()['facts'], [])

    def test_restart_reuses_interrupted_run_after_first_retry_call_fails(self):
        doc = self.document()
        first, _ = self.execute(doc, [extraction_response(), TimeoutError('timeout')])
        self.store.run_status(first['id'], 'running')
        Store(self.store.path).recover()
        second, calls = self.execute(doc, [TimeoutError('timeout')])
        self.assertEqual([c['payload']['offset'] for c in calls], [8000])
        self.assertEqual(second['status'], 'error')
        self.assertEqual(len(json.loads(self.checkpoint(second).read_text(encoding='utf-8'))['outputs']), 1)
        final, calls = self.execute(doc, [extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([c['payload']['offset'] for c in calls], [8000])

    def test_invalid_chunk_is_not_checkpointed(self):
        doc = self.document()
        invalid = extraction_response()
        invalid['facts'][0]['predicate'] = 'unknown'
        failed, _ = self.execute(doc, [extraction_response(), invalid])
        self.assertEqual(failed['status'], 'error')
        self.assertEqual(len(json.loads(self.checkpoint(failed).read_text(encoding='utf-8'))['outputs']), 1)
        final, calls = self.execute(doc, [extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([c['payload']['offset'] for c in calls], [8000])

    def test_corrupt_or_incompatible_checkpoint_restarts_with_reason(self):
        for mutate in (
            lambda value: value['contract'].update(prompt='different'),
            lambda value: value['contract'].update(text_sha256='different'),
            lambda value: value['outputs'][0]['claims'][0].update(quote='not in source'),
            lambda value: value.update(outputs=[]),
            lambda value: value.update(schema_version=True),
        ):
            with self.subTest(mutation=mutate):
                doc = self.document()
                first, _ = self.execute(doc, [extraction_response(), TimeoutError('timeout')])
                value = json.loads(self.checkpoint(first).read_text(encoding='utf-8'))
                mutate(value)
                atomic_json(self.checkpoint(first), value)
                final, calls = self.execute(doc, [extraction_response(), extraction_response()])
                self.assertEqual(final['status'], 'success', final['detail'])
                self.assertEqual([c['payload']['offset'] for c in calls], [0, 8000])
                self.assertIn('호환·검증 조건', final['detail'])

    def test_timeout_increase_reuses_older_validated_checkpoint(self):
        doc = self.document()
        first, _ = self.execute(doc, [extraction_response(), TimeoutError('timeout')])
        value = json.loads(self.checkpoint(first).read_text(encoding='utf-8'))
        value['contract']['chunk_timeout'] = 180
        atomic_json(self.checkpoint(first), value)
        final, calls = self.execute(doc, [extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual([call['payload']['offset'] for call in calls], [8000])
        self.assertIn('1청크 재사용', final['detail'])

    def test_dictionary_revision_provider_model_and_document_version_must_match(self):
        for change in ('dictionary', 'agent', 'model', 'version'):
            with self.subTest(change=change):
                doc = self.document()
                self.execute(doc, [extraction_response(), TimeoutError('timeout')])
                agent, model = 'copilot', 'chosen'
                if change == 'dictionary':
                    dictionary = next(item for item in self.store.document(doc['id'])['items'] if item['kind'] == 'ontology')
                    self.store.review(dictionary['id'], dictionary['revision'], 'revise', 'R', 'change', DICTIONARY)
                    self.store.review(dictionary['id'], dictionary['revision'] + 1, 'approved', 'R', 'approve')
                elif change == 'agent':
                    agent = 'agy'
                elif change == 'model':
                    model = 'other'
                else:
                    doc = self.store.register('retry', 'retry.md', doc['text'].encode(), doc['text'],
                                              doc['id'], doc['version'], doc['generation'])
                    approved_dictionary(self.store, doc['id'])
                final, calls = self.execute(doc, [extraction_response(), extraction_response()], agent=agent, model=model)
                self.assertEqual(final['status'], 'success', final['detail'])
                self.assertEqual([c['payload']['offset'] for c in calls], [0, 8000])

    def test_final_save_failure_can_resume_without_new_calls_or_usage(self):
        doc = self.document()
        with patch.object(self.store, 'finish_run', side_effect=ValueError('synthetic publication failure')):
            first, _ = self.execute(doc, [extraction_response(), extraction_response()])
        self.assertEqual(first['status'], 'error')
        self.assertEqual(len(json.loads(self.checkpoint(first).read_text(encoding='utf-8'))['outputs']), 2)
        final, calls = self.execute(doc, [])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertEqual(calls, [])
        self.assertEqual(job_calls(self.root, 'runs', final), [])
        self.assertEqual(analysis_progress(self.root, self.store, final['id'])['percent'], 100)

    def test_checkpoint_write_failure_is_visible_and_does_not_block_publication(self):
        doc = self.document()
        def write(path, value):
            if Path(path).name == 'extraction-checkpoint.json':
                raise OSError('disk unavailable')
            atomic_json(path, value)
        with patch('verinote.extraction_checkpoint.atomic_json', side_effect=write):
            final, _ = self.execute(doc, [extraction_response(), extraction_response()])
        self.assertEqual(final['status'], 'success', final['detail'])
        self.assertIn('복구 기록 저장 실패', final['detail'])
        self.assertFalse(self.checkpoint(final).exists())

    def test_missing_and_oversized_checkpoint_are_not_reused(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                doc = self.document()
                first, _ = self.execute(doc, [extraction_response(), TimeoutError('timeout')])
                if oversized:
                    with self.checkpoint(first).open('wb') as stream:
                        stream.truncate(CHECKPOINT_LIMIT + 1)
                else:
                    self.checkpoint(first).unlink()
                final, calls = self.execute(doc, [extraction_response(), extraction_response()])
                self.assertEqual(final['status'], 'success', final['detail'])
                self.assertEqual([c['payload']['offset'] for c in calls], [0, 8000])
                self.assertIn('이전 추출 복구 기록', final['detail'])

    def test_repairs_share_chunk_deadline_and_overdue_output_is_not_saved(self):
        for repair_elapsed in (10, 51):
            with self.subTest(repair_elapsed=repair_elapsed):
                doc = self.document(1)
                clock = [1000]
                def broken(cli):
                    clock[0] += 310  # noqa: B023 (intentional: same-iteration clock)
                    output = extraction_response()
                    output['facts'][0]['quote'] = 'absent quote'
                    return output
                def repair(cli):
                    clock[0] += repair_elapsed  # noqa: B023 (intentional: same-iteration clock, repair_elapsed)
                    return {'repairs': [{'path': 'facts.0.quote', 'quote': FACTS[0]['quote']}]}
                with patch('verinote.service.time.monotonic', side_effect=lambda: clock[0]):  # noqa: B023 (intentional: same-iteration clock)
                    result, calls = self.execute(doc, [broken, repair])
                self.assertEqual([c['timeout'] for c in calls], [360, 50])
                self.assertEqual(result['status'], 'success' if repair_elapsed == 10 else 'error', result['detail'])
                if repair_elapsed > 50:
                    self.assertIn('360초', result['detail'])
                    self.assertFalse(self.checkpoint(result).exists())

    def test_deadline_restarts_for_each_chunk_and_rejects_late_initial_response(self):
        for elapsed in (350, 361):
            with self.subTest(elapsed=elapsed):
                doc = self.document()
                clock = [1000]
                def response(cli):
                    clock[0] += elapsed  # noqa: B023 (intentional: same-iteration clock, elapsed)
                    return extraction_response()
                with patch('verinote.service.time.monotonic', side_effect=lambda: clock[0]):  # noqa: B023 (intentional: same-iteration clock)
                    result, calls = self.execute(doc, [response, response])
                if elapsed == 350:
                    self.assertEqual(result['status'], 'success', result['detail'])
                    self.assertEqual([call['timeout'] for call in calls], [360, 360])
                else:
                    self.assertEqual(result['status'], 'error')
                    self.assertEqual(len(calls), 1)
                    self.assertFalse(self.checkpoint(result).exists())

    def test_reused_prefix_does_not_bypass_dictionary_approval_at_publication(self):
        doc = self.document()
        self.execute(doc, [extraction_response(), TimeoutError('timeout')])
        dictionary = next(item for item in self.store.document(doc['id'])['items'] if item['kind'] == 'ontology')
        def revoke(cli):
            self.store.review(dictionary['id'], dictionary['revision'], 'pending', 'R', 'recheck')
            return extraction_response()
        result, calls = self.execute(doc, [revoke])
        self.assertEqual(result['status'], 'error')
        self.assertIn('사전이 변경', result['detail'])
        self.assertEqual([call['payload']['offset'] for call in calls], [8000])
        self.assertEqual([item['kind'] for item in self.store.document(doc['id'])['items']], ['ontology'])


class ExtractionDeadlines(unittest.TestCase):
    def test_limit_preserves_shorter_overrides_and_other_phases(self):
        for agent in CLI_PROVIDERS:
            for override in (None, 600, 30, 0):
                with self.subTest(agent=agent, override=override):
                    cli = CLI(timeout=override)
                    previous = cli.effective_timeout(agent)
                    limited = cli.limited_timeout(agent, 360)
                    self.assertEqual(limited.effective_timeout(agent), 360 if override is None else min(override, 360))
                    self.assertEqual(cli.effective_timeout(agent), previous)
                    self.assertEqual(cli.effective_timeout(agent, 'ontology'), 600 if override is None else override)
        options = ExecutionOptions(max_tokens=256, timeout=600)
        cli = CLI(options=options)
        limited = cli.limited_timeout('openrouter', 360)
        self.assertEqual(limited.execution_options('openrouter'), ExecutionOptions(max_tokens=256, timeout=360))
        self.assertIs(cli.options, options)

    def test_http_job_snapshot_is_unchanged_by_chunk_and_repair_caps(self):
        cli = CLI(options=ExecutionOptions(max_tokens=512, timeout=600))
        snapshot = capture_options(cli)
        transport = JobTransport(cli, snapshot)
        limited = transport.limited_timeout('openrouter', 360)
        repair = limited.limited_timeout('openrouter', 25)
        self.assertEqual(transport.effective_timeout('openrouter'), 600)
        self.assertEqual(limited.effective_timeout('openrouter'), 360)
        self.assertEqual(repair.effective_timeout('openrouter'), 25)
        self.assertEqual(repair.effective_timeout('openrouter', 'ontology'), 600)
        self.assertEqual(repair.options['max_tokens'], 512)
        self.assertEqual(snapshot, capture_options(cli))

    def test_limited_cli_enforces_deadline_and_records_timeout(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            for index, agent in enumerate(CLI_PROVIDERS):
                proc = Mock(pid=12345, returncode=-9)
                proc.poll.return_value = None
                with self.subTest(agent=agent), \
                     patch('verinote.agents.executable', return_value='synthetic-cli'), \
                     patch('verinote.agents.subprocess.Popen', return_value=proc), \
                     patch('verinote.agents.time.monotonic', side_effect=[1000, 1361]), \
                     patch('verinote.agents._kill_process_tree') as kill:
                    folder = Path(tmp) / str(index)
                    with self.assertRaisesRegex(TimeoutError, '360초'):
                        CLI(persistent=False).limited_timeout(agent, 360).complete(agent, '', 'synthetic', folder)
                    kill.assert_called_once_with(proc)
                    meta = json.loads((folder / 'run.json').read_text(encoding='utf-8'))
                    self.assertEqual(meta['timeout_seconds'], 360)
                    self.assertEqual(meta['status'], 'error')
