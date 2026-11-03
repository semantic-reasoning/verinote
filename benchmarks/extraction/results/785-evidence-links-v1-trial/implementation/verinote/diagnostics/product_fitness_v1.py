"""Versioned extraction product-fitness criteria (#788); offline, deterministic, no model judge.

Separates deterministic source/span and reference checks (D1, D3-D6) from prose
fitness, which is never computed here. Agent-proposed semantic judgments can flag
an output but never pass or fail it.
"""
import copy
import hashlib
import json
from pathlib import Path

from ..documents import chunks
from ..extraction_ab_runner import _hash
from ..extraction_coverage import GAP_CODES
from ..extraction_evaluation import DATA_ROOT, ideal_outputs, load_case, load_manifest
from ..ontology import validate_dictionary
from ..store import digest
from .extraction_fidelity import REPORT_SHA256, _production, assess_outputs, diagnose

REPO = Path(__file__).resolve().parents[2]
CRITERIA_ROOT = REPO/'benchmarks/extraction/criteria/product-fitness-v1'
REVIEW_PATH = REPO/'benchmarks/extraction/results/785-criteria-review/review.json'
REVIEW_SHA256 = '77357b149da5a7d7fe2a0c46e8070d610369893ed1a32878b542b175d578dad6'
REPORT_PATH = REPO/'benchmarks/extraction/results/788-product-fitness-v1/report.json'
REGISTRY_CASES = ('short', 'three')
QUALIFIER_CODE = 'qualifier_not_representable'
STATUSES = ('extracted', 'no_evidence', 'not_representable')
# Decisions this module can execute, per policy; _policies rejects a spec decision outside this set.
IMPLEMENTED_DECISIONS = {
    'P-EQUIV': ('registry_substitutes',),
    'P-ORDER': ('index_remap_invariant',),
    'P-CLAIM-MERGE': ('one_quote_covers_each_span',),
    'P-CLAIM-SPLIT': ('reject_joined_quotes',),
    'P-COVERAGE-LINK': ('all_required_linked', 'links_optional'),
    'P-STATUS-HISTORICAL': ('registry_expected_status', 'historical_no_evidence_linked'),
    'P-FAILURE-KIND': ('evidence_iff_obligation_fails_d3', 'missing_link_always_evidence'),
    'P-GAP-QUALIFIER-DUP': ('every_qualified_obligation', 'exempt_linked_from_coverage'),
}
OBLIGATION_SCOPES = (None, 'all_qualifier_pairs')
# Owners of declared spec subjects (by prefix) and of declaration keys; see _check_declarations.
DECLARATION_OWNERS = {'control:': 'differences_from_v2', 'fixture:': 'differences_from_v2',
                      'historical:': 'historical_mapping'}
DECLARATION_KEYS = {'deliberate_differences_from_v2': 'differences_from_v2',
                    'disagreements_with_785': 'differences_from_v2',
                    'historical_mapping_disagreements_with_785': 'historical_mapping'}
# spec overall_vs_category: product materiality, separate from the overall verdict.
CATEGORY_BY_KIND = {'evidence': 'product_evidence', 'label': 'unresolved', None: None}
CATEGORY_NAMES_785 = {'product_evidence_gap': 'product_evidence', 'representation_sensitive': 'representation_sensitive',
                      'unresolved': 'unresolved'}
# Per-case prose for the contradictory_prose recipe. Its quote is unchanged.
CONTRADICTORY_TEXT = {'short': 'S1는 S0를 현재 무조건 직접 호출한다.', 'authored-pf1': 'S4는 S3를 현재 무조건 직접 호출한다.'}


def _load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('product_fitness_file:'+path.name)
    return json.loads(path.read_text(encoding='utf-8'))


def load_spec(root=CRITERIA_ROOT):
    spec = _load(Path(root)/'spec.json')
    if spec.get('spec_id') != 'extraction-product-fitness-v1' or spec.get('schema_version') != 1:
        raise ValueError('product_fitness_spec_identity')
    _policies(spec)
    return spec


def _policies(spec):
    policies = spec['policies']
    if set(policies) != set(IMPLEMENTED_DECISIONS): raise ValueError('product_fitness_policy_set')
    for key, policy in policies.items():
        if policy['decision'] not in IMPLEMENTED_DECISIONS[key]: raise ValueError('product_fitness_policy_unimplemented:'+key)
        for row in [policy]+policy['rejected_alternatives']:
            if row.get('obligation_scope') not in OBLIGATION_SCOPES: raise ValueError('product_fitness_policy_scope:'+key)
    return {key: policy['decision'] for key, policy in policies.items()}


def with_policy(spec, policy_id, decision, label=None):
    """Spec copy with one rejected alternative swapped in, for sensitivity runs."""
    result = copy.deepcopy(spec); policy = result['policies'][policy_id]
    found = [row for row in policy['rejected_alternatives']
             if row['decision'] == decision and (label is None or row.get('label') == label)]
    if len(found) != 1: raise ValueError('product_fitness_alternative:'+policy_id)
    policy['decision'] = decision
    policy.pop('obligation_ids', None); policy.pop('obligation_scope', None)
    policy.update(copy.deepcopy({key: value for key, value in found[0].items() if key in ('obligation_ids', 'obligation_scope')}))
    _policies(result)
    return result


def validate_chunk_registry(source, offset, text, row, predicates):
    """Reject an obligation, substitute, coverage or gap row the v1 layers cannot trust."""
    def fail(reason): raise ValueError('product_fitness_registry:'+reason)
    end = offset+len(text); ids = []
    for obligation in row['obligations']:
        canonical = obligation['canonical']; ids.append(obligation['id'])
        if (source[canonical['start']:canonical['end']] != canonical['text'] or
                not offset <= canonical['start'] < canonical['end'] <= end):
            fail('canonical_text')
        if text.count(canonical['text']) != 1: fail('canonical_not_unique')
        markers = obligation['required_markers']['markers']
        if not markers or any(not marker or marker not in canonical['text'] for marker in markers):
            fail('marker_not_in_canonical')
        for substitute in obligation['substitutes']:
            if source[substitute['start']:substitute['end']] != substitute['text']: fail('substitute_text')
            if not canonical['start'] <= substitute['start'] < substitute['end'] <= canonical['end']:
                fail('substitute_outside_canonical')
            if text.count(substitute['text']) != 1: fail('substitute_not_unique')
            if any(marker not in substitute['text'] for marker in markers): fail('substitute_marker_missing')
    if len(ids) != len(set(ids)): fail('obligation_identity')
    if sorted(coverage['predicate'] for coverage in row['coverage']) != sorted(predicates): fail('coverage_predicates')
    for coverage in row['coverage']:
        if coverage['expected_status'] not in STATUSES or not set(coverage['required_obligation_ids']) <= set(ids):
            fail('coverage_row')
    pairs = [tuple(pair) for pair in row['gap_pairs']]
    if len(pairs) != len(set(pairs)) or any(code not in GAP_CODES or key not in ids for code, key in pairs):
        fail('gap_pairs')
    return row


def load_registry(root=CRITERIA_ROOT):
    registry = _load(Path(root)/'registry.json')
    shas = {row['id']: row['sha256'] for row in load_manifest(DATA_ROOT/'v2')['cases']}
    if set(registry['cases']) != set(REGISTRY_CASES): raise ValueError('product_fitness_registry_cases')
    for case_id, entry in registry['cases'].items():
        case = load_case(case_id, DATA_ROOT/'v2')
        if entry['case_sha256'] != shas[case_id] or entry['source_sha256'] != case['source_sha256']:
            raise ValueError('product_fitness_registry_case_identity')
        slices = list(chunks(case['source']))
        if [row['index'] for row in entry['chunks']] != list(range(1, len(slices)+1)):
            raise ValueError('product_fitness_registry_chunks')
        predicates = [r['id'] for r in case['dictionary']['relations']]
        for row, (offset, text) in zip(entry['chunks'], slices, strict=True):
            validate_chunk_registry(case['source'], offset, text, row, predicates)
    return registry


def load_authored(root=CRITERIA_ROOT):
    """The entity-substituted, line-reordered short variant, in the case shape _production accepts."""
    return validate_authored(_load(Path(root)/'authored-pf1.json'))


def validate_authored(doc):
    case = doc['case']
    if (doc.get('fixture_id') != 'authored-pf1' or case['case_id'] != 'authored-pf1' or
            case['source_sha256'] != hashlib.sha256(case['source'].encode('utf-8')).hexdigest()):
        raise ValueError('product_fitness_authored_identity')
    if validate_dictionary(case['dictionary'], case['source']) != case['dictionary']:
        raise ValueError('product_fitness_authored_dictionary')
    slices = list(chunks(case['source']))
    template = doc['template_of']
    if len(slices) != 1 or doc['chunk']['index'] != 1 or template['case_id'] != 'short':
        raise ValueError('product_fitness_authored_chunks')
    shas = {row['id']: row['sha256'] for row in load_manifest(DATA_ROOT/'v2')['cases']}
    if template['case_sha256'] != shas['short']: raise ValueError('product_fitness_authored_template_identity')
    derived = doc['chunk']['derived_from']
    if (derived['template_case_id'], derived['case_sha256'], derived['annotation_index']) != \
            (template['case_id'], template['case_sha256'], template['annotation_index']) or not derived['relation']:
        raise ValueError('product_fitness_authored_template')
    offset, text = slices[0]
    validate_chunk_registry(case['source'], offset, text, doc['chunk'], [r['id'] for r in case['dictionary']['relations']])
    return doc


def _declared_view(case, index):
    annotation = case['annotations'][index-1]
    return {'obligations': [[c['id'], c['evidence']['start'], c['evidence']['end'], c['evidence']['text']] for c in annotation['claims']],
            'coverage': [[c['predicate'], c['status'], c['claim_ids']] for c in annotation['coverage']],
            'gap_pairs': [[g['code'], key] for g in annotation['gaps'] for key in g['claim_ids']]}


def provenance_differences(registry, case_loader=None):
    """Undeclared differences between registry chunks and their v2 annotations."""
    case_loader = case_loader or (lambda case_id: load_case(case_id, DATA_ROOT/'v2'))
    result = []
    for case_id, entry in registry['cases'].items():
        case = case_loader(case_id)
        for row in entry['chunks']:
            source = row['derived_from']
            if source != {'case_id': case_id, 'case_sha256': entry['case_sha256'], 'annotation_index': row['index']}:
                result.append({'case_id': case_id, 'chunk': row['index'], 'field': 'derived_from'}); continue
            v2 = _declared_view(case, row['index'])
            v1 = {'obligations': [[o['id'], o['canonical']['start'], o['canonical']['end'], o['canonical']['text']] for o in row['obligations']],
                  'coverage': [[c['predicate'], c['expected_status'], c['required_obligation_ids']] for c in row['coverage']],
                  'gap_pairs': [list(pair) for pair in row['gap_pairs']]}
            for field in v2:
                if v1[field] == v2[field]: continue
                declared = any(d['field'] == field and d['v2'] == v2[field] and d['v1'] == v1[field] and
                               d.get('rationale') and d.get('author') for d in row['deviations'])
                if not declared: result.append({'case_id': case_id, 'chunk': row['index'], 'field': field, 'v2': v2[field], 'v1': v1[field]})
    return result


def _occurrence(text, offset, quote):
    first = text.find(quote)
    if first < 0 or text.find(quote, first+1) >= 0: raise ValueError('product_fitness_internal:quote_not_unique')
    return offset+first, offset+first+len(quote)


def _support(occurrence, obligation):
    start, end = occurrence
    if start <= obligation['canonical']['start'] and end >= obligation['canonical']['end']: return 'span_supported'
    for substitute in obligation['substitutes']:
        if start <= substitute['start'] and end >= substitute['end']: return 'substitute_supported'
    return None


def _covered_by_union(source, occurrences, start, end):
    """Every non-whitespace character of [start, end) lies in some quote occurrence."""
    return all(source[p].isspace() or any(left <= p < right for left, right in occurrences) for p in range(start, end))


def evaluate_chunk(source, offset, text, row, normalized, spec):
    """D3-D6 on one production-normalized output."""
    decisions = _policies(spec)
    gap_policy = spec['policies']['P-GAP-QUALIFIER-DUP']
    obligations = {o['id']: o for o in row['obligations']}
    claims = normalized['claims']
    occurrences = [_occurrence(text, offset, claim['quote']) for claim in claims]
    supports = [{key: reason for key, o in obligations.items() if (reason := _support(occ, o))} for occ in occurrences]
    failures: list[dict] = []; exemptions: list[dict] = []; obligation_rows = []
    failed_d3 = set()
    for key, obligation in obligations.items():
        supporting = [{'claim_index': i, 'reason': s[key]} for i, s in enumerate(supports) if key in s]
        reason = None
        if not supporting:
            spans = [(obligation['canonical']['start'], obligation['canonical']['end'])]
            spans += [(s['start'], s['end']) for s in obligation['substitutes']]
            joined = any(_covered_by_union(source, occurrences, start, end) for start, end in spans)
            reason = 'split_across_quotes' if joined else 'unreviewed_quote'
            failed_d3.add(key)
            failures.append({'layer': 'D3', 'reason': reason, 'kind': 'evidence', 'obligation_ids': [key]})
        obligation_rows.append({'id': key, 'supporting_claims': supporting, 'failure': reason})

    def kind(keys, reason):
        if reason == 'missing_link' and decisions['P-FAILURE-KIND'] == 'missing_link_always_evidence': return 'evidence'
        return 'evidence' if set(keys) & failed_d3 else 'label'

    by_predicate = {c['predicate']: c for c in normalized['coverage']}
    linked_from_coverage = set().union(*(supports[i] for c in normalized['coverage'] for i in c['claim_indices']), set())
    coverage_rows = []
    for expected in row['coverage']:
        actual = by_predicate[expected['predicate']]
        linked = set().union(*(supports[i] for i in actual['claim_indices']), set())
        missing = [key for key in expected['required_obligation_ids'] if key not in linked]
        for key in missing:
            failure = {'layer': 'D4', 'reason': 'missing_link', 'kind': kind([key], 'missing_link'),
                       'predicate': expected['predicate'], 'obligation_ids': [key]}
            if decisions['P-COVERAGE-LINK'] == 'links_optional': exemptions.append(failure | {'policy': 'P-COVERAGE-LINK'})
            else: failures.append(failure)
        if actual['status'] != expected['expected_status']:
            failure = {'layer': 'D6', 'reason': 'status_mismatch', 'predicate': expected['predicate'],
                       'expected': expected['expected_status'], 'actual': actual['status'],
                       'obligation_ids': list(expected['required_obligation_ids'])}
            failure['kind'] = kind(failure['obligation_ids'], 'status_mismatch')
            exempt = (decisions['P-STATUS-HISTORICAL'] == 'historical_no_evidence_linked' and
                      expected['expected_status'] == 'not_representable' and actual['status'] == 'no_evidence' and
                      expected['required_obligation_ids'] and not missing)
            if exempt: exemptions.append(failure | {'policy': 'P-STATUS-HISTORICAL'})
            else: failures.append(failure)
        coverage_rows.append({'predicate': expected['predicate'], 'linked_obligation_ids': sorted(linked),
                              'missing_obligation_ids': missing, 'status': actual['status'],
                              'expected_status': expected['expected_status']})

    expected_pairs = [tuple(pair) for pair in row['gap_pairs']]
    present: set[tuple] = set()
    for gap_index, gap in enumerate(normalized['dictionary_gaps']):
        wanted = [key for code, key in expected_pairs if code == gap['code']]
        for claim_index in gap['claim_indices']:
            keys = [key for key in wanted if key in supports[claim_index]]
            if not keys:
                failures.append({'layer': 'D5', 'reason': 'unrelated_gap_link', 'kind': kind(wanted, 'unrelated_gap_link'),
                                 'code': gap['code'], 'gap_index': gap_index, 'claim_index': claim_index, 'obligation_ids': wanted})
            present.update((gap['code'], key) for key in keys)
    for code, key in expected_pairs:
        if (code, key) in present: continue
        failure = {'layer': 'D5', 'reason': 'missing_gap_pair', 'kind': kind([key], 'missing_gap_pair'), 'code': code, 'obligation_ids': [key]}
        exempt = (gap_policy['decision'] == 'exempt_linked_from_coverage' and code == QUALIFIER_CODE and
                  (gap_policy.get('obligation_scope') == 'all_qualifier_pairs' or key in gap_policy.get('obligation_ids', [])) and
                  key in linked_from_coverage)
        if exempt: exemptions.append(failure | {'policy': 'P-GAP-QUALIFIER-DUP'})
        else: failures.append(failure)

    claim_rows = [{'index': i, 'supports': dict(s),
                   'semantic': 'semantic_review_required' if s else 'out_of_scope'} for i, s in enumerate(supports)]
    for failure in failures+exemptions:
        if failure['reason'] not in spec['reasons']: raise ValueError('product_fitness_internal:reason')
        failure['category'] = CATEGORY_BY_KIND[failure['kind']]
    return {'claims': claim_rows, 'obligations': obligation_rows, 'coverage': coverage_rows,
            'failures': failures, 'exemptions': exemptions}


def assess(case, chunk_outputs, registry_case, spec, judgments=()):
    """D1 via the production replay, then D3-D6, then the overall verdict."""
    slices = list(chunks(case['source']))
    if sorted(row['index'] for row in chunk_outputs) != list(range(1, len(slices)+1)):
        raise ValueError('product_fitness_chunk_identity')
    results, failures = [], []
    for item in sorted(chunk_outputs, key=lambda row: row['index']):
        index = item['index']; offset, text = slices[index-1]
        production = _production(case, index, item['output'])
        row = {'index': index, 'production_passed': production['production_passed']}
        if not production['production_passed']:
            failure = {'layer': 'D1', 'reason': 'production_failure', 'kind': None, 'category': None, 'chunk': index,
                       'production_error': production['production_error'], 'failed_stage': production['failed_stage']}
            row.update(failures=[failure], exemptions=[], claims=[]); failures.append(failure)
        else:
            row.update(evaluate_chunk(case['source'], offset, text, registry_case['chunks'][index-1],
                                      production['normalized_output'], spec))
            failures.extend(f | {'chunk': index} for f in row['failures'])
            normalized = production['normalized_output']['claims']
            for claim in row['claims']:
                content = {'text': normalized[claim['index']]['text'], 'quote': normalized[claim['index']]['quote']}
                claim['agent_flags'] = [j['id'] for j in judgments if j['case_id'] == case['case_id'] and
                                        j['chunk'] == index and j['claim'] == content and j['verdict'] == 'material_failure']
        results.append(row)
    deterministic = 'fail' if failures else 'pass'
    chunk_outputs_sha256 = digest(chunk_outputs)
    approvals = [r for r in spec['semantic_approvals'] if r['case_id'] == case['case_id'] and
                 r['chunk_outputs_sha256'] == chunk_outputs_sha256 and r['human_review'] == 'approved']
    flags = sorted({flag for row in results for claim in row['claims'] for flag in claim.get('agent_flags', [])})
    if deterministic == 'fail' or any(r['verdict'] == 'material_failure' for r in approvals):
        overall, reasons = 'fail', sorted({f['reason'] for f in failures}) or ['human_material_failure']
    elif any(r['verdict'] == 'no_material_failure' for r in approvals):
        overall, reasons = 'pass', []
    elif flags:
        overall, reasons = 'undecided_agent_flagged', ['semantic_review_required']
    else:
        overall, reasons = 'undecided', ['semantic_review_required']
    return {'case_id': case['case_id'], 'chunk_outputs_sha256': chunk_outputs_sha256, 'chunks': results,
            'production_passed': all(row['production_passed'] for row in results),
            'deterministic': deterministic, 'failures': failures, 'agent_flags': flags,
            'categories': sorted({f['category'] for f in failures if f['category'] is not None}),
            'human_approvals': [r['id'] for r in approvals], 'overall': overall, 'overall_reasons': reasons}


def failure_vector(result):
    """Sorted unique (reason, kind) pairs: the comparable deterministic verdict shape."""
    return sorted({(f['reason'], f['kind']) for f in result['failures']}, key=lambda pair: (pair[0], pair[1] or ''))


def baseline_output(row, facts):
    """Registry-derived oracle output in the shape of the v2 synthetic oracle."""
    claims = [{'heading': o['id'], 'text': o['canonical']['text'], 'quote': o['canonical']['text']} for o in row['obligations']]
    ids = {o['id']: i for i, o in enumerate(row['obligations'])}
    facts = copy.deepcopy(facts); gaps: dict[str, list[int]] = {}
    for code, key in row['gap_pairs']: gaps.setdefault(code, []).append(ids[key])
    return {'title': 'Synthetic threshold fixture', 'claims': claims, 'facts': facts,
            'coverage': [{'predicate': c['predicate'], 'status': c['expected_status'],
                          'claim_indices': [ids[key] for key in c['required_obligation_ids']],
                          'fact_indices': [i for i, f in enumerate(facts) if f['predicate'] == c['predicate']],
                          'reason': 'Versioned synthetic evidence annotation'} for c in row['coverage']],
            'dictionary_gaps': [{'code': code, 'claim_indices': indices, 'detail': 'Versioned synthetic dictionary constraint'}
                                for code, indices in gaps.items()]}


def _marker_free_segment(obligation):
    """Longest piece of the substitute containing no required marker: a qualifier-deleted quote."""
    pieces = [obligation['substitutes'][0]['text']]
    for marker in obligation['required_markers']['markers']:
        pieces = [part for piece in pieces for part in piece.split(marker)]
    return max((piece.strip() for piece in pieces), key=len)


def fixtures(case, row, baseline):
    """Recipes applied to one single-chunk baseline. Returns {fixture_id: output}."""
    offset, text = list(chunks(case['source']))[row['index']-1]
    obligations = {o['id']: o for o in row['obligations']}
    ids = {o['id']: i for i, o in enumerate(row['obligations'])}
    result = {'baseline': copy.deepcopy(baseline)}
    out = copy.deepcopy(baseline); size = len(out['claims'])
    out['claims'].reverse(); out['coverage'].reverse(); out['dictionary_gaps'].reverse()
    for item in out['coverage']+out['dictionary_gaps']: item['claim_indices'] = [size-1-i for i in item['claim_indices']]
    result['order_remap'] = out
    start = min(o['canonical']['start'] for o in row['obligations']); end = max(o['canonical']['end'] for o in row['obligations'])
    span = case['source'][start:end]
    out = copy.deepcopy(baseline); out['claims'] = [{'heading': baseline['claims'][0]['heading'], 'text': span, 'quote': span}]
    for item in out['coverage']+out['dictionary_gaps']:
        if item['claim_indices']: item['claim_indices'] = [0]
    result['merge_contiguous'] = out
    out = copy.deepcopy(baseline)
    for claim, obligation in zip(out['claims'], row['obligations'], strict=True): claim['quote'] = obligation['substitutes'][0]['text']
    result['label_free_substitutes'] = out
    out = copy.deepcopy(baseline)
    for claim in out['claims']: claim['quote'] = span
    result['longer_quote'] = out
    out = copy.deepcopy(baseline)
    out['dictionary_gaps'] = [dict(item, claim_indices=[i]) for item in out['dictionary_gaps'] for i in item['claim_indices']]
    result['gap_split'] = out
    out = copy.deepcopy(out); grouped: dict[str, dict] = {}
    for item in out['dictionary_gaps']:
        grouped.setdefault(item['code'], dict(item, claim_indices=[]))['claim_indices'].extend(item['claim_indices'])
    out['dictionary_gaps'] = list(grouped.values())
    result['gap_group'] = out
    out = copy.deepcopy(baseline)
    for claim, obligation in zip(out['claims'], row['obligations'], strict=True): claim['heading'] = 'evidence '+obligation['id']
    for item in out['coverage']: item['reason'] = text.splitlines()[0].split(': ', 1)[1]
    for item in out['dictionary_gaps']: item['detail'] = 'synthetic dictionary constraint'
    result['incidental_wording'] = out
    for fixture_id, key in (('negation_deleted', 'negated'), ('condition_deleted', 'conditional'), ('time_deleted', 'historical')):
        out = copy.deepcopy(baseline); out['claims'][ids[key]]['quote'] = _marker_free_segment(obligations[key])
        result[fixture_id] = out
    out = copy.deepcopy(baseline)
    for item in out['claims']+out['facts']: item['quote'] = item['quote'].split(': ', 1)[1]
    result['generic_prefix_strip'] = out
    out = copy.deepcopy(baseline)
    next(g for g in out['dictionary_gaps'] if g['code'] == 'missing_relation')['claim_indices'] = [ids['missing_entity']]
    result['wrong_gap_link'] = out
    out = copy.deepcopy(baseline); calls = next(c for c in out['coverage'] if c['predicate'] == 'calls')
    calls['claim_indices'] = [i for i in calls['claim_indices'] if i != ids['missing_entity']]
    result['coverage_link_dropped'] = out
    out = copy.deepcopy(baseline); out['claims'][ids['negated']]['text'] = CONTRADICTORY_TEXT[case['case_id']]
    result['contradictory_prose'] = out
    out = copy.deepcopy(baseline); index = ids['conditional']
    first, second = obligations['conditional']['substitutes'][0]['text'].split('. ', 1)
    out['claims'][index].update(text=first+'.', quote=first+'.')
    out['claims'].append({'heading': 'conditional', 'text': second, 'quote': second})
    for item in out['coverage']+out['dictionary_gaps']:
        if index in item['claim_indices']: item['claim_indices'] = item['claim_indices']+[size]
    result['split_conditional_claim'] = out
    out = copy.deepcopy(baseline); next(c for c in out['coverage'] if c['predicate'] == 'archives')['status'] = 'no_evidence'
    result['historical_status_no_evidence'] = out
    out = copy.deepcopy(baseline); qualifier = next(g for g in out['dictionary_gaps'] if g['code'] == QUALIFIER_CODE)
    qualifier['claim_indices'] = [i for i in qualifier['claim_indices'] if i != ids['historical']]
    result['historical_qualifier_gap_omitted'] = out
    out = copy.deepcopy(baseline); line = next(line for line in text.splitlines() if '가정 예시' in line).strip()
    out['claims'].append({'heading': 'evidence', 'text': line, 'quote': line})
    result['hypothetical_line_claim'] = out
    return result


def _load_review():
    if _hash(REVIEW_PATH) != REVIEW_SHA256: raise ValueError('product_fitness_785_review_identity')
    return _load(REVIEW_PATH)


def _review_controls():
    controls = _load_review()['controlled_contrasts']
    for row in controls['cases']:
        if digest(row['output']) != row['output_sha256']: raise ValueError('product_fitness_785_output_identity:'+row['id'])
    return controls


def _v2_observed(case, output):
    result = assess_outputs(case, [{'index': 1, 'output': output}])
    return {'production_passed': result['production_passed'],
            'finite_passed': result['finite_gold']['passed'] if result['finite_gold'] is not None else None}


def _differs(v2_finite, overall):
    return bool(v2_finite) != (overall in ('undecided', 'pass'))


def _fixture_report(case, registry_case, spec, judgments, outputs, *, compare_v2):
    rows = {}
    for fixture_id, output in outputs.items():
        result = assess(case, [{'index': 1, 'output': output}], registry_case, spec, judgments)
        row = {'output_sha256': digest(output), 'chunk_outputs_sha256': result['chunk_outputs_sha256'],
               'production_passed': result['production_passed'],
               'deterministic': result['deterministic'], 'failures': [list(p) for p in failure_vector(result)],
               'categories': result['categories'], 'agent_flags': result['agent_flags'], 'overall': result['overall'],
               'claims': [{'index': c['index'], 'semantic': c['semantic'], 'supports': c['supports']}
                          for chunk in result['chunks'] for c in chunk['claims']]}
        if compare_v2: row['v2'] = _v2_observed(case, output)
        rows[fixture_id] = row
    return rows


def _check_declarations(spec, rows_by_owner):
    """Check declared spec subjects against their owners and the computed rows; raise on a mismatch."""
    for key, key_owner in DECLARATION_KEYS.items():
        subjects = [d['subject'] for d in spec[key]]
        if len(subjects) != len(set(subjects)): raise ValueError('product_fitness_duplicate_declaration:'+key)
        for subject in subjects:
            owner = next((owner for prefix, owner in DECLARATION_OWNERS.items() if subject.startswith(prefix)), None)
            if owner is None: raise ValueError('product_fitness_unowned_declaration:'+subject)
            if owner not in rows_by_owner: raise ValueError('product_fitness_unchecked_owner:'+owner)
            if owner != key_owner: raise ValueError('product_fitness_misfiled_declaration:'+subject)
            if subject not in rows_by_owner[owner]: raise ValueError('product_fitness_orphan_declaration:'+subject)


def _related_failures(error, failures):
    """v1 failures that a frozen v2 annotation error points at, derived from the error's own fields."""
    reason = error['reason']
    if reason == 'missing_claim_evidence':
        return [f for f in failures if f['layer'] == 'D3' and f['obligation_ids'] == [error['id']]]
    if reason == 'coverage_semantics':
        return [f for f in failures if f['layer'] in ('D4', 'D6') and f['predicate'] == error['predicate']]
    if reason == 'unexpected_gap_evidence':
        return [f for f in failures if f['reason'] == 'unrelated_gap_link' and f['code'] == error['code']]
    if reason == 'gap_completeness':
        # v2 raises gap_completeness when an expected (code, obligation) pair is absent.
        return [f for f in failures if f['reason'] == 'missing_gap_pair']
    raise ValueError('product_fitness_mapping_unsupported_error:'+reason)


def _cause_keys(chunk, failure, failed_d3):
    """Cause keys (chunk, target, basis) for one related v1 failure.

    Shared-cause collapse: a failure needing an obligation that fails D3 belongs
    to that D3 cause. An unrelated gap link needs its code's expected
    obligations, which evaluate_chunk stores as its obligation_ids. Any other
    failure gives a single key, formed by the branches below.
    """
    failing = [key for key in failure['obligation_ids'] if key in failed_d3]
    if failing: return [(chunk, key, 'D3') for key in failing]
    if failure['reason'] == 'missing_link': return [(chunk, failure['obligation_ids'][0], 'P-COVERAGE-LINK')]
    if failure['reason'] == 'status_mismatch': return [(chunk, 'predicate:'+failure['predicate'], 'D6')]
    if failure['reason'] == 'missing_gap_pair' and failure['code'] == QUALIFIER_CODE:
        return [(chunk, failure['obligation_ids'][0], 'P-GAP-QUALIFIER-DUP')]
    return [(chunk, 'code:'+failure['code'], 'D5:'+failure['code'])]


def _error_target(error):
    return ':'.join([error['reason']]+[str(error[key]) for key in ('id', 'predicate', 'code') if key in error])


def map_errors(errors, evaluations):
    """Map frozen v2 errors to unique v1 causes; evaluations[chunk] is evaluate_chunk's result."""
    rows: list[dict] = []; causes: dict[tuple, dict] = {}
    for ordinal, error in enumerate(errors, 1):
        chunk = error['chunk']; evaluation = evaluations[chunk]
        failed_d3 = {row['id'] for row in evaluation['obligations'] if row['failure']}
        related = _related_failures(error, evaluation['failures'])
        exempted = _related_failures(error, evaluation['exemptions'])
        keys: dict[tuple, set] = {}
        for failure in related:
            for key in _cause_keys(chunk, failure, failed_d3): keys.setdefault(key, set()).add(failure['category'])
        if not related: keys[(chunk, _error_target(error), 'v1_pass')] = {'representation_sensitive'}
        error_id = f'error-{ordinal}'
        for key, categories in keys.items():
            cause = causes.setdefault(key, {'categories': set(), 'error_ids': []})
            cause['categories'] |= categories; cause['error_ids'].append(error_id)
        rows.append({'id': error_id, 'frozen_error': copy.deepcopy(error),
                     'v1_failures': [{k: f[k] for k in ('layer', 'reason', 'kind', 'category', 'obligation_ids', 'predicate', 'code') if k in f}
                                     for f in related],
                     'v1_exempted': [{k: f[k] for k in ('layer', 'reason', 'policy', 'obligation_ids', 'predicate', 'code') if k in f}
                                     for f in exempted],
                     'cause_keys': sorted(keys)})
    cause_rows = []
    for (chunk, target, basis), cause in sorted(causes.items()):
        if len(cause['categories']) != 1: raise ValueError('product_fitness_internal:mixed_cause_category')
        category = next(iter(cause['categories']))
        cause_rows.append({'id': f'c{chunk}:{target}:{basis}', 'chunk': chunk, 'target': target, 'basis': basis,
                           'category': category, 'error_ids': cause['error_ids']})
    by_key = {(c['chunk'], c['target'], c['basis']): c for c in cause_rows}
    for row in rows:
        row['causes'] = [by_key[key]['id'] for key in row.pop('cause_keys')]
        row['categories'] = sorted({c['category'] for c in cause_rows if c['id'] in row['causes']})
    counts = {category: sum(c['category'] == category for c in cause_rows)
              for category in ('product_evidence', 'unresolved', 'representation_sensitive')}
    return {'errors': rows, 'causes': cause_rows, 'cause_counts': counts | {'total': len(cause_rows)}}


def _historical_evaluations(diagnosis, registry, spec):
    case = load_case('three', DATA_ROOT/'v2'); entry = registry['cases']['three']
    if diagnosis['source_sha256'] != entry['source_sha256'] or not diagnosis['production_passed']:
        raise ValueError('product_fitness_historical_identity')
    slices = list(chunks(case['source']))
    result = {}
    for row in diagnosis['chunks']:
        offset, text = slices[row['index']-1]
        result[row['index']] = evaluate_chunk(case['source'], offset, text, entry['chunks'][row['index']-1], row['normalized_output'], spec)
    return result


def _review_errors(errors):
    rows = _load_review()['criteria_review']['errors']
    if [r['id'] for r in rows] != [f'error-{i}' for i in range(1, len(errors)+1)] or \
            [r['observations']['frozen_error'] for r in rows] != errors:
        raise ValueError('product_fitness_785_error_identity')
    return rows


def historical_mapping(spec, registry, diagnosis):
    """The frozen v2 errors of the authenticated 768 run, mapped to v1 causes and joined with #785."""
    errors = diagnosis['finite_gold']['annotation_errors']
    review = _review_errors(errors)
    result = map_errors(errors, _historical_evaluations(diagnosis, registry, spec))
    correspondence = spec['group_correspondence_785']
    for row, reviewed in zip(result['errors'], review, strict=True):
        chunk = row['frozen_error']['chunk']
        groups = [j['group'] for j in reviewed['agent_proposed_review']['judgments']]
        if any(group not in correspondence for group in groups): raise ValueError('product_fitness_785_group_unmapped')
        expected = {f'c{chunk}:{c["target"]}:{c["basis"]}' for group in groups for c in correspondence[group]['causes']}
        categories_785 = sorted({CATEGORY_NAMES_785[c] for c in reviewed['agent_proposed_review']['categories']})
        row['review_785'] = {'groups': groups, 'categories': reviewed['agent_proposed_review']['categories'],
                             'causes_only_v1': sorted(set(row['causes'])-expected),
                             'causes_only_785': sorted(expected-set(row['causes'])),
                             'categories_only_v1': sorted(set(row['categories'])-set(categories_785)),
                             'categories_only_785': sorted(set(categories_785)-set(row['categories']))}
        row['review_785']['agrees'] = not (row['review_785']['causes_only_v1'] or row['review_785']['causes_only_785'])
    return result


def _check_historical_disagreements(spec, mapping):
    declared = {d['subject']: d for d in spec['historical_mapping_disagreements_with_785']}
    for row in mapping['errors']:
        subject = 'historical:'+row['id']
        if not row['review_785']['agrees'] and subject not in declared:
            raise ValueError('product_fitness_undeclared_historical_disagreement:'+subject)
        if row['review_785']['agrees'] and subject in declared:
            raise ValueError('product_fitness_spurious_historical_disagreement:'+subject)
        if subject in declared and (declared[subject]['causes_only_v1'], declared[subject]['causes_only_785']) != \
                (row['review_785']['causes_only_v1'], row['review_785']['causes_only_785']):
            raise ValueError('product_fitness_historical_disagreement_value:'+subject)


def sensitivity(spec, registry, diagnosis, baseline):
    """Re-run the mapping under each executable rejected alternative; record what moves."""
    rows = []
    for policy_id, policy in spec['policies'].items():
        for alternative in policy['rejected_alternatives']:
            row = {'policy': policy_id, 'decision': alternative['decision'], 'label': alternative.get('label')}
            if alternative['decision'] not in IMPLEMENTED_DECISIONS[policy_id]:
                rows.append(row | {'executable': False}); continue
            changed = with_policy(spec, policy_id, alternative['decision'], alternative.get('label'))
            result = map_errors(diagnosis['finite_gold']['annotation_errors'], _historical_evaluations(diagnosis, registry, changed))
            moved = []
            for before, after in zip(baseline['errors'], result['errors'], strict=True):
                if (before['causes'], before['categories']) != (after['causes'], after['categories']):
                    moved.append({'id': before['id'], 'categories': after['categories'],
                                  'causes_added': sorted(set(after['causes'])-set(before['causes'])),
                                  'causes_removed': sorted(set(before['causes'])-set(after['causes']))})
            rows.append(row | {'executable': True, 'cause_counts': result['cause_counts'], 'moved_errors': moved})
    return rows


def report_source_paths(repo=REPO):
    """The hashed source set: criteria JSON, this module, the script and tests/test_product_fitness_v1*.py.

    From the criteria directory only `*.json` files whose names do not start with '.' are taken.
    """
    criteria = repo/CRITERIA_ROOT.relative_to(REPO)
    paths = sorted(p.relative_to(repo).as_posix() for p in criteria.glob('*.json') if not p.name.startswith('.'))
    paths += ['verinote/diagnostics/product_fitness_v1.py', 'scripts/verify_product_fitness_v1.py']
    paths += sorted(p.relative_to(repo).as_posix() for p in (repo/'tests').glob('test_product_fitness_v1*.py'))
    return paths


def render(report):
    return json.dumps(report, ensure_ascii=False, indent=2)+'\n'


def verify_proposal(spec=None, registry=None):
    """Recompute the fixture verdicts, v2 contrasts and the historical mapping.

    A mismatch raises ValueError from a check in this function or in a helper it
    calls; a report field that none of those checks compares is reported only.
    """
    spec = spec if spec is not None else load_spec()
    registry = registry if registry is not None else load_registry()
    undeclared = provenance_differences(registry)
    if undeclared: raise ValueError('product_fitness_undeclared_provenance_difference')
    case = load_case('short', DATA_ROOT/'v2'); registry_case = registry['cases']['short']
    baseline = ideal_outputs(case, contract_version=2)[0]['output']
    facts = [dict(zip(('subject', 'predicate', 'object'), g['variants'][0], strict=True), quote=g['sample_quote']) for g in case['gold']]
    if baseline_output(registry_case['chunks'][0], facts) != baseline: raise ValueError('product_fitness_baseline_builder')
    judgments = spec['semantic_judgments']
    fixture_rows = {'short': _fixture_report(case, registry_case, spec, judgments,
                                             fixtures(case, registry_case['chunks'][0], baseline), compare_v2=True)}
    authored = load_authored(); authored_registry = {'chunks': [authored['chunk']]}
    authored_baseline = baseline_output(authored['chunk'], authored['facts'])
    fixture_rows['authored-pf1'] = _fixture_report(authored['case'], authored_registry, spec, authored['semantic_judgments'],
                                                   fixtures(authored['case'], authored['chunk'], authored_baseline), compare_v2=False)
    for case_id, rows in fixture_rows.items():
        if set(rows) != set(spec['fixture_expectations']): raise ValueError('product_fitness_fixture_set:'+case_id)
        for fixture_id, row in rows.items():
            expected = spec['fixture_expectations'][fixture_id]
            if row['overall'] != expected['overall'] or row['failures'] != expected['failures']:
                raise ValueError(f'product_fitness_fixture_expectation:{case_id}:{fixture_id}')
    differences = []
    for control in _review_controls()['cases']:
        result = assess(case, [{'index': 1, 'output': control['output']}], registry_case, spec, judgments)
        v2 = _v2_observed(case, control['output'])
        if v2 != {key: control['observed'][key] for key in ('production_passed', 'finite_passed')}:
            raise ValueError('product_fitness_785_observation:'+control['id'])
        differences.append({'subject': 'control:'+control['id'], 'output_sha256': control['output_sha256'],
                            'chunk_outputs_sha256': result['chunk_outputs_sha256'],
                            'production_passed': result['production_passed'], 'v2_finite_passed': v2['finite_passed'],
                            'v1_deterministic': result['deterministic'], 'v1_failures': [list(p) for p in failure_vector(result)],
                            'v1_agent_flags': result['agent_flags'], 'v1_overall': result['overall'],
                            'expectation_785': control['agent_proposed_product_expectation']['expectation']})
    for case_id, rows in fixture_rows.items():
        if case_id == 'authored-pf1': continue  # not a dataset v2 case: no v2 observation exists
        for fixture_id, row in rows.items():
            differences.append({'subject': f'fixture:{case_id}:{fixture_id}', 'output_sha256': row['output_sha256'],
                                'chunk_outputs_sha256': row['chunk_outputs_sha256'],
                                'production_passed': row['production_passed'], 'v2_finite_passed': row['v2']['finite_passed'],
                                'v1_deterministic': row['deterministic'], 'v1_failures': row['failures'],
                                'v1_agent_flags': row['agent_flags'], 'v1_overall': row['overall'], 'expectation_785': None})
    diagnosis = diagnose()
    mapping = historical_mapping(spec, registry, diagnosis)
    _check_declarations(spec, {'differences_from_v2': {row['subject'] for row in differences},
                               'historical_mapping': {'historical:'+row['id'] for row in mapping['errors']}})
    _check_historical_disagreements(spec, mapping)
    declared = {d['subject']: d for d in spec['deliberate_differences_from_v2']}
    for row in differences:
        row['differs'] = _differs(row['v2_finite_passed'], row['v1_overall'])
        row['deliberate'] = row['subject'] in declared
        if row['differs'] != row['deliberate']: raise ValueError('product_fitness_undeclared_v2_difference:'+row['subject'])
        if row['deliberate']:
            v2 = 'production_failure' if row['v2_finite_passed'] is None else 'pass' if row['v2_finite_passed'] else 'fail'
            if (declared[row['subject']]['v2'], declared[row['subject']]['v1']) != (v2, row['v1_overall']):
                raise ValueError('product_fitness_declared_v2_difference_value:'+row['subject'])
    disagreements = {d['subject']: d for d in spec['disagreements_with_785']}
    for row in differences:
        kinds = sorted({kind for _, kind in row['v1_failures'] if kind is not None})
        row['disagreement_with_785'] = (row['expectation_785'] == 'material_failure' and row['v1_overall'] == 'fail' and
                                        kinds == ['label'])
        if row['disagreement_with_785'] != (row['subject'] in disagreements):
            raise ValueError('product_fitness_undeclared_785_disagreement:'+row['subject'])
        if row['disagreement_with_785']:
            d = disagreements[row['subject']]
            if (d['expectation_785'], d['v1_overall'], d['v1_kinds']) != (row['expectation_785'], row['v1_overall'], kinds):
                raise ValueError('product_fitness_785_disagreement_value:'+row['subject'])
    sensitivity_rows = sensitivity(spec, registry, diagnosis, mapping)
    summary = {'fixture_cases': len(fixture_rows), 'fixture_recipes': len(spec['fixture_expectations']),
               'controls_785': sum(row['subject'].startswith('control:') for row in differences),
               'differences_from_v2_rows': len(differences), 'v2_differences': sum(row['differs'] for row in differences),
               'disagreements_with_785': sum(row['disagreement_with_785'] for row in differences),
               'controls_disagreeing_with_785': [row['subject'] for row in differences if row['disagreement_with_785']],
               'historical_errors': len(mapping['errors']),
               'historical_errors_agreeing_with_785': sum(row['review_785']['agrees'] for row in mapping['errors']),
               'historical_errors_disagreeing_with_785': [row['id'] for row in mapping['errors'] if not row['review_785']['agrees']],
               'historical_causes': mapping['cause_counts']['total'],
               'sensitivity_alternatives': len(sensitivity_rows),
               'sensitivity_executable': sum(row['executable'] for row in sensitivity_rows)}
    return {'schema_version': 1, 'spec_id': spec['spec_id'], 'issue': spec['issue'], 'base_revision': spec['base_revision'],
            'generation_requests': 0, 'author': 'agent_proposed', 'human_review': 'pending',
            'assisted_review': copy.deepcopy(spec['assisted_review']),
            'base_revision_definition': spec['base_revision_definition'],
            'issue_reproduction_revision': spec['issue_reproduction_revision'],
            'review_785_sha256': REVIEW_SHA256,
            'diagnostic_implementation': copy.deepcopy(diagnosis['diagnostic_implementation']),
            'historical_authentication': {'report_sha256': REPORT_SHA256,
                                          'raw_index_sha256': diagnosis['historical_authentication']['raw_index_sha256']},
            'source_hashes': {path: _hash(REPO/path) for path in report_source_paths()},
            'fixtures': fixture_rows, 'differences_from_v2': differences,
            'summary': summary, 'historical_mapping': mapping, 'sensitivity': sensitivity_rows,
            'scope': 'v1 deterministic conformance under agent-proposed policies; prose fitness is never computed.'}
