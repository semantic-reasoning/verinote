"""Synthetic usage records only: renderUsage from app.js, given a usage summary whose calls is 0 (no started call recorded and no job
of unknown call count), shows one zero-total line instead of the four 미확인 metric lines and leaves 미확인은 0이 아니며 out of the
note; a summary with calls above 0, an unknown-count job included, renders the metric lines and the note as before. The summaries
are built by the real usage.job_calls and usage.summarize; the usage JSON itself is not changed."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import test_workflow as workflow
from test_usage import record
from test_workflow import legacy_run

from verinote.service import Service
from verinote.store import QUESTION_QUEUED_CANCEL
from verinote.usage import atomic_json, job_calls, summarize

ROOT = Path(__file__).resolve().parents[1]
KEY = 'a' * 32
ZERO = '<p>시작한 호출의 기록이 없어 토큰·비용·크레딧 합계는 0입니다.</p>'
MISSING_LOGS = '보존된 호출 기록의 합계이며, 누락된 로그의 사용량은 포함할 수 없습니다.'
NOT_STARTED = '시작하지 않은 호출은 모델 요청을 보내기 전에 실패한 호출(CLI 실행 파일 없음·프로세스 시작 실패·연결 실패 등)로, 확인된 호출과 사용량 합계에 넣지 않습니다.'
NOTE = ('<p class="muted">입력에는 캐시, 출력에는 추론이 포함됩니다. 보존된 호출 기록의 합계이며, 누락된 로그의 사용량은 포함할 수 없습니다. '
        '미확인은 0이 아니며, 보고 비용과 추정 비용은 합산하지 않습니다. 계정의 실제 청구액·잔액과는 다를 수 있습니다. ' + NOT_STARTED + '</p>')


def render_usage(usage):
    app = (ROOT / 'verinote/web/app.js').read_text()
    source = ('const esc=String;' + app[app.index('function usageStage('):app.index('function downloadJSON(')]
              + f'\nprocess.stdout.write(renderUsage({json.dumps(usage)}));')
    return subprocess.check_output(['node'], input=source, text=True)


def before_details(html):
    """The box's headline, totals and note: everything before the per-call <details> (the whole box when it has none)."""
    return html.split('<details>', 1)[0]


class ZeroCallBox(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(path, value)

    def detail(self, kind, owner):
        """The run/question detail usage shape (Service.with_usage): summarize(calls) plus the calls as details."""
        calls = job_calls(self.root, kind, owner)
        return summarize(calls) | {'details': calls}

    def assert_zero_box(self, html):
        head = before_details(html)
        self.assertIn(ZERO, head)
        self.assertNotIn('미확인', head)
        self.assertIn(MISSING_LOGS, head)
        self.assertIn(NOT_STARTED, head)

    def test_a_question_cancelled_while_queued_shows_zero_totals_and_no_unknown_before_the_details(self):
        usage = self.detail('questions', {'id': KEY, 'agent': 'claude', 'model': '', 'status': 'cancelled',
                                          'error': QUESTION_QUEUED_CANCEL, 'call_manifest': []})
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls'], usage['tokens']['input']),
                         (0, 0, 0, {'value': None, 'known_calls': 0}))
        html = render_usage(usage)
        self.assertIn('<p>확인된 호출 0회</p>', html)
        self.assert_zero_box(html)
        self.assertNotIn('<details>', html)

    def test_a_run_with_only_a_not_started_local_cli_call_shows_zero_totals_and_no_unknown_before_the_details(self):
        self.write(f'runs/{KEY}/1/usage.json', {'schema_version': 1, 'agent': 'claude', 'status': 'not_started', 'tokens': {}})
        usage = self.detail('runs', {'id': KEY, 'agent': 'claude', 'model': '', 'status': 'error', 'stage': 'extract'})
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls'], usage['tokens']['input']),
                         (0, 0, 1, {'value': None, 'known_calls': 0}))
        html = render_usage(usage)
        self.assertIn('<p>확인된 호출 0회 · 시작하지 않은 호출 1개</p>', html)
        self.assert_zero_box(html)
        self.assertIn('<details>', html)

    def test_a_run_with_only_a_not_started_openrouter_call_shows_zero_totals_and_records_the_per_call_diagnostic(self):
        """The totals before <details> carry no 미확인. The per-call reasoning diagnostic inside <details> still prints 미확인 for the
        call that never sent its request; that is follow-up #906, recorded here as it is, not fixed by #898."""
        call = 'e' * 32
        self.write(f'runs/{KEY}/1/run.json', {'agent': 'openrouter', 'transport': 'http', 'provider_call_id': call, 'status': 'error',
                                              'transport_status': 'not_started', 'transmission_state': 'not_sent'})
        self.write(f'runs/{KEY}/1/usage.json', {'schema_version': 1, 'agent': 'openrouter', 'status': 'error', 'provider_call_id': call,
                                                'tokens': {}})
        usage = self.detail('runs', {'id': KEY, 'agent': 'openrouter', 'model': 'x/y', 'status': 'error', 'stage': 'extract'})
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls']), (0, 0, 1))
        html = render_usage(usage)
        self.assert_zero_box(html)
        details = html.split('<details>', 1)[1]
        self.assertIn('보고된 출력 미확인 · 그중 추론 미확인 · 비추론 출력 미확인 토큰', details)

    def test_the_overview_of_no_jobs_shows_zero_totals(self):
        html = render_usage(summarize([]))
        self.assertIn('<p>확인된 호출 0회</p>', html)
        self.assert_zero_box(html)


class StartedOrUnknownCallBox(unittest.TestCase):
    setUp = ZeroCallBox.setUp
    write = ZeroCallBox.write
    detail = ZeroCallBox.detail

    def test_a_run_of_unknown_call_count_keeps_the_unknown_metric_lines(self):
        usage = self.detail('runs', {'id': KEY, 'agent': 'claude', 'model': '', 'status': 'error', 'stage': 'extract'})
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs']), (1, 1))
        html = render_usage(usage)
        self.assertIn('<p>확인된 호출 0회 · 호출 횟수 미확인 작업 1개 · 불완전 기록 1개</p>', html)
        self.assertIn('<p>입력 미확인 · 출력 미확인</p>', html)
        self.assertIn('<p>크레딧: 미확인</p>', html)
        self.assertIn(NOTE, html)
        self.assertNotIn(ZERO, html)

    def test_a_run_with_one_started_call_without_token_report_renders_the_metric_lines_and_full_note(self):
        self.write(f'runs/{KEY}/1/usage.json', {'schema_version': 1, 'agent': 'claude', 'status': 'success', 'tokens': {}})
        usage = self.detail('runs', {'id': KEY, 'agent': 'claude', 'model': '', 'status': 'success', 'stage': 'extract'})
        self.assertEqual((usage['calls'], usage['unknown_call_count_jobs']), (1, 0))
        head = before_details(render_usage(usage))
        self.assertIn('<p>확인된 호출 1회 · 불완전 기록 1개</p><p>입력 미확인 · 출력 미확인</p>'
                      '<p>출력 중 추론 미확인 · 보고된 비추론 출력 미확인</p><p>제공자 보고 비용 미확인 · 설정 단가 추정 비용 미확인</p>'
                      '<p>크레딧: 미확인</p>' + NOTE, head)
        self.assertNotIn(ZERO, head)


class UsageReportGroups(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown

    def test_a_totals_group_of_only_not_started_calls_shows_zero_while_the_summary_keeps_unknown(self):
        """One legacy run: a started call without token report (legacy group) and a not-started quote-repair call (quote_repair group)."""
        run = legacy_run(self.store, self.doc['id'])
        self.store.run_status(run['id'], 'error')
        folder = self.root / 'runs' / run['id'] / '1'
        folder.mkdir(parents=True)
        atomic_json(folder / 'usage.json', record() | {'tokens': {}})
        (folder / 'quote-repair').mkdir()
        atomic_json(folder / 'quote-repair' / 'usage.json', record() | {'status': 'not_started', 'tokens': {}})
        service = Service(self.store, self.root)
        self.addCleanup(service.close)
        report = service.usage_report()
        groups = {g['stage']: g['usage'] for g in report['groups']}
        self.assertEqual((report['summary']['calls'], report['summary']['not_started_calls']), (1, 1))
        self.assertEqual({stage: (u['calls'], u['not_started_calls']) for stage, u in groups.items()},
                         {'legacy': (1, 0), 'quote_repair': (0, 1)})
        summary = render_usage(report['summary'])
        self.assertIn('<p>입력 미확인 · 출력 미확인</p>', summary)
        self.assertNotIn(ZERO, summary)
        self.assertIn(ZERO, render_usage(groups['quote_repair']))
        self.assertNotIn('미확인', render_usage(groups['quote_repair']))


if __name__ == '__main__':
    unittest.main()
