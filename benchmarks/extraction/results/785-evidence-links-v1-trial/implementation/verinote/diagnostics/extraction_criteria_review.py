"""Offline observations plus explicitly agent-proposed evaluation criteria review."""
import copy
from pathlib import Path

from ..extraction_ab_runner import _hash, file_index
from ..extraction_evaluation import DATA_ROOT, ideal_outputs, load_case
from ..store import digest
from .extraction_fidelity import DEFAULT_ROOT, assess_outputs, diagnose

REPO = Path(__file__).resolve().parents[2]
REVIEW_BASE_REVISION = 'eafa841de6980dd08f2cf11a1413688038ab9fe2'
REVIEW_VERSION = 'extraction-criteria-review-v1'
REVIEW_PATHS = ('verinote/diagnostics/extraction_criteria_review.py',
                'scripts/review_extraction_criteria.py', 'tests/test_extraction_criteria_review.py')
# Anchors identify actual inspected consumers. Hashes bind the complete files;
# excerpts/line numbers make consequences independently inspectable.
SOURCE_ANCHORS = {
    'page_review': ('verinote/web/app.js', "  const body=item.kind==='ontology'?", 1),
    'bulk_review': ('verinote/web/app.js', "  $('#bulk-review-preview').innerHTML=", 1),
    'coverage_ui': ('verinote/web/app.js', 'function renderCoverage(', 8),
    'wiki_evidence': ('verinote/assistant.py', '    def wiki_text(page):', 9),
    'accounting': ('verinote/extraction_coverage.py', 'def validate_accounting(', 49),
    'coverage_export': ('verinote/extraction_coverage.py', '            # Indices in reports are local;', 5),
    'finite_support': ('verinote/extraction_threshold_evaluation.py', 'def _supported(', 3),
    'finite_annotations': ('verinote/extraction_threshold_evaluation.py', 'def evaluate_annotations(', 40),
    'page_validation': ('verinote/service.py', 'def validate_page(', 17),
}


def inspected_sources():
    result = {}
    for name, (relative, anchor, length) in SOURCE_ANCHORS.items():
        path = REPO/relative
        if path.is_symlink(): raise ValueError('review source symlink')
        lines = path.read_text(encoding='utf-8').splitlines()
        found = [i for i, line in enumerate(lines) if line.startswith(anchor)]
        if len(found) != 1: raise ValueError('review source anchor mismatch:'+name)
        start = found[0]
        result[name] = {'path': relative, 'sha256': _hash(path), 'line_start': start+1,
                        'line_end': min(len(lines), start+length), 'excerpt': '\n'.join(lines[start:start+length])}
    return result


JUDGMENTS = {
    'unquoted_negation': {
        'categories': ['product_evidence_gap'],
        'rationale': 'The prose preserves the negation, but its attached quote does not contain that source statement. This is an auditability gap, not evidence that a positive forbidden fact was published.',
        'consumer_consequence': 'Page/bulk reviewers and approved-wiki question context see the claim prose beside a quote supporting another statement; they cannot verify this negation from the attached quote.',
        'consumer_refs': ['page_review', 'bulk_review', 'wiki_evidence', 'finite_support'],
        'uncertainty': 'General entailment is not decided here; this judgment concerns the observed literal evidence mismatch.'},
    'unsupported_missing_relation': {
        'categories': ['product_evidence_gap'],
        'rationale': 'The ownership constraint is described in prose/detail, but the linked claim quote is about the unregistered target of a call. The quoted ownership evidence is absent. Several frozen errors share this one underlying mismatch.',
        'consumer_consequence': 'The gap UI points to a claim number whose quote does not support the missing-relation detail. The page and wiki evidence carry the same unsupported prose-to-quote pairing.',
        'consumer_refs': ['coverage_ui', 'coverage_export', 'page_review', 'wiki_evidence', 'finite_annotations'],
        'uncertainty': 'Correct prose is useful but does not replace the missing source evidence link.'},
    'coverage_navigation': {
        'categories': ['product_evidence_gap', 'representation_sensitive'],
        'rationale': 'The unregistered-target quote and claim already exist, but calls coverage does not reference that claim. This is incomplete relation-specific navigation, not loss of the claim from the wiki.',
        'consumer_consequence': 'The coverage UI lists only the selected claim numbers. A reviewer following the calls row misses the existing outside-target claim even when the reason mentions its exclusion.',
        'consumer_refs': ['coverage_ui', 'coverage_export', 'wiki_evidence', 'finite_annotations'],
        'uncertainty': 'How much completeness the navigation list promises is a product policy question; the frozen evaluator requires this specific evidence link.'},
    'historical_status_policy': {
        'categories': ['representation_sensitive', 'unresolved'],
        'rationale': 'The archives row links the correct ended-in-the-past quote and explains that there is no current direct fact. no_evidence versus not_representable depends on whether status scopes current approved facts or all relevant historical evidence; the frozen annotation chooses the latter.',
        'consumer_consequence': 'The UI badge says no direct evidence while the adjacent reason and referenced wiki claim preserve the past/end qualifier. This can confuse status interpretation, but the historical qualifier itself has not been omitted.',
        'consumer_refs': ['coverage_ui', 'wiki_evidence', 'accounting', 'finite_annotations'],
        'uncertainty': 'Explicit product wording/policy should settle the status distinction before treating both representations as equally material failures.'},
    'qualified_gap_policy': {
        'categories': ['representation_sensitive', 'unresolved'],
        'rationale': 'The evaluator requires qualifier gap membership for every annotated conditional/historical obligation. Historical evidence is already preserved in the wiki and archives coverage; requiring another gap reference is an additional representation policy.',
        'consumer_consequence': 'The gap count/list omits that qualifier entry, while the page and coverage still expose the historical restriction. The list is labelled model self-review, not a proof of semantic completeness.',
        'consumer_refs': ['coverage_ui', 'wiki_evidence', 'accounting', 'finite_annotations'],
        'uncertainty': 'The baseline prompt enumerates allowed gap codes and supporting claims but does not fully settle per-qualifier gap granularity. Missing ownership evidence remains a separate material component.'},
}


def _error_observation(error, chunk, obligation):
    claims = chunk['normalized_output']['claims']
    coverage = chunk['normalized_output']['coverage']
    gaps = chunk['normalized_output']['dictionary_gaps']
    if error['reason'] == 'missing_claim_evidence':
        selected = [r for r in obligation['claim_obligations'] if r['id'] == error['id']]
        requirements = selected[0]['prompt_requirements']
        expected = {r['id']: r['source_evidence'] for r in selected}
    elif error['reason'] == 'coverage_semantics':
        selected = [r for r in obligation['coverage_obligations'] if r['predicate'] == error['predicate']]
        requirements = selected[0]['prompt_requirements']
        expected = {r['id']: r['source_evidence'] for r in obligation['claim_obligations'] if r['id'] in selected[0]['expected_claim_ids']}
        coverage = [row for row in coverage if row['predicate'] == error['predicate']]
    else:
        selected = obligation['gap_obligations']
        requirements = ['dictionary_gaps', 'qualified_claims']
        expected = {r['id']: r['source_evidence'] for r in obligation['claim_obligations']}
        if error['reason'] == 'unexpected_gap_evidence': gaps = [r for r in gaps if r['code'] == error['code']]
    return {'frozen_error': copy.deepcopy(error), 'expected_source_evidence': copy.deepcopy(expected),
            'actual_claims': [{'index': i, **copy.deepcopy(row)} for i, row in enumerate(claims)],
            'actual_coverage': copy.deepcopy(coverage), 'actual_gaps': copy.deepcopy(gaps),
            'frozen_obligation_rows': copy.deepcopy(selected), 'prompt_requirement_ids': requirements}


def _review_groups(error, obligation):
    """Match observed relationships, not error codes alone; otherwise abstain."""
    claims = {row['id']: row for row in obligation['claim_obligations']}
    relation_gap = next((r for r in obligation['gap_obligations'] if r['code'] == 'missing_relation'), None)
    unsupported_relation = (claims['missing_relation']['matched_claim_indices'] == [] and relation_gap is not None and
                            any(row['unexpected_claim_indices'] for row in relation_gap['actual_rows']))
    if error['reason'] == 'missing_claim_evidence':
        if error['id'] == 'negated' and not claims['negated']['matched_claim_indices']: return ['unquoted_negation']
        if error['id'] == 'missing_relation' and unsupported_relation: return ['unsupported_missing_relation']
    if error['reason'] == 'coverage_semantics':
        row = next(r for r in obligation['coverage_obligations'] if r['predicate'] == error['predicate'])
        if (row['predicate'] == 'calls' and 'missing_entity' in row['missing_claim_ids'] and
                claims['missing_entity']['matched_claim_indices']): return ['coverage_navigation']
        if (row['predicate'] == 'archives' and row['actual']['status'] == 'no_evidence' and
                row['expected_status'] == 'not_representable' and not row['missing_claim_ids'] and
                claims['historical']['matched_claim_indices']): return ['historical_status_policy']
    if error['reason'] == 'unexpected_gap_evidence' and error['code'] == 'missing_relation' and unsupported_relation:
        return ['unsupported_missing_relation']
    if error['reason'] == 'gap_completeness':
        result = ['unsupported_missing_relation'] if unsupported_relation else []
        qualifier = next(r for r in obligation['gap_obligations'] if r['code'] == 'qualifier_not_representable')
        if 'historical' in qualifier['missing_claim_ids'] and claims['historical']['matched_claim_indices']:
            result.append('qualified_gap_policy')
        return result
    return []


def historical_review(diagnosis):
    rows = []; groups: dict[str, list[str]] = {key: [] for key in JUDGMENTS}
    for ordinal, error in enumerate(diagnosis['finite_gold']['annotation_errors'], 1):
        chunk = next(row for row in diagnosis['chunks'] if row['index'] == error['chunk'])
        obligation = next(row for row in diagnosis['obligations'] if row['index'] == error['chunk'])
        group_ids = _review_groups(error, obligation)
        judgments = [{'group': key, **copy.deepcopy(JUDGMENTS[key])} for key in group_ids]
        categories = sorted({category for row in judgments for category in row['categories']}) or ['unresolved']
        key = f'error-{ordinal}'
        for group in group_ids: groups[group].append(key)
        rows.append({'id': key, 'observations': _error_observation(error, chunk, obligation),
                     'agent_proposed_review': {'human_review': 'pending', 'categories': categories, 'judgments': judgments,
                                              'abstention': None if judgments else 'Observed relationships do not match the inspected review case; needs review.'}})
    return {'judgment_author': 'agent_proposed', 'human_review': 'pending', 'errors': rows, 'shared_symptoms': [{'id': key, 'error_ids': ids, **copy.deepcopy(JUDGMENTS[key])}
                                               for key, ids in groups.items() if ids],
            'counting_notice': 'These are overlapping evidence/representation symptoms, not 16 independent product defects. Categories are not a new verdict.'}


def controls():
    """Controlled synthetic contrasts; no original or candidate output is repaired."""
    case = load_case('short', DATA_ROOT/'v2'); baseline = ideal_outputs(case, contract_version=2)[0]['output']
    rows = []
    def add(key, output, expectation, rationale, changes):
        result = assess_outputs(case, [{'index': 1, 'output': output}])
        rows.append({'id': key, 'definition': changes, 'output': output, 'output_sha256': digest(output),
                     'observed': {'production_passed': result['production_passed'],
                                  'production_errors': [r['production_error'] for r in result['chunks'] if r['production_error']],
                                  'finite_passed': result['finite_gold']['passed'] if result['finite_gold'] is not None else None,
                                  'finite_evaluation': result['finite_gold']},
                     'agent_proposed_product_expectation': {'expectation': expectation, 'rationale': rationale, 'human_review': 'pending'}})
    add('split_claims_baseline', copy.deepcopy(baseline), 'preserves_evidence', 'Each relevant constraint has its source quote and references.', 'Unchanged bundled synthetic oracle; not a generated result.')
    out = copy.deepcopy(baseline); size = len(out['claims']); out['claims'].reverse(); out['coverage'].reverse(); out['dictionary_gaps'].reverse()
    for row in out['coverage']+out['dictionary_gaps']: row['claim_indices'] = [size-1-i for i in row['claim_indices']]
    add('order_and_remap', out, 'preserves_evidence', 'Order is incidental when all references retain their meaning.', 'Reverse claim/coverage/gap order and remap every claim index.')
    out = copy.deepcopy(baseline)
    annotation = case['annotations'][0]['claims']; start = min(c['evidence']['start'] for c in annotation); end = max(c['evidence']['end'] for c in annotation)
    out['claims'] = [{'heading': 'Combined constraints', 'text': case['source'][start:end], 'quote': case['source'][start:end]}]
    for row in out['coverage']+out['dictionary_gaps']:
        if row['claim_indices']: row['claim_indices'] = [0]
    add('combined_contiguous_claim', out, 'preserves_evidence', 'One contiguous quote genuinely contains each required constraint; no oracle claim count is necessary.', 'Combine all five constraints with their actual contiguous source and remap nonempty references to claim 0.')
    out = copy.deepcopy(baseline)
    for i, row in enumerate(out['claims']): row['heading'] = f'Source constraint {i+1}'
    for row in out['coverage']: row['reason'] = '인용된 원문과 승인 정의를 대조한 판단'
    for row in out['dictionary_gaps']: row['detail'] = '참조한 원문에 있는 승인 사전 제약'
    add('incidental_wording', out, 'preserves_evidence', 'Headings and explanatory wording change, while substantive claims, quotes, status and links remain.', 'Change heading/reason/detail wording only.')
    out = copy.deepcopy(baseline)
    out['dictionary_gaps'] = [dict(row, claim_indices=[i]) for row in out['dictionary_gaps'] for i in row['claim_indices']]
    add('split_gap_rows', out, 'preserves_evidence', 'Multiple rows of one code can reference the same complete set of constraints.', 'Split each multi-claim gap into individual rows without changing evidence.')
    out = copy.deepcopy(out); grouped = {}
    for row in out['dictionary_gaps']:
        if row['code'] not in grouped: grouped[row['code']] = dict(row, claim_indices=[])
        grouped[row['code']]['claim_indices'].extend(row['claim_indices'])
    out['dictionary_gaps'] = list(grouped.values())
    add('group_gap_rows', out, 'preserves_evidence', 'Grouping by code preserves all supported links.', 'Recombine the split gap rows by code.')
    out = copy.deepcopy(baseline)
    labels = ('구간 1 부정: ', '구간 1 조건: ', '구간 1 과거 기록: ', '구간 1 미등록 대상: ', '구간 1 소유: ')
    for row, label in zip(out['claims'], labels, strict=True):
        if not row['quote'].startswith(label): raise ValueError('synthetic label definition changed')
        row['quote'] = row['quote'][len(label):]
    add('short_quotes_without_fixture_labels', out, 'preserves_evidence_proposed', 'Only the five identified fixture labels are removed; the quoted bodies retain negation, conditions, dates/end and dictionary restrictions. Their product sufficiency is proposed, while frozen span rejection is preserved.', 'Shorten only those five known claim prefixes; leave fact quotes, hypothetical markers and all semantic body text unchanged.')
    out = copy.deepcopy(baseline)
    for row in out['claims']: row['quote'] = case['source'][start:end]
    add('longer_contiguous_quotes', out, 'preserves_evidence', 'Additional continuous context retains the evidence, although broader quotes impose more review burden and do not prove claim entailment.', 'Extend each claim quote to the complete contiguous constraint passage; retain claim text and indices.')
    out = copy.deepcopy(baseline); out['claims'][1].update(text='S0는 S2를 현재 무조건 직접 호출한다.', quote='S0는 S2를 직접 호출한다.')
    add('dangerous_conditional_dropping', out, 'material_failure', 'The original conditional call is presented as unconditional and its quoted excerpt removes the condition.', 'Drop the condition from one claim text and quote; retain references.')
    out = copy.deepcopy(baseline)
    next(r for r in out['dictionary_gaps'] if r['code'] == 'missing_relation')['claim_indices'] = [3]
    add('wrong_gap_reference', out, 'material_failure', 'A missing-relation detail is now supported only by an unrelated missing-entity quote.', 'Point the missing_relation gap to the missing_entity claim.')
    out = copy.deepcopy(baseline); out['coverage'] = [r for r in out['coverage'] if r['predicate'] != 'calls']
    add('missing_coverage_row', out, 'material_failure', 'The approved relation has no accounting row and its fact cannot be audited through that relation summary.', 'Remove the calls coverage row.')
    out = copy.deepcopy(baseline); out['coverage'][0].update(status='no_evidence', fact_indices=[])
    add('status_contradicts_emitted_fact', out, 'material_failure', 'The summary denies evidence while a direct fact remains in the response.', 'Set calls to no_evidence with empty fact_indices while keeping its fact.')
    out = copy.deepcopy(baseline); out['coverage'][1].update(status='no_evidence', reason='보관은 과거에 종료되어 현재 직접 근거 없음')
    add('historical_status_with_preserved_evidence', out, 'policy_unresolved', 'The past/end qualifier remains in a correctly linked quote and reason; product policy must distinguish current-fact absence from unrepresentable historical evidence.', 'Change only archives status/reason, retaining its historical claim reference.')
    out = copy.deepcopy(baseline)
    next(r for r in out['dictionary_gaps'] if r['code'] == 'qualifier_not_representable')['claim_indices'] = [1]
    add('historical_gap_omitted_but_claim_retained', out, 'policy_unresolved', 'Historical evidence and archives linkage remain. Requiring a second qualifier-gap link is an explicit completeness policy question.', 'Remove only the historical qualifier gap reference, leaving the conditional gap, claim and coverage intact.')
    out = copy.deepcopy(baseline); out['claims'][0]['text'] = 'S1는 S0를 현재 무조건 직접 호출한다.'
    add('contradictory_prose_with_correct_quote', out, 'material_failure', 'The negated source quote accompanies affirmative unconditional prose. A frozen pass here demonstrates a claim-text entailment blind spot, not product correctness.', 'Change only the negated claim text to contradict its unchanged quote; all gold-covering quotes/references remain.')
    return {'case_id': case['case_id'], 'case_sha256': digest(case), 'source_sha256': case['source_sha256'], 'cases': rows,
            'notice': 'Observed production/finite results are independent of agent-proposed product expectations; controls do not revise gold or historical outputs.'}


def review():
    before = file_index(DEFAULT_ROOT)
    diagnosis = diagnose()
    if len(diagnosis['finite_gold']['annotation_errors']) != 16:
        raise ValueError('unexpected authenticated baseline error set')
    sources = inspected_sources()
    result = {'schema_version': 1, 'review_version': REVIEW_VERSION, 'review_base_revision': REVIEW_BASE_REVISION,
              'human_review': 'pending', 'judgment_author': 'agent_proposed', 'generation_requests': 0,
              'historical_authentication': diagnosis['historical_authentication'],
              'diagnostic_implementation': diagnosis['diagnostic_implementation'],
              'historical_observations': {'production_passed': diagnosis['production_passed'], 'finite_evaluation': diagnosis['finite_gold'],
                                          'prompt_requirements': diagnosis['prompt_requirements'], 'call_linkages': diagnosis['call_linkages']},
              'criteria_review': historical_review(diagnosis), 'controlled_contrasts': controls(),
              'inspected_consumer_and_evaluator_sources': sources,
              'review_implementation': {'source_hashes': {p: _hash(REPO/p) for p in REVIEW_PATHS},
                                       'scope': 'Current review code/control definitions, separate from historical authentication.'},
              'limits': ['No new acceptance rule or automatic semantic repair is proposed by this executable report.',
                         'Sixteen frozen errors remain unchanged; overlap is not an independent defect/severity count.',
                         'A contradictory-prose pass demonstrates the documented finite scope; it does not show that the checker promised general entailment.',
                         'Product expectations and category assignments require human review, including the proposed short-quote false-reject and contradictory-prose false-accept interpretations.']}
    if before != file_index(DEFAULT_ROOT) or sources != inspected_sources(): raise ValueError('review inputs changed')
    return result
