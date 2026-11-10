"""Synthetic records only: a failed OpenRouter job whose sent call may have left no log at all. A run's trusted error marker counts its
call attempts; more attempts than logged entries (started or not), or an untrusted marker with only not-started records, adds one entry
of unknown call count. A question without a call manifest is checked for a round below the highest present round that has no log.
Transports that write their first log before the request are left alone."""
import tempfile
import unittest
from pathlib import Path

from test_zero_call_usage_display import render_usage

from verinote.usage import LOST_ATTEMPT, atomic_json, job_calls, summarize

KEY = 'd' * 32
CALL = 'e' * 32
MARKERS = {'extract': ('extraction-progress.json', 'approved-extraction-v1'), 'ontology': ('dictionary-progress.json', 'source-spans-v1')}


class LostAttempts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, value)

    def call(self, kind, path, sent, agent='openrouter'):
        """One logged call: an OpenRouter call whose run.json records not_sent (or a received success), or a local CLI usage record."""
        folder = f'{kind}/{KEY}/{path}'
        if agent != 'openrouter':
            self.write(folder + '/usage.json', {'schema_version': 1, 'agent': agent, 'status': 'success' if sent else 'not_started', 'tokens': {}})
            return
        state = ({'status': 'success', 'transport_status': 'success', 'transmission_state': 'received'} if sent
                 else {'status': 'error', 'transport_status': 'not_started', 'transmission_state': 'not_sent'})
        self.write(folder + '/run.json', {'agent': 'openrouter', 'transport': 'http', 'provider_call_id': CALL} | state)
        self.write(folder + '/usage.json', {'schema_version': 1, 'agent': 'openrouter', 'status': state['status'], 'provider_call_id': CALL,
                                            'tokens': {}})

    def marker(self, stage, calls_started, status='error'):
        filename, pipeline = MARKERS[stage]
        self.write(f'runs/{KEY}/{filename}', {'schema_version': 1, 'pipeline': pipeline, 'run_id': KEY, 'status': status,
                                              'calls_started': calls_started})

    def run_usage(self, stage='extract', agent='openrouter', status='error'):
        calls = job_calls(self.root, 'runs', {'id': KEY, 'agent': agent, 'model': 'x/y', 'stage': stage, 'status': status, 'detail': 'x'})
        return summarize(calls) | {'details': calls}

    def question_usage(self, **extra):
        owner = {'id': KEY, 'agent': 'openrouter', 'model': 'x/y', 'status': 'error', 'error': 'x'} | extra
        calls = job_calls(self.root, 'questions', owner)
        return summarize(calls) | {'details': calls}

    def counts(self, usage):
        """(calls, unknown_call_count_jobs, not_started_calls) and whether the lost-attempt warning is on an entry."""
        lost = any(LOST_ATTEMPT in c['warnings'] for c in usage['details'])
        return usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls'], lost

    def issue_layout(self, stage='extract', calls_started=2):
        """Call folder 1 has no log; call folder 2 has a not-sent record. calls_started None writes no marker."""
        self.call('runs', '2', sent=False)
        if calls_started is not None:
            self.marker(stage, calls_started)

    def test_more_attempts_than_logged_entries_add_one_unknown_count_entry(self):
        self.issue_layout()
        self.assertEqual(self.counts(self.run_usage()), (1, 1, 1, True))

    def test_attempts_equal_to_logged_entries_add_no_entry(self):
        self.issue_layout(calls_started=1)
        self.assertEqual(self.counts(self.run_usage()), (0, 0, 1, False))

    def test_no_marker_with_only_not_started_records_adds_one_unknown_count_entry(self):
        self.issue_layout(calls_started=None)
        self.assertEqual(self.counts(self.run_usage()), (1, 1, 1, True))

    def test_a_running_marker_is_not_trusted_and_adds_one_unknown_count_entry(self):
        self.call('runs', '2', sent=False)
        self.marker('extract', 1, status='running')
        self.assertEqual(self.counts(self.run_usage()), (1, 1, 1, True))

    def test_a_negative_calls_started_marker_is_not_trusted_and_adds_one_unknown_count_entry(self):
        self.issue_layout(calls_started=-1)
        self.assertEqual(self.counts(self.run_usage()), (1, 1, 1, True))

    def test_a_trusted_excess_adds_one_entry_even_with_a_started_record(self):
        self.call('runs', '1', sent=True)
        self.call('runs', '3', sent=False)
        self.marker('extract', 3)
        self.assertEqual(self.counts(self.run_usage()), (2, 1, 1, True))

    def test_a_started_record_and_no_marker_add_no_unknown_count_entry(self):
        """The residual gap: without a marker, a run with a started record shows only its logged calls."""
        self.call('runs', '1', sent=True)
        self.call('runs', '3', sent=False)
        self.assertEqual(self.counts(self.run_usage()), (1, 0, 1, False))

    def test_a_local_cli_run_with_an_excess_adds_no_entry(self):
        self.call('runs', '2', sent=False, agent='claude')
        self.marker('extract', 2)
        self.assertEqual(self.counts(self.run_usage(agent='claude')), (0, 0, 1, False))

    def test_a_dictionary_run_reads_its_own_marker(self):
        for calls_started, expected in ((2, (1, 1, 1, True)), (1, (0, 0, 1, False))):
            with self.subTest(calls_started=calls_started):
                self.issue_layout('ontology', calls_started)
                self.assertEqual(self.counts(self.run_usage('ontology')), expected)

    def test_cancelled_and_interrupted_runs_are_checked_and_a_success_run_is_not(self):
        self.issue_layout(calls_started=None)
        for status, lost in (('cancelled', True), ('interrupted', True), ('success', False)):
            with self.subTest(status=status):
                self.assertEqual(self.counts(self.run_usage(status=status))[3], lost)

    def test_a_question_without_a_manifest_and_a_missing_earlier_round_adds_one_unknown_count_entry(self):
        self.call('questions', 'llm-2', sent=False)
        self.assertEqual(self.counts(self.question_usage()), (1, 1, 1, True))

    def test_a_question_without_a_manifest_whose_first_round_did_not_start_adds_no_entry(self):
        self.call('questions', 'llm-1', sent=False)
        self.assertEqual(self.counts(self.question_usage()), (0, 0, 1, False))

    def test_a_context_folder_counts_its_round_as_present(self):
        """Round 1 logged under a context-1-* folder, round 2 not sent: no round is missing."""
        (self.root / 'questions' / KEY / 'context-1-decision').mkdir(parents=True)
        self.call('questions', 'llm-2', sent=False)
        self.assertEqual(self.counts(self.question_usage()), (0, 0, 1, False))

    def test_a_question_with_a_manifest_adds_no_lost_attempt_entry(self):
        self.call('questions', 'llm-2', sent=False)
        usage = self.question_usage(call_manifest=[{'path': 'llm-2', 'stage': 'question', 'status': 'failed'}])
        self.assertEqual(self.counts(usage), (0, 0, 1, False))

    def test_the_usage_box_shows_the_unknown_count_job_not_the_zero_total_line(self):
        self.issue_layout()
        html = render_usage(self.run_usage())
        self.assertIn('호출 횟수 미확인 작업 1개', html)
        self.assertIn('시작하지 않은 호출 1개', html)
        self.assertIn(LOST_ATTEMPT, html)
        self.assertNotIn('시작한 호출의 기록이 없어', html)


if __name__ == '__main__':
    unittest.main()
