"""Deterministic synthetic extraction evaluation; no model calls or repairs."""
import hashlib
import json
from pathlib import Path

from .documents import chunks
from .ontology import dictionary_fact, validate_dictionary

DATASET_VERSION = 1
EVALUATOR_VERSION = 1
DATA_ROOT = Path(__file__).with_name('benchmark_data')
MAX_FILE_BYTES = 2 * 1024 * 1024


def _read(path, limit=MAX_FILE_BYTES):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit: raise ValueError('평가 파일 크기·종류 오류')
    raw = path.read_bytes()
    if len(raw) > limit: raise ValueError('평가 파일 크기 오류')
    return json.loads(raw.decode('utf-8'))


def _sha(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _triple(value):
    if not isinstance(value, list) or len(value) != 3 or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in value):
        raise ValueError('평가 triple 형식 오류')
    return tuple(v.strip() for v in value)


def _canonical(case, triple):
    aliases = {r['alias']: r['entity'] for r in case['dictionary']['aliases']}
    return (aliases.get(triple[0], triple[0]), triple[1], aliases.get(triple[2], triple[2]))


def _occurrences(source, quote, offset, length):
    positions, cursor = [], offset
    while quote and (found := source.find(quote, cursor, offset+length)) != -1:
        positions.append((found, found+len(quote))); cursor = found+1
        if len(positions) > 64: break
    return positions


def _citation(case, quote, index, gold=None):
    slices = list(chunks(case['source'])); offset, text = slices[index-1]
    if not isinstance(quote, str) or not quote or len(quote) > 8000: return 'invalid_quote', []
    positions = _occurrences(case['source'], quote, offset, len(text))
    if not positions: return 'not_in_origin_chunk', []
    if len(positions) > 64: return 'occurrence_limit', positions[:64]
    if gold is None: return None, positions
    eligible = [pos for pos in positions if any(pos[0] <= e['start'] and pos[1] >= e['end'] for e in gold['evidence'])]
    if not eligible: return 'wrong_or_incomplete_evidence', positions
    if len(eligible) != len(positions): return 'ambiguous_occurrence', positions
    return None, positions


def validate_case(case):
    keys = {'schema_version', 'dataset_version', 'case_id', 'category', 'source', 'source_sha256',
            'dictionary', 'gold', 'forbidden', 'notes'}
    if (not isinstance(case, dict) or set(case) != keys or type(case['schema_version']) is not int or case['schema_version'] != 1
            or type(case['dataset_version']) is not int or case['dataset_version'] != DATASET_VERSION
            or not isinstance(case['case_id'], str) or not case['case_id'].replace('_', '').isalnum()
            or not isinstance(case['source'], str) or not 1 <= len(case['source']) <= 128000
            or case['source_sha256'] != _sha(case['source'])
            or not isinstance(case['category'], str) or not 1 <= len(case['category']) <= 40
            or not isinstance(case['notes'], str) or len(case['notes']) > 5000): raise ValueError('평가 자료 신원·원문 오류')
    dictionary = validate_dictionary(case['dictionary'], case['source'])
    if dictionary != case['dictionary']: raise ValueError('사전은 정규화된 형태여야 합니다.')
    if not isinstance(case['gold'], list) or len(case['gold']) > 1280 or not isinstance(case['forbidden'], list): raise ValueError('정답 배열 오류')
    ids: set[str] = set()
    variants: dict[tuple, str] = {}
    slices = list(chunks(case['source']))
    for gold in case['gold']:
        if (not isinstance(gold, dict) or set(gold) != {'id', 'variants', 'evidence', 'sample_quote'}
                or not isinstance(gold['id'], str) or not gold['id'] or gold['id'] in ids
                or not isinstance(gold['variants'], list) or not 1 <= len(gold['variants']) <= 20
                or not isinstance(gold['evidence'], list) or not gold['evidence']): raise ValueError('정답 구조 오류')
        ids.add(gold['id']); exact = set()
        for e in gold['evidence']:
            if (not isinstance(e, dict) or set(e) != {'start', 'end', 'text'} or type(e['start']) is not int or type(e['end']) is not int
                    or not 0 <= e['start'] < e['end'] <= len(case['source']) or case['source'][e['start']:e['end']] != e['text']
                    or not any(o <= e['start'] < e['end'] <= o+len(t) for o, t in slices)):
                raise ValueError('정답 인용 위치 오류')
        for variant in gold['variants']:
            triple = _triple(variant); key = _canonical(case, triple)
            if triple in exact or (key in variants and variants[key] != gold['id']): raise ValueError('모호하거나 중복된 정답 표현')
            exact.add(triple); variants[key] = gold['id']
            dictionary_fact(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=gold['evidence'][0]['text']), case['source'], dictionary)
        if not any(_citation(case, gold['sample_quote'], i, gold)[0] is None for i in range(1, len(slices)+1)):
            raise ValueError('모범 인용 오류')
    forbidden = set()
    for triple in case['forbidden']:
        key = _canonical(case, _triple(triple))
        if key in variants or key in forbidden: raise ValueError('금지 관계와 정답/다른 금지 관계 중복')
        forbidden.add(key)
    return case


def load_manifest(root=None):
    root = Path(root) if root is not None else DATA_ROOT / 'v1'
    manifest = _read(root / 'manifest.json')
    if (not isinstance(manifest, dict) or set(manifest) != {'dataset_version', 'chunk_size', 'extraction_contract', 'human_review', 'cases'}
            or type(manifest['dataset_version']) is not int or manifest['dataset_version'] != DATASET_VERSION
            or type(manifest['chunk_size']) is not int or manifest['chunk_size'] != 8000
            or type(manifest['extraction_contract']) is not int or manifest['extraction_contract'] != 1
            or not isinstance(manifest['cases'], list) or not 1 <= len(manifest['cases']) <= 32
            or not isinstance(manifest['human_review'], dict) or set(manifest['human_review']) != {'status', 'evidence'}
            or manifest['human_review'].get('status') not in ('pending', 'approved')):
        raise ValueError('평가 manifest 오류')
    review = manifest['human_review']
    if ((review['status'] == 'pending' and review['evidence'] is not None)
            or (review['status'] == 'approved' and (not isinstance(review['evidence'], str) or not review['evidence'].strip() or len(review['evidence']) > 2000))):
        raise ValueError('사람 검토 상태·근거 오류')
    seen = set()
    for row in manifest['cases']:
        if (not isinstance(row, dict) or set(row) != {'id', 'file', 'sha256'} or not isinstance(row['id'], str)
                or row['id'] in seen or not isinstance(row['file'], str) or Path(row['file']).name != row['file']
                or not row['file'].endswith('.json') or not isinstance(row['sha256'], str)
                or len(row['sha256']) != 64 or any(c not in '0123456789abcdef' for c in row['sha256'])): raise ValueError('평가 자료 목록 오류')
        seen.add(row['id'])
    return manifest


def load_case(case_id, root=None):
    root = Path(root) if root is not None else DATA_ROOT / 'v1'
    manifest = load_manifest(root)
    entry = next((r for r in manifest['cases'] if r['id'] == case_id), None)
    if entry is None: raise ValueError('평가 자료 ID를 찾을 수 없습니다.')
    case = _read(root / entry['file'])
    raw = json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    if _sha(raw) != entry['sha256'] or case.get('case_id') != case_id: raise ValueError('평가 자료 hash 오류')
    return validate_case(case)


def metric(numerator, denominator):
    return {'value': numerator/denominator if denominator else None, 'numerator': numerator,
            'denominator': denominator, 'reason': None if denominator else 'not_applicable'}


def evaluate_case(case, chunk_outputs):
    validate_case(case)
    if not isinstance(chunk_outputs, list) or len(chunk_outputs) > 16: raise ValueError('청크 결과 배열 오류')
    golds = {g['id']: g for g in case['gold']}
    variants = {_canonical(case, _triple(v)): g['id'] for g in case['gold'] for v in g['variants']}
    forbidden = {_canonical(case, _triple(v)) for v in case['forbidden']}
    slices, seen_chunks = list(chunks(case['source'])), set()
    units: dict[tuple, dict] = {}
    invalid, citation_errors, duplicates, ambiguities = [], [], [], []
    for chunk in chunk_outputs:
        if (not isinstance(chunk, dict) or set(chunk) != {'index', 'output'} or type(chunk['index']) is not int
                or not 1 <= chunk['index'] <= len(slices) or chunk['index'] in seen_chunks
                or not isinstance(chunk['output'], dict) or not isinstance(chunk['output'].get('facts'), list)
                or len(chunk['output']['facts']) > 80): raise ValueError('청크 결과 구조·번호 오류')
        index = chunk['index']; seen_chunks.add(index)
        for row_index, row in enumerate(chunk['output']['facts'], 1):
            location = {'chunk': index, 'row': row_index}
            if (not isinstance(row, dict) or set(row) != {'subject', 'predicate', 'object', 'quote'}
                    or not isinstance(row['quote'], str)
                    or any(not isinstance(row[k], str) or not row[k].strip() or len(row[k]) > 200 for k in ('subject', 'predicate', 'object'))):
                invalid.append(location | {'reason': 'invalid_fact_shape'}); continue
            triple = _canonical(case, tuple(row[k].strip() for k in ('subject', 'predicate', 'object')))
            gold_id = variants.get(triple); unit = ('gold', gold_id) if gold_id is not None else ('triple', triple)
            if unit in units: duplicates.append(location | {'gold_id': gold_id, 'triple': list(triple)})
            else: units[unit] = {'gold_id': gold_id, 'triple': list(triple), 'grounded': False, 'forbidden': triple in forbidden, **location}
            reason, positions = _citation(case, row.get('quote'), index, golds.get(gold_id))
            if reason is not None: citation_errors.append(location | {'gold_id': gold_id, 'reason': reason})
            if len(positions) > 1: ambiguities.append(location | {'occurrences': [list(p) for p in positions], 'reason': 'literal_quote_has_multiple_occurrences'})
            if gold_id is not None and reason is None: units[unit]['grounded'] = True
    matched = sorted(u['gold_id'] for u in units.values() if u['gold_id'] is not None)
    grounded = sorted(u['gold_id'] for u in units.values() if u['gold_id'] is not None and u['grounded'])
    extras = sorted((u for u in units.values() if u['gold_id'] is None), key=lambda u: tuple(u['triple']))
    missing = sorted(set(golds)-set(matched)); grounded_missing = sorted(set(golds)-set(grounded))
    denominator = len(units)+len(invalid)
    return {'schema_version': 1, 'evaluator_version': EVALUATOR_VERSION, 'dataset_version': DATASET_VERSION,
            'case_id': case['case_id'], 'source_sha256': case['source_sha256'],
            'case_sha256': _sha(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(',', ':'))),
            'gold_count': len(golds), 'prediction_count': denominator, 'raw_prediction_count': sum(len(c['output']['facts']) for c in chunk_outputs),
            'true_positive_count': len(matched), 'false_positive_count': len(extras)+len(invalid), 'false_negative_count': len(missing),
            'grounded_true_positive_count': len(grounded),
            'precision': metric(len(matched), denominator), 'recall': metric(len(matched), len(golds)),
            'grounded_precision': metric(len(grounded), denominator), 'grounded_recall': metric(len(grounded), len(golds)),
            'citation_accuracy': metric(len(grounded), len(matched)),
            'true_positives': matched, 'false_negatives': missing, 'false_positives': extras,
            'grounded_missing': grounded_missing, 'invalid_facts': invalid, 'citation_errors': citation_errors,
            'duplicates': duplicates, 'quote_ambiguities': ambiguities,
            'no_relation_case_pass': denominator == 0 if not golds else None,
            'passed': not (missing or extras or invalid or citation_errors or duplicates)}


def ideal_outputs(case, *, contract_version=1):
    """Deterministic synthetic oracle for tests/runners; not an evaluator repair."""
    from .ontology import extraction_quote_issues
    from .service import validate_page
    result = []
    for index, (_offset, text) in enumerate(chunks(case['source']), 1):
        facts = []
        for gold in case['gold']:
            if _citation(case, gold['sample_quote'], index, gold)[0] is None:
                triple = _canonical(case, _triple(gold['variants'][0]))
                facts.append(dict(zip(('subject', 'predicate', 'object'), triple, strict=True), quote=gold['sample_quote']))
        first_line = text.splitlines()[0] or text[:80]
        output: dict = {'title': 'Synthetic extraction fixture', 'claims': [{'heading': 'Fixture', 'text': first_line, 'quote': first_line}],
                  'facts': facts, 'coverage': [{'predicate': r['id'], 'fact_indices': [i for i, f in enumerate(facts) if f['predicate'] == r['id']],
                                               'claim_indices': [], 'status': 'extracted' if any(f['predicate'] == r['id'] for f in facts) else 'no_evidence',
                                               'reason': 'Synthetic fixture annotation'} for r in case['dictionary']['relations']], 'dictionary_gaps': []}
        if contract_version == 2:
            # Synthetic v2 oracle only: the entire source chunk is explicit unique context.
            # Never apply this to model responses or historical v1 outputs.
            for row in output['claims']+output['facts']:
                quote = row['quote'].strip()
                first = text.find(quote)
                if first >= 0 and text.find(quote, first+1) >= 0: row['quote'] = text.strip()
        if extraction_quote_issues(output, text, case['dictionary'], require_coverage=True, contract_version=contract_version): raise ValueError('모범 응답 인용 오류')
        validate_page(output, text)
        for fact in facts: dictionary_fact(fact, text, case['dictionary'])
        result.append({'index': index, 'output': output})
    return result


def evaluate_many(results):
    results = list(results)
    if len(results) > 1024: raise ValueError('평가 집계 개수 오류')
    for row in results:
        if (not isinstance(row, dict) or row.get('evaluator_version') != EVALUATOR_VERSION
                or type(row.get('evaluator_version')) is not int or type(row.get('dataset_version')) is not int
                or row.get('dataset_version') != DATASET_VERSION
                or any(type(row.get(k)) is not int or not 0 <= row[k] <= 1280 for k in
                       ('true_positive_count', 'false_positive_count', 'false_negative_count', 'gold_count', 'prediction_count', 'grounded_true_positive_count'))
                or row['true_positive_count'] + row['false_negative_count'] != row['gold_count']
                or row['true_positive_count'] + row['false_positive_count'] != row['prediction_count']
                or row['grounded_true_positive_count'] > row['true_positive_count']):
            raise ValueError('평가 집계 입력 오류')
    tp, fp, fn = (sum(r[key] for r in results) for key in ('true_positive_count', 'false_positive_count', 'false_negative_count'))
    grounded = sum(r['grounded_true_positive_count'] for r in results)
    return {'schema_version': 1, 'evaluator_version': EVALUATOR_VERSION, 'cases': results,
            'micro_grounded_precision': metric(grounded, tp+fp), 'micro_grounded_recall': metric(grounded, tp+fn),
            'micro_citation_accuracy': metric(grounded, tp),
            'micro_precision': metric(tp, tp+fp), 'micro_recall': metric(tp, tp+fn)}
