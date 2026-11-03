"""#795 stop decision doc (docs/extraction-fidelity-stop-decision.md) checked against DOC rendered with measured values.

DOC lists the doc's non-blank lines outside its command block, in order, with FENCE where that block stands; measure() fills each
placeholder. Report values come from the committed #785 assessment report.json, whose SHA256 the doc quotes;
tests/test_evidence_links_v1_trial_record.py checks that the assessment script prints it byte for byte. Chunk prompts and outputs
and the fixture copy are read after checking their SHA256 against the trial index, and the index against INDEX_SHA256 in
scripts/assess_extraction_fidelity_trial.py; run directory names come from the index paths; the criteria spec copy is checked
against CRITERIA_SHA256 there. HUMAN_LINES pins the transcription of the human answer; its source, the session record, is outside
the repository, and no test here reads it. INSTRUCTIONS is pinned text that a test finds in the candidate module's TEMPLATE and in
each retained chunk prompt.txt, and not in its original-prompt.txt. COMMAND pins the command block's code lines; a test runs its
code argument with this interpreter from the repository root. Fixed wording is pinned, not interpreted."""
import json
import os
import re
import shlex
import subprocess
import sys
import unittest

import scripts.assess_extraction_fidelity_trial as S
from verinote.agents import parse_response
from verinote.diagnostics import extraction_fidelity_candidate as candidate
from verinote.extraction_ab_runner import _hash
from verinote.store import digest
from verinote.usage import decode_transport

DOC_PATH = S.REPO/'docs/extraction-fidelity-stop-decision.md'
FENCE = '```'
HISTORY = '과거 기록:'
QUALIFIER = 'qualifier_not_representable'
FIELD = re.compile(r'\{[a-z0-9_]+\}')
ISSUE_REFERENCE = re.compile(r'(?:https://github\.com/semantic-reasoning/verinote/issues/|#)(?:748|756|768|785|794|795)(?!\d)')
DIGIT_TOKEN = re.compile(r'(?<![A-Za-z0-9_])(?:SHA256|D6|v1|v2|comparison_768|trial_causes_within_768)(?![A-Za-z0-9_])')
NUMBER_WORDS = re.compile(r'(?<![가-힣])(한|두|세|네|다섯)\s?(개|번|회|건|청크|오류|원인|지시|정책|조합|줄|절|가지)')
INSTRUCTIONS = ('직접 근거가 없는 no_evidence와 근거는 있으나 승인 정의로 의미를 보존할 수 없는 not_representable을 구분하세요',
                '각 dictionary_gaps 항목의 claim_indices는 해당 code의 제약을 직접 뒷받침하는 인용에 연결하세요',
                '조건·시점 제약은 실제 의미에 맞는 code로 구분하세요')
COMMAND = (
    '.venv/bin/python -c "',
    'import glob,hashlib,re',
    'from verinote.agents import parse_response; from verinote.usage import decode_transport',
    "for d in sorted(glob.glob('benchmarks/extraction/results/785-evidence-links-v1-trial/documents/three-1/runs/*/[0-9]')):",
    "  p=open(d+'/prompt.txt',encoding='utf-8').read(); o=parse_response('claude',decode_transport('claude',open(d+'/stdout.txt',encoding='utf-8').read())[0])[0]",
    "  print(d[-1], hashlib.sha256(re.sub('[0-9]+','#',p).encode()).hexdigest()[:12], [g['claim_indices'] for g in o['dictionary_gaps'] if g['code']=='qualifier_not_representable'])\"")
HUMAN_LINES = (
    '- 질문: "#795: evidence_links_v1이 기각된 뒤, 다음 추출 충실도 후보를 진행할까요, 아니면 근거 있는 중단 결정을 남길까요?"',
    '- 답: "중단, #756·#748도 닫기"',
    '- 답이 세션 기록에 남은 시각(UTC): 2026-11-03T05:35:41.699Z',
    '- 고른 선택지의 설명(에이전트 작성): "#795에 중단 사유를 기록하고 결정으로 닫습니다. #756(운영 적용)은 채택 후보가 없어 진행하지 않는 것으로, '
    '#748(우산)은 \'개선 미달·중단 사유 기록\'으로 닫습니다."',
    '- 같은 답의 #794 결정 3: "(a) v2 gold만 (Recommended)"',
    '- 같은 답의 #794 결정 1·2: "둘 다 보류 (Recommended)"')
DOC = (
    '# 추출 충실도 다음 후보 중단 결정',
    '**[#795](https://github.com/semantic-reasoning/verinote/issues/795)는 새 versioned 후보를 만들지 않고 중단한다.**',
    "중단은 사람이 정했다(아래 '사람의 결정').",
    '나머지 절의 정리와 해석은 에이전트가 썼고(`agent_proposed`), `human_review`는 pending이다.',
    '`tests/test_extraction_fidelity_stop_decision.py`가 이 문서를 검사한다.',
    '## 사람의 결정',
    'Claude Code 세션의 AskUserQuestion 질문과 답을 에이전트가 옮겨 적었고, 답의 원본은 그 세션 기록이다.',
    '에이전트도 사용자 계정으로 커밋하고 댓글을 쓰므로, 커밋이나 GitHub의 작성자 표시는 이 답이 사람의 것이라는 증명이 아니다.',
    *HUMAN_LINES,
    '`(Recommended)`는 에이전트가 선택지에 붙인 표시다.',
    '## 남은 오류와 원인',
    '[#785 평가 보고서]({report_link})의 SHA256은 `{report_sha256}`이다.',
    '보고서의 `trial.limits.repetitions`는 {repetitions}이고, 판정은 `{verdict}`, 사유는 {reasons}다.',
    '남은 v2 annotation 오류는 청크 {coverage_chunk}의 `{coverage_predicate}` coverage(`coverage_semantics`)와 '
    '청크 {gap_chunks}의 `gap_completeness`다.',
    '청크 {coverage_chunk}의 `{coverage_predicate}` coverage 행은 과거 기록 claim을 연결했지만 status가 `{coverage_status}`이고, '
    'v2 gold의 status는 `{gold_status}`이다.',
    '보고서 `v1.mapping`은 오류마다 원인을 하나씩 적는다: {trial_causes}.',
    '위 원인의 category는 {categories}이고, 원인이 된 v1 실패의 `obligation_ids`는 {obligation_ids}이다.',
    '보고서 `v1.sensitivity`에서 이 오류를 옮긴 정책은 {moving_policies}이고, 보존 기준 사본에서 그 `human_review`는 {moving_reviews}이다.',
    '보고서 `v1`의 판정은 결정적 층 `{v1_deterministic}`, 전체 `{v1_overall}`이고, 시험 뒤에 고른 정책 조합(`v1.post_hoc_combinations`)의 판정은 '
    '결정적 층 `{post_hoc_deterministic}`, 전체 `{post_hoc_overall}`다.',
    '이 조합은 시험 뒤에 골랐으므로 채택 근거가 아니다(#795).',
    '위 #794 답 (a)에서는 v2 유한 gold가 채택을 좌우한다(에이전트 읽기).',
    '## #768 원인과의 관계',
    'annotation 오류는 #768 실행이 {errors_768}개, 이 시험이 {errors_trial}개다.',
    '보고서 `comparison_768`에서 시험에만 있는 원인(`causes_only_trial`)은 없고, `trial_causes_within_768`은 `{within_768}`다.',
    '`historical:P-GAP-QUALIFIER-DUP` 원인은 #768 실행에서 청크 {historical_768}에, 이 시험에서 청크 {historical_trial}에 있다.',
    '`predicate:archives:D6` 원인은 #768 실행에서 청크 {archives_768}에, 이 시험에서 청크 {archives_trial}에 있다.',
    '이 비교만으로는 줄어든 원인을 후보 지시의 효과로 보지 않는다(에이전트 판단, 아래 분기 절).',
    '## 같은 시험 안의 청크 분기',
    '저장소 루트에서 다음 명령은 청크마다 청크 번호, 숫자열을 `#`로 바꾼 prompt.txt의 SHA256 앞 {prefix_length}자, '
    '`qualifier_not_representable` gap의 `claim_indices`를 출력한다.',
    FENCE,
    '청크 {same_chunks}의 prompt.txt는 숫자열을 `#`로 바꾸면 같고, 청크 {other_chunks}은 다르다.',
    '청크 {linked_chunk}는 과거 기록 claim(인덱스 {linked_index})을 `qualifier_not_representable` gap에 연결했다.',
    '청크 {unlinked_peer}은 같은 인덱스의 과거 기록 claim을 `archives` coverage 행에 연결했지만 gap에는 연결하지 않았다.',
    '과거 기록 claim을 이 gap에 연결하지 않은 청크는 청크 {unlinked_chunks}이고, v2 `gap_completeness` 오류가 남은 청크와 같다.',
    '같은 실행에서 숫자만 다른 입력의 출력이 갈렸으므로, 실행 {run_count}회의 통과·실패를 prompt 변경의 효과로 볼 수 없다(에이전트 판단).',
    '## 기존 template 지시',
    '후보 모듈 `verinote/diagnostics/extraction_fidelity_candidate.py`의 `TEMPLATE`에 다음 지시가 있고, 시험의 청크별 prompt.txt에는 '
    '들어 있으며 original-prompt.txt에는 없다.',
    *(f'- `{text}`' for text in INSTRUCTIONS),
    '이 지시가 남은 의무(`archives` coverage 상태, 과거 한정어 gap)에 해당한다는 것은 에이전트 판단이다.',
    '남은 오류에 맞춘 문구를 더하면 사례 `{case_id}`에 맞추는 변경이 될 수 있다(에이전트 판단).',
    '## 결정의 범위',
    '사람의 답에 따라 #756은 채택 후보가 없어 진행하지 않는 것으로, #748은 개선 미달·중단 사유를 기록하고 닫는다.',
    '이 결정은 #748과 #756의 성공 완료가 아니다.',
    '이 결정은 #785의 시험 원본·평가 보고서, v2 gold와 후보 모듈을 고치지 않는다.',
    '다음 후보를 다시 시작하려면 새 이슈가 #795의 계약·예산 승인·held-out 또는 과적합 방지 조건을 갖춰야 한다(에이전트 제안).',
)


def split_doc(text):
    """(non-blank lines outside fenced blocks, with FENCE where each block stands; fenced block bodies as line lists)."""
    lines, blocks, block = [], [], None
    for line in text.splitlines():
        if line.startswith(FENCE):
            if block is None: block = []
            else: blocks.append(block); lines.append(FENCE); block = None
        elif block is not None: block.append(line)
        elif line.strip(): lines.append(line)
    return lines, blocks


def unmeasured_digits(template):
    """Digits left after removing placeholders, ISSUE_REFERENCE matches and DIGIT_TOKEN matches."""
    return re.findall(r'\d', DIGIT_TOKEN.sub('', ISSUE_REFERENCE.sub('', FIELD.sub('', template))))


def checked(path, sha256):
    if _hash(path) != sha256: raise AssertionError(path)
    return path.read_bytes()


def load_trial():
    """({chunk number: (prompt, original prompt, parsed output)}, run directory names under runs/ in the trial index paths,
    fixture three) of the retained trial; each file is read after checking its SHA256 against the trial index, and the index
    against S.INDEX_SHA256."""
    index = json.loads(checked(S.TRIAL_ROOT/'index.json', S.INDEX_SHA256))
    def read(path): return checked(path, index[path.relative_to(S.TRIAL_ROOT).as_posix()]['sha256']).decode('utf-8')
    row = json.loads(read(S.TRIAL_ROOT/'report.json'))['documents'][0]
    run = S.TRIAL_ROOT/row['artifact']/'runs'/row['run_id']
    chunks = {int(f.name): (read(f/'prompt.txt'), read(f/'original-prompt.txt'),
                            parse_response('claude', decode_transport('claude', read(f/'stdout.txt'))[0])[0])
              for f in sorted(run.iterdir()) if f.is_dir()}
    prefix = f"{row['artifact']}/runs/"
    run_ids = sorted({key[len(prefix):].split('/')[0] for key in index if key.startswith(prefix)})
    return chunks, run_ids, json.loads(read(S.TRIAL_ROOT/'fixtures/three.json'))


def history_index(output):
    found = [i for i, claim in enumerate(output['claims']) if HISTORY in claim['quote']]
    if len(found) != 1: raise AssertionError(found)
    return found[0]


def qualifier_gaps(output):
    return [g['claim_indices'] for g in output['dictionary_gaps'] if g['code'] == QUALIFIER]


def normalized(prompt):
    return re.sub('[0-9]+', '#', prompt)


def joined(numbers): return '·'.join(map(str, numbers))
def ticked(values): return ', '.join(f'`{v}`' for v in values)
def cause_chunks(causes, target): return sorted(int(c[1:c.index(':')]) for c in causes if c.split(':', 1)[1] == target)


def run_doc_command(block):
    """(shown output lines, stdout lines) of the doc's fenced command, run with this interpreter from the repository root."""
    shown = [line[2:] for line in block if line.startswith('# ')]
    argv = shlex.split('\n'.join(line for line in block if not line.startswith('# ')))
    if (argv[:2], len(argv)) != (['.venv/bin/python', '-c'], 3): raise AssertionError(argv[:2])
    run = subprocess.run([sys.executable, '-c', argv[2]], cwd=S.REPO, capture_output=True, text=True, check=True, timeout=60,
                         env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    return shown, run.stdout.splitlines()


def measure(report, chunks, run_ids, gold, spec, printed):
    """Placeholder values for DOC, each derived from the report, the retained chunk files, the run directory names, the fixture
    copy, the spec copy or the doc command's stdout lines (printed)."""
    errors = report['v2_annotation_errors']; v1 = report['v1']; mapping = v1['mapping']; comparison = report['comparison_768']
    (coverage,) = [e for e in errors if e['reason'] == 'coverage_semantics']
    row = next(c for c in chunks[coverage['chunk']][2]['coverage'] if c['predicate'] == coverage['predicate'])
    trial = [c for e in mapping['errors'] for c in e['causes']]; causes_768 = set(comparison['causes_only_768']) | set(trial)
    moving = list(dict.fromkeys(r['policy'] for r in v1['sensitivity'] if r.get('moved_errors')))
    groups = {}
    for n, (prompt, _, _) in chunks.items(): groups.setdefault(normalized(prompt), []).append(n)
    same = next((g for g in groups.values() if len(g) > 1), [])
    linked = [n for n, (_, _, o) in chunks.items() if any(history_index(o) in g for g in qualifier_gaps(o))]
    return {
        'report_link': os.path.relpath(S.REPORT_PATH, DOC_PATH.parent), 'report_sha256': digest(S.REPORT_PATH.read_bytes()),
        'case_id': report['verdict']['quality']['evaluation']['case_id'],
        'repetitions': report['trial']['limits']['repetitions'], 'verdict': report['verdict']['verdict'],
        'reasons': ticked(report['verdict']['reasons']),
        'coverage_chunk': coverage['chunk'], 'coverage_predicate': coverage['predicate'],
        'gap_chunks': joined(e['chunk'] for e in errors if e['reason'] == 'gap_completeness'),
        'coverage_status': row['status'],
        'gold_status': next(c for c in gold['annotations'][coverage['chunk']-1]['coverage'] if c['predicate'] == coverage['predicate'])['status'],
        'trial_causes': ticked(trial),
        'categories': ticked(sorted({c['category'] for c in mapping['causes']})),
        'obligation_ids': ticked(sorted({o for e in mapping['errors'] for f in e['v1_failures'] for o in f['obligation_ids']})),
        'moving_policies': '·'.join(moving), 'moving_reviews': ', '.join(sorted({spec['policies'][p]['human_review'] for p in moving})),
        'v1_deterministic': v1['deterministic'], 'v1_overall': v1['overall'],
        'post_hoc_deterministic': ', '.join(sorted({r['deterministic'] for r in v1['post_hoc_combinations']})),
        'post_hoc_overall': ', '.join(sorted({r['overall'] for r in v1['post_hoc_combinations']})),
        'errors_768': comparison['annotation_errors']['768'], 'errors_trial': len(errors),
        'within_768': json.dumps(comparison['trial_causes_within_768']),
        'historical_768': joined(cause_chunks(causes_768, 'historical:P-GAP-QUALIFIER-DUP')),
        'historical_trial': joined(cause_chunks(trial, 'historical:P-GAP-QUALIFIER-DUP')),
        'archives_768': joined(cause_chunks(causes_768, 'predicate:archives:D6')),
        'archives_trial': joined(cause_chunks(trial, 'predicate:archives:D6')),
        'prefix_length': joined(sorted({len(line.split(' ')[1]) for line in printed})),
        'same_chunks': joined(same), 'other_chunks': joined(n for n in sorted(chunks) if n not in same),
        'linked_chunk': joined(linked), 'linked_index': joined(history_index(chunks[n][2]) for n in linked),
        'unlinked_peer': joined(n for n in same if n not in linked),
        'unlinked_chunks': joined(n for n in sorted(chunks) if n not in linked),
        'run_count': len(run_ids),
    }


class StopDecisionDoc(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines, cls.blocks = split_doc(DOC_PATH.read_text(encoding='utf-8'))
        cls.report = json.loads(S.REPORT_PATH.read_bytes())
        cls.chunks, cls.run_ids, cls.gold = load_trial()
        cls.spec = json.loads(checked(S.CRITERIA_ROOT/'spec.json', S.CRITERIA_SHA256['spec.json']))
        (cls.block,) = cls.blocks; cls.shown, cls.printed = run_doc_command(cls.block)
        cls.values = measure(cls.report, cls.chunks, cls.run_ids, cls.gold, cls.spec, cls.printed)

    # The doc's lines and the rules on DOC's own text.
    def test_doc_nonblank_lines_outside_the_command_block_equal_DOC_rendered_with_measured_values(self):
        self.assertEqual(self.lines, [template.format(**self.values) for template in DOC])

    def test_DOC_entries_other_than_HUMAN_LINES_keep_no_digit_after_removing_placeholders_issue_references_and_DIGIT_TOKEN(self):
        self.assertEqual([t for t in DOC if t not in HUMAN_LINES and unmeasured_digits(t)], [])

    def test_NUMBER_WORDS_matches_in_DOC_entries_other_than_HUMAN_LINES_are_empty(self):
        self.assertEqual([t for t in DOC if t not in HUMAN_LINES and NUMBER_WORDS.search(t)], [])

    def test_digit_and_number_word_rules_flag_constructed_lines(self):
        lines = ['오류는 3개다.', '청크 #1도 있다.', '원인은 ３개다.', 'v12 기준이 있다.', '#795와 v2, SHA256, `D6`, {errors_768}만 있다.']
        self.assertEqual([line for line in lines if unmeasured_digits(line)], lines[:4])
        lines = ['세 원인이 있다.', '원인은 세 가지다.', '원인이 하나씩 있다.']
        self.assertEqual([line for line in lines if NUMBER_WORDS.search(line)], lines[:2])

    def test_backticked_and_linked_repository_paths_exist(self):
        text = '\n'.join(self.lines)
        paths = re.findall(r'`((?:verinote|tests|scripts|benchmarks|docs)/[^`]+)`', text)+re.findall(r'\]\(\.\./([^)#]+)\)', text)
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path): self.assertTrue((S.REPO/path).exists())

    # Propositions the doc lines state beyond their placeholder values.
    def test_v2_annotation_errors_equal_evaluation_errors_and_their_reasons_are_coverage_semantics_and_gap_completeness(self):
        errors = self.report['v2_annotation_errors']
        self.assertEqual(errors, self.report['verdict']['quality']['evaluation']['annotation_errors'])
        self.assertEqual(sorted({e['reason'] for e in errors}), ['coverage_semantics', 'gap_completeness'])

    def test_coverage_row_claim_indices_include_the_history_claim(self):
        output = self.chunks[self.values['coverage_chunk']][2]
        row = next(c for c in output['coverage'] if c['predicate'] == self.values['coverage_predicate'])
        self.assertIn(history_index(output), row['claim_indices'])

    def test_each_v1_mapping_error_has_one_cause_and_its_frozen_error_is_the_v2_error(self):
        errors = self.report['v1']['mapping']['errors']
        self.assertEqual([e['frozen_error'] for e in errors], self.report['v2_annotation_errors'])
        self.assertEqual([len(e['causes']) for e in errors], [1]*len(errors))

    def test_v1_failures_are_nonempty_and_each_has_obligation_ids_historical(self):
        failures = [f for e in self.report['v1']['mapping']['errors'] for f in e['v1_failures']]
        self.assertTrue(failures)
        self.assertEqual([f['obligation_ids'] for f in failures], [['historical']]*len(failures))

    def test_sensitivity_moved_error_ids_are_nonempty_and_are_v1_mapping_error_ids(self):
        moved = {m['id'] for r in self.report['v1']['sensitivity'] for m in r.get('moved_errors', [])}
        self.assertTrue(moved)
        self.assertLessEqual(moved, {e['id'] for e in self.report['v1']['mapping']['errors']})

    def test_post_hoc_combinations_are_nonempty_and_report_scope_says_they_were_chosen_after_the_trial(self):
        self.assertTrue(self.report['v1']['post_hoc_combinations'])
        self.assertIn('post_hoc_combinations were chosen after the trial', self.report['scope'])

    def test_errors_trial_equals_comparison_768_trial_count(self):
        self.assertEqual(self.report['comparison_768']['annotation_errors']['trial'], len(self.report['v2_annotation_errors']))

    def test_causes_only_trial_top_level_and_per_error_are_empty_and_causes_only_768_is_not(self):
        self.assertEqual(self.report['comparison_768']['causes_only_trial'], [])
        self.assertEqual([e['causes_only_trial'] for e in self.report['comparison_768']['errors']], [[]]*len(self.report['comparison_768']['errors']))
        self.assertTrue(self.report['comparison_768']['causes_only_768'])

    def test_doc_command_block_code_lines_equal_COMMAND(self):
        self.assertEqual([line for line in self.block if not line.startswith('# ')], list(COMMAND))

    def test_doc_command_stdout_equals_the_shown_lines_and_the_test_derivation(self):
        self.assertEqual(self.printed, self.shown)
        n = int(self.values['prefix_length'])
        self.assertEqual(self.shown, [f'{k} {digest(normalized(p).encode())[:n]} {qualifier_gaps(o)}' for k, (p, _, o) in sorted(self.chunks.items())])

    def test_unlinked_peer_history_claim_index_equals_linked_index_is_in_its_archives_row_and_absent_from_its_gap_claim_indices(self):
        output = self.chunks[int(self.values['unlinked_peer'])][2]; index = history_index(output)
        self.assertEqual(index, int(self.values['linked_index']))
        self.assertIn(index, next(c for c in output['coverage'] if c['predicate'] == 'archives')['claim_indices'])
        self.assertNotIn(index, [i for g in output['dictionary_gaps'] for i in g['claim_indices']])

    def test_unlinked_chunks_equal_gap_completeness_chunks(self):
        self.assertEqual(self.values['unlinked_chunks'], self.values['gap_chunks'])

    def test_qualifier_gaps_differ_within_the_equal_prompt_group(self):
        self.assertGreater(len({str(qualifier_gaps(self.chunks[int(n)][2])) for n in self.values['same_chunks'].split('·')}), 1)

    def test_each_INSTRUCTIONS_text_is_in_TEMPLATE_and_each_chunk_prompt_and_absent_from_each_original_prompt(self):
        for text in INSTRUCTIONS:
            with self.subTest(text=text):
                self.assertIn(text, candidate.TEMPLATE)
                for n, (prompt, original, _) in self.chunks.items():
                    self.assertIn(text, prompt, n); self.assertNotIn(text, original, n)


if __name__ == '__main__': unittest.main()
