"""Records stored before #894 can carry an empty or whitespace-only error: the question screen and the run list render each
through the real renderAnswer / renderRun in app.js (other helpers stubbed; executionGuidance returns GUIDANCE)."""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOT_STORED = '오류 문구가 저장되지 않았습니다.'
MARKUP = '<b>x</b> & y'
ESCAPED = '&lt;b&gt;x&lt;/b&gt; &amp; y'

PRELUDE = r'''
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync('verinote/web/app.js','utf8');
const elements=new Map(),$=s=>{if(!elements.has(s))elements.set(s,{innerHTML:'',value:''});return elements.get(s);};
const stub=()=>'';
const ctx=vm.createContext({$,executionGuidance:()=>'GUIDANCE',bindMeaningControls:()=>{},providerById:()=>null,
  renderUsage:stub,renderQueueAhead:stub,renderQuestionWindow:stub,renderQuestionMeanings:stub,renderEvidence:stub,renderAnalysis:stub,
  renderRelationTypeWarnings:stub,renderDictionaryReconciliations:stub,renderDictionaryExclusions:stub,renderAnalysisProgress:stub,
  renderCoverage:stub,renderLossAudit:stub,date:stub,sourceLink:stub});
vm.runInContext(['esc','labels','badge'].map(n=>source.match(new RegExp(`^const ${n} = .*$`,'m'))[0]).join(';'),ctx);
const record=JSON.parse(fs.readFileSync(0,'utf8'));
'''
ANSWER = PRELUDE + r'''
vm.runInContext(source.slice(source.indexOf('function renderAnswer('),source.indexOf('let deleteTarget;')),ctx);
ctx.job=record;vm.runInContext('renderAnswer(job)',ctx);process.stdout.write($('#answer-result').innerHTML);
'''
RUN = PRELUDE + r'''
vm.runInContext(source.slice(source.indexOf('function renderRun('),source.indexOf('function renderCoverage(')),ctx);
ctx.run=record;process.stdout.write(vm.runInContext('renderRun(run)',ctx));
'''


def render(script, record):
    return subprocess.check_output(['node', '-e', script], input=json.dumps(record), text=True, cwd=ROOT)


def question(status, error):
    return {'id': 'q1', 'question': '질문', 'agent': 'openrouter', 'model': '', 'snapshot': 's' * 64, 'status': status,
            'trace': [], 'evidence': {}, 'answer': None, 'error': error}


def run(status, detail):
    return {'id': 'r1', 'stage': 'extract', 'agent': 'openrouter', 'model': '', 'status': status, 'detail': detail,
            'document_id': 'd1', 'version': 1, 'created_at': ''}


class StoredEmptyError(unittest.TestCase):
    def test_question_with_status_error_and_an_empty_or_whitespace_only_error_shows_the_not_stored_line_and_guidance(self):
        for error in ('', ' \n\t '):
            with self.subTest(error=error):
                self.assertIn(f'<p class="error">{NOT_STORED}</p>GUIDANCE', render(ANSWER, question('error', error)))

    def test_question_with_status_error_and_a_nonblank_error_shows_that_error_as_stored(self):
        for error in ('RuntimeError (오류 문구 없음)', ' 앞뒤 공백 '):
            with self.subTest(error=error):
                html = render(ANSWER, question('error', error))
                self.assertIn(f'<p class="error">{error}</p>GUIDANCE', html)
                self.assertNotIn(NOT_STORED, html)

    def test_question_with_status_error_and_an_error_containing_markup_shows_it_escaped(self):
        html = render(ANSWER, question('error', MARKUP))
        self.assertIn(f'<p class="error">{ESCAPED}</p>GUIDANCE', html)
        self.assertNotIn('<b>', html)

    def test_run_with_status_error_and_an_empty_or_whitespace_only_detail_shows_the_not_stored_line_and_guidance(self):
        for detail in ('', ' \n\t '):
            with self.subTest(detail=detail):
                self.assertIn(f'<p class="error">{NOT_STORED}</p>GUIDANCE', render(RUN, run('error', detail)))

    def test_run_with_status_error_and_a_nonblank_detail_shows_that_detail_as_stored(self):
        for detail in ('RuntimeError (오류 문구 없음)', ' 앞뒤 공백 '):
            with self.subTest(detail=detail):
                html = render(RUN, run('error', detail))
                self.assertIn(f'<p class="error">{detail}</p>GUIDANCE', html)
                self.assertNotIn(NOT_STORED, html)

    def test_run_with_status_error_and_a_detail_containing_markup_shows_it_escaped(self):
        html = render(RUN, run('error', MARKUP))
        self.assertIn(f'<p class="error">{ESCAPED}</p>GUIDANCE', html)
        self.assertNotIn('<b>', html)


if __name__ == '__main__':
    unittest.main()
