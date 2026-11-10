"""Synthetic usage records only: renderUsage from app.js, given a usage summary whose calls is 0 (no started call recorded and no job
of unknown call count), shows one zero-total line instead of the four 미확인 metric lines and leaves 미확인은 0이 아니며 out of the
note; a summary with calls above 0, an unknown-count job included, renders the metric lines and the note as before. The summaries
are built by the real usage.job_calls and usage.summarize; the zero-total line changes only the rendering, not the usage JSON. A
not-started OpenRouter call (call_started False) carries no reasoning diagnostic, in the usage JSON or the box; a started one keeps
it, including one whose run.json records not_sent but which is counted as started because its usage.json is missing."""
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
from verinote.usage import NS_WARNING, atomic_json, job_calls, summarize

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

    def test_a_run_with_only_a_not_started_openrouter_call_shows_zero_totals_and_no_reasoning_diagnostic(self):
        """run.json records not_sent with a not_started or timeout transport status and usage.json is accepted, for a v1 and a v2
        request, and the run's error marker records one call attempt: the call's usage JSON has no reasoning_diagnostic key and the
        whole rendered box, <details> included, has no 미확인; the row's not_started status and warning are what explain the call."""
        call = 'e' * 32
        self.write(f'runs/{KEY}/extraction-progress.json', {'schema_version': 1, 'pipeline': 'approved-extraction-v1', 'run_id': KEY,
                                                            'status': 'error', 'calls_started': 1})
        v2 = {'adapter_contract_version': 2, 'max_tokens': 16384, 'reasoning_max_tokens': 4096, 'answer_reserve_tokens': 8192}
        for name, transport_status, request in (('v1', 'not_started', {}), ('v2', 'not_started', v2), ('v1 timeout', 'timeout', {}),
                                                ('v2 timeout', 'timeout', v2)):
            with self.subTest(name):
                self.write(f'runs/{KEY}/1/run.json', {'agent': 'openrouter', 'transport': 'http', 'provider_call_id': call,
                                                      'status': 'error', 'transport_status': transport_status,
                                                      'transmission_state': 'not_sent'} | request)
                self.write(f'runs/{KEY}/1/usage.json', {'schema_version': 1, 'agent': 'openrouter', 'status': 'error',
                                                        'provider_call_id': call, 'tokens': {}})
                usage = self.detail('runs', {'id': KEY, 'agent': 'openrouter', 'model': 'x/y', 'status': 'error', 'stage': 'extract'})
                self.assertEqual((usage['calls'], usage['unknown_call_count_jobs'], usage['not_started_calls']), (0, 0, 1))
                self.assertEqual((usage['details'][0]['call_started'], usage['details'][0]['status']), (False, 'not_started'))
                self.assertNotIn('reasoning_diagnostic', usage['details'][0])
                html = render_usage(usage)
                self.assert_zero_box(html)
                details = html.split('<details>', 1)[1]
                for text in ('<section class="panel"', '보고된 출력', '종료 사유', '추론 요청 예산'):
                    self.assertNotIn(text, details)
                self.assertNotIn('미확인', html)
                self.assertIn(' · not_started · ', details)
                self.assertIn(NS_WARNING, details)

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

    def test_a_started_openrouter_call_keeps_its_reasoning_diagnostic(self):
        """A v2 call whose request may have been sent, and a v2 call whose run.json records not_sent but whose usage.json is missing
        (so it is counted as started), both keep the diagnostic dict and its rendered panel."""
        call = 'e' * 32
        request = {'agent': 'openrouter', 'transport': 'http', 'provider_call_id': call, 'status': 'error',
                   'adapter_contract_version': 2, 'max_tokens': 16384, 'reasoning_max_tokens': 4096, 'answer_reserve_tokens': 8192}
        for name, state, usage_record in (
                ('sent', {'transport_status': 'error', 'transmission_state': 'sent_unknown'},
                 {'schema_version': 1, 'agent': 'openrouter', 'status': 'error', 'provider_call_id': call, 'tokens': {}}),
                ('not sent, no usage.json', {'transport_status': 'not_started', 'transmission_state': 'not_sent'}, None)):
            with self.subTest(name):
                self.write(f'runs/{KEY}/1/run.json', request | state)
                if usage_record is None:
                    (self.root / f'runs/{KEY}/1/usage.json').unlink(missing_ok=True)
                else:
                    self.write(f'runs/{KEY}/1/usage.json', usage_record)
                usage = self.detail('runs', {'id': KEY, 'agent': 'openrouter', 'model': 'x/y', 'status': 'error', 'stage': 'extract'})
                self.assertIs(usage['details'][0]['call_started'], True)
                self.assertEqual(usage['details'][0]['reasoning_diagnostic'],
                                 {'status': 'unknown', 'total_limit': 16384, 'requested_reasoning': 4096, 'answer_target': 8192,
                                  'reported_reasoning': None, 'reported_output': None, 'non_reasoning_output': None,
                                  'excess_tokens': None})
                self.assertIn('<section class="panel" role="status"><strong>추론 요청 예산 준수 여부 미확인</strong>'
                              '<p>총 출력 한도 16,384 · 추론 요청 예산 4,096 · 답변 목표 여유 8,192</p>'
                              '<p>보고된 출력 미확인 · 그중 추론 미확인 · 비추론 출력 미확인 토큰</p><p>종료 사유 미확인</p>'
                              '<p class="muted">비추론 출력은 보고된 출력에서 추론을 뺀 값이며 완성된 답변 길이나 예약된 토큰이 아닙니다. '
                              '예산 이하라는 관측도 다음 호출의 준수를 보장하지 않습니다.</p></section>',
                              render_usage(usage).split('<details>', 1)[1])


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
