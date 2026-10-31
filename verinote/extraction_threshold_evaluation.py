"""Dataset v2's finite evidence/coverage/gap checks; not a language-model judge."""
import copy

from .documents import chunks
from .extraction_coverage import GAP_CODES
from .ontology import extraction_quote_issues


def validate_annotations(case):
    slices = list(chunks(case['source']))
    annotations = case['annotations']
    if not isinstance(annotations, list) or len(annotations) != len(slices):
        raise ValueError('v2 annotation chunk count')
    predicates = {r['id'] for r in case['dictionary']['relations']}
    for index, (row, (offset, text)) in enumerate(zip(annotations, slices, strict=True), 1):
        if (not isinstance(row, dict) or set(row) != {'index', 'claims', 'coverage', 'gaps'}
                or type(row['index']) is not int or row['index'] != index
                or not isinstance(row['claims'], list) or not 1 <= len(row['claims']) <= 12):
            raise ValueError('v2 annotation shape')
        ids = set()
        for claim in row['claims']:
            if (not isinstance(claim, dict) or set(claim) != {'id', 'evidence'}
                    or not isinstance(claim['id'], str) or not claim['id'] or claim['id'] in ids):
                raise ValueError('v2 claim identity')
            ids.add(claim['id'])
            evidence = claim['evidence']
            if (not isinstance(evidence, dict) or set(evidence) != {'start', 'end', 'text'}
                    or type(evidence['start']) is not int or type(evidence['end']) is not int
                    or not offset <= evidence['start'] < evidence['end'] <= offset+len(text)
                    or case['source'][evidence['start']:evidence['end']] != evidence['text']
                    or not isinstance(evidence['text'], str) or len(evidence['text']) > 1500
                    or text.count(evidence['text']) != 1):
                raise ValueError('v2 claim evidence')
        if not isinstance(row['coverage'], list) or len(row['coverage']) != len(predicates):
            raise ValueError('v2 coverage annotations')
        seen = set()
        for coverage in row['coverage']:
            if (not isinstance(coverage, dict) or set(coverage) != {'predicate', 'status', 'claim_ids'}
                    or not isinstance(coverage['predicate'], str) or coverage['predicate'] not in predicates
                    or coverage['predicate'] in seen or coverage['status'] not in ('extracted', 'no_evidence', 'not_representable')):
                raise ValueError('v2 coverage identity')
            seen.add(coverage['predicate'])
            _claim_ids(coverage['claim_ids'], ids)
            has_gold = any(v[1] == coverage['predicate'] and any(offset <= e['start'] < e['end'] <= offset+len(text) for e in g['evidence'])
                           for g in case['gold'] for v in g['variants'])
            if has_gold != (coverage['status'] == 'extracted') or (coverage['status'] == 'not_representable' and not coverage['claim_ids']):
                raise ValueError('v2 coverage gold mismatch')
        if not isinstance(row['gaps'], list) or len(row['gaps']) > 12: raise ValueError('v2 gap annotations')
        pairs = set()
        for gap in row['gaps']:
            if not isinstance(gap, dict) or set(gap) != {'code', 'claim_ids'} or not isinstance(gap['code'], str) or gap['code'] not in GAP_CODES:
                raise ValueError('v2 gap code')
            _claim_ids(gap['claim_ids'], ids)
            if not gap['claim_ids']: raise ValueError('v2 gap evidence missing')
            for key in gap['claim_ids']:
                pair = (gap['code'], key)
                if pair in pairs: raise ValueError('v2 duplicate gap annotation')
                pairs.add(pair)


def _claim_ids(value, ids):
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in ids for v in value) or len(value) != len(set(value)):
        raise ValueError('v2 annotation claim linkage')


def _supported(source, quote, offset, text, evidence):
    start = source.find(quote, offset, offset+len(text))
    return start >= 0 and start <= evidence['start'] and start+len(quote) >= evidence['end']


def evaluate_annotations(case, outputs, fact_passed):
    slices = list(chunks(case['source']))
    errors = []
    for item in outputs:
        index, output = item['index'], item['output']
        annotation = case['annotations'][index-1]; offset, text = slices[index-1]
        try:
            if extraction_quote_issues(output, text, case['dictionary'], require_coverage=True, contract_version=2):
                raise ValueError('production_quote_contract')
            supported = [{a['id'] for a in annotation['claims'] if _supported(case['source'], c['quote'].strip(), offset, text, a['evidence'])}
                         for c in output['claims']]
            covered = set().union(*supported)
            for claim in annotation['claims']:
                if claim['id'] not in covered: errors.append({'chunk': index, 'reason': 'missing_claim_evidence', 'id': claim['id']})
            by_predicate = {c['predicate']: c for c in output['coverage']}
            for expected in annotation['coverage']:
                actual = by_predicate[expected['predicate']]
                evidence = set().union(*(supported[i] for i in actual['claim_indices']))
                if actual['status'] != expected['status'] or not set(expected['claim_ids']) <= evidence:
                    errors.append({'chunk': index, 'reason': 'coverage_semantics', 'predicate': expected['predicate']})
            expected_gaps = {(g['code'], key) for g in annotation['gaps'] for key in g['claim_ids']}
            actual_gaps: set[tuple[str, str]] = set()
            for gap in output['dictionary_gaps']:
                for claim_index in gap['claim_indices']:
                    # Evidence spans may legitimately cover several annotations, but
                    # a gap cannot cite unrelated claims to hide its missing evidence.
                    keys = {key for key in supported[claim_index] if (gap['code'], key) in expected_gaps}
                    if not keys: errors.append({'chunk': index, 'reason': 'unexpected_gap_evidence', 'code': gap['code']})
                    actual_gaps.update((gap['code'], key) for key in keys)
            if actual_gaps != expected_gaps: errors.append({'chunk': index, 'reason': 'gap_completeness'})
        except (ValueError, KeyError, TypeError, IndexError):
            errors.append({'chunk': index, 'reason': 'production_contract'})
    complete = {o['index'] for o in outputs} == set(range(1, len(slices)+1))
    if not complete: errors.append({'chunk': None, 'reason': 'missing_chunks'})
    return {'annotation_errors': errors, 'complete': complete, 'passed': fact_passed and not errors}


def annotated_outputs(case):
    from .extraction_evaluation import _canonical, _citation, _triple, validate_case
    validate_case(case)
    outputs = []
    for index, (_offset, text) in enumerate(chunks(case['source']), 1):
        annotation = case['annotations'][index-1]
        ids = {c['id']: i for i, c in enumerate(annotation['claims'])}
        claims = [{'heading': c['id'], 'text': c['evidence']['text'], 'quote': c['evidence']['text']} for c in annotation['claims']]
        facts = []
        for gold in case['gold']:
            if _citation(case, gold['sample_quote'], index, gold)[0] is None:
                triple = _canonical(case, _triple(gold['variants'][0]))
                facts.append(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=gold['sample_quote']))
        coverage = [{'predicate': c['predicate'], 'status': c['status'], 'claim_indices': [ids[key] for key in c['claim_ids']],
                     'fact_indices': [i for i, f in enumerate(facts) if f['predicate'] == c['predicate']], 'reason': 'Versioned synthetic evidence annotation'}
                    for c in annotation['coverage']]
        gaps = [{'code': g['code'], 'claim_indices': [ids[key] for key in g['claim_ids']], 'detail': 'Versioned synthetic dictionary constraint'} for g in annotation['gaps']]
        output = {'title': 'Synthetic threshold fixture', 'claims': claims, 'facts': facts, 'coverage': coverage, 'dictionary_gaps': gaps}
        if extraction_quote_issues(output, text, case['dictionary'], require_coverage=True, contract_version=2):
            raise ValueError('v2 oracle quote contract')
        outputs.append({'index': index, 'output': copy.deepcopy(output)})
    return outputs
