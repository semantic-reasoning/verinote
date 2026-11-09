"""Bounded model self-review. Accounting consistency is not semantic completeness."""
import copy
import json
import re
from pathlib import Path

from .ontology import MAX_FACTS, VocabularyError, dictionary_terms
from .usage import atomic_json, safe_file

MAX_REPORT_BYTES = 2 * 1024 * 1024
STATUSES = {'extracted', 'no_evidence', 'not_representable', 'limit_reached'}
GAP_CODES = {'missing_entity', 'missing_relation', 'type_mismatch', 'qualifier_not_representable'}
MAX_REPAIR_BYTES = 64 * 1024
MAX_EXCLUDED_QUOTE = 300
MAX_EXCLUDED_TEXT = 300
EXCLUSION_CODES = {'unknown_entity', 'type_mismatch', 'unverifiable_quote'}


def integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def indices(value, size):
    if (not isinstance(value, list) or len(value) > size
            or any(not integer(i, 0, size - 1) for i in value)
            or len(set(value)) != len(value)):
        raise ValueError('추출 점검: 중복 없는 범위 내 정수 인덱스가 필요합니다.')
    return value


def validate_exclusions(rows, kept, predicates):
    if not isinstance(rows, list) or kept + len(rows) > MAX_FACTS:
        raise ValueError('추출 점검 excluded_facts 형식 오류')
    for row in rows:
        located = isinstance(row, dict) and row.get('code') == 'unverifiable_quote'
        if (not isinstance(row, dict) or set(row) != {'code', 'subject', 'predicate', 'object', 'quote'} | ({'path'} if located else set())
                or any(not isinstance(field, str) for field in row.values())
                or row['code'] not in EXCLUSION_CODES or row['predicate'] not in predicates
                or (located and not re.fullmatch(r'facts\.[0-9]{1,3}\.quote', row['path']))
                or len(row['subject']) > 200 or len(row['object']) > 200 or len(row['quote']) > MAX_EXCLUDED_QUOTE):
            raise ValueError('추출 점검 excluded_facts 필드 오류')
    return rows


def validate_excluded_claims(rows, kept, predicates):
    if not isinstance(rows, list) or kept + len(rows) > 12:
        raise ValueError('추출 점검 excluded_claims 형식 오류')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'path', 'heading', 'text', 'quote', 'predicates', 'gaps'}
                or any(not isinstance(row[key], str) for key in ('path', 'heading', 'text', 'quote'))
                or not re.fullmatch(r'claims\.[0-9]{1,3}\.quote', row['path'])
                or len(row['heading']) > 200 or len(row['text']) > MAX_EXCLUDED_TEXT or len(row['quote']) > MAX_EXCLUDED_QUOTE
                or not isinstance(row['predicates'], list)
                or any(not isinstance(predicate, str) or predicate not in predicates for predicate in row['predicates'])
                or not isinstance(row['gaps'], list) or len(row['gaps']) > 12
                or any(not isinstance(gap, dict) or set(gap) != {'code', 'detail'} or not isinstance(gap['code'], str)
                       or gap['code'] not in GAP_CODES or not isinstance(gap['detail'], str) or len(gap['detail']) > 160 for gap in row['gaps'])):
            raise ValueError('추출 점검 excluded_claims 필드 오류')
    return rows


def resolve_quote_repairs(value, source, issues, response, contract_version):
    """Apply a repair response row by row: `(result, changes, refused)`.

    A row whose reply passes the contract's quote check is replaced; any other string excludes that row. Only rows
    the model was asked to repair can be excluded, and a malformed response raises, as
    apply_quote_repairs does. `value` must already have passed shape and accounting checks.
    """
    from .service import quote_text
    expected = {issue['path'] for issue in issues}
    if not isinstance(response, dict) or set(response) != {'repairs'}:
        raise ValueError('인용 보정 응답에는 repairs 배열만 필요합니다.')
    rows = response['repairs']
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError('문제가 있는 인용을 빠짐없이 한 번씩 보정해야 합니다.')
    replies = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'path', 'quote'} or not isinstance(row['quote'], str):
            raise ValueError('인용 보정 항목에는 문자열 path와 quote만 필요합니다.')
        path = row['path']
        if not isinstance(path, str) or path not in expected or path in replies:
            raise ValueError('인용 보정 대상이 누락·중복되거나 요청과 다릅니다.')
        replies[path] = row['quote']
    result = copy.deepcopy(value)
    changes, refused = [], []
    gone: dict[str, dict[int, str]] = {'claims': {}, 'facts': {}}
    for path, reply in replies.items():
        kind, index, _ = path.split('.')
        item = result[kind][int(index)]
        try:
            quote = quote_text(reply)
            first = source.find(quote)
        except ValueError:
            first = -1
        if first < 0 or (contract_version in (2, 3) and source.find(quote, first + 1) >= 0):
            gone[kind][int(index)] = path
            refused.append({'path': path, 'before': item['quote'][:MAX_EXCLUDED_QUOTE], 'reply': reply[:MAX_EXCLUDED_QUOTE]})
        else:
            changes.append({'path': path, 'before': item['quote'], 'after': quote})
            item['quote'] = quote
    _drop_facts(result, gone['facts'])
    _drop_claims(result, gone['claims'])
    return result, changes, refused


# A model quote may differ from the source only in the width of a run of these. Not regex \s or str.split(), which also
# cross \f, \v, \x1c-\x1f, \x85 and \u2028-\u2029. A run is never removed: spacing can change meaning in Korean.
QUOTE_SPACE = ' \t\r\n'


def _span_end(source, start, tokens):
    """`(end, crossed)` of the span whose words are `tokens`, starting at `start`, or `(-1, False)`.

    `crossed` says a source run on the way holds two or more line breaks (CRLF is one).
    """
    at = start + len(tokens[0])
    crossed = False
    for token in tokens[1:]:
        gap, breaks = at, 0
        while at < len(source) and source[at] in QUOTE_SPACE:
            breaks += source[at] == '\n' or (source[at] == '\r' and source[at + 1:at + 2] != '\n')
            at += 1
        if at == gap or not source.startswith(token, at):
            return -1, False
        crossed = crossed or breaks > 1
        at += len(token)
    return at, crossed


def whitespace_variant_span(quote, source):
    """The source passage equal to `quote` except for run widths, or None.

    None unless exactly one start position matches, nested ones included, and that match crosses no source run with two or more
    line breaks. A crossing match still counts as a position: the response may have meant it.
    """
    from .service import quote_text
    tokens = re.split(f'[{QUOTE_SPACE}]+', quote_text(quote))
    found, start = None, source.find(tokens[0])
    while start >= 0:
        end, crossed = _span_end(source, start, tokens)
        if end >= 0:
            if crossed or found is not None:
                return None
            found = source[start:end]
        start = source.find(tokens[0], start + 1)
    return found


def locate_whitespace_variants(value, source, issues):
    """Replace each quote that has one whitespace-variant passage: `(result, changes, issues left)`. No other field moves.

    An exact but ambiguous quote stays: each of its occurrences is itself a match, and two matches refuse.
    The stored passage is exact and unique: each occurrence of it is a match of the same words, and only one match was found.
    """
    result = copy.deepcopy(value)
    changes, left = [], []
    for issue in issues:
        kind, index, _ = issue['path'].split('.')
        row = result[kind][int(index)]
        span = whitespace_variant_span(row['quote'], source)
        if span is None:
            left.append(issue)
        else:
            changes.append({'path': issue['path'], 'before': row['quote'], 'after': span})
            row['quote'] = span
    return result, changes, left


def write_located_quotes(folder, changes):
    try:
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / 'quote-located.json', changes)
    except (OSError, ValueError) as exc:
        raise ValueError('인용 위치 확정 기록 저장 실패: 이번 청크는 확정하지 않았습니다.') from exc


def located_quote_notice(count, places):
    """Run-detail clause for chunks that passed validation, numbered like the repair clause. Reused chunks are not recounted."""
    shown = '; '.join(places[:5]) + (f' 외 {len(places) - 5}개 청크' if len(places) > 5 else '')
    return (f'이번 실행 검증을 마친 청크에서 공백만 다른 인용 {count}개를 모델 호출 없이 원문 구간으로 확정 ({shown} · 번호는 제외 뒤 기준). '
            '확정 구간에는 모델이 인용한 내용만 있으므로 그 항목을 뒷받침하는지 확인하세요')


def saved_quote_paths(changes, refused):
    """Where the replaced quotes sit once the rows this repair refused are gone."""
    gone = [row['path'].split('.') for row in refused]
    paths = []
    for change in changes:
        kind, index, field = change['path'].split('.')
        paths.append(f'{kind}.{int(index) - sum(k == kind and int(i) < int(index) for k, i, _ in gone)}.{field}')
    return paths


def _drop_facts(result, gone):
    if not gone:
        return
    records = result.setdefault('excluded_facts', [])
    earlier = {record['predicate'] for record in records}
    kept: list[dict] = []
    renumber = {}
    for index, fact in enumerate(result['facts']):
        if index in gone:
            records.append({'code': 'unverifiable_quote', 'subject': fact['subject'], 'predicate': fact['predicate'], 'object': fact['object'],
                            'quote': fact['quote'][:MAX_EXCLUDED_QUOTE], 'path': gone[index]})
        else:
            renumber[index] = len(kept)
            kept.append(fact)
    result['facts'] = kept
    for row in result['coverage']:
        before = row['fact_indices']
        row['fact_indices'] = [renumber[i] for i in before if i in renumber]
        if before and not row['fact_indices']:  # Accounting put every fact of this predicate in this row.
            row['status'] = 'not_representable'
            row['reason'] = ('응답 관계가 승인 사전 불일치 또는 인용 미확인으로 모두 저장에서 제외됨 (모델 판단이 아닌 로컬 처리)' if row['predicate'] in earlier
                             else '응답 관계의 인용을 원문에서 확인하지 못해 저장에서 제외됨 (모델 판단이 아닌 로컬 처리)')


def _drop_claims(result, gone):
    if not gone:
        return
    kept: list[dict] = []
    renumber, records = {}, {}
    for index, claim in enumerate(result['claims']):
        if index in gone:
            records[index] = {'path': gone[index], 'heading': claim['heading'], 'text': claim['text'][:MAX_EXCLUDED_TEXT],
                              'quote': claim['quote'][:MAX_EXCLUDED_QUOTE], 'predicates': [], 'gaps': []}
        else:
            renumber[index] = len(kept)
            kept.append(claim)
    result['claims'] = kept
    without_facts = {record['predicate'] for record in result.get('excluded_facts', ())}
    for row in result['coverage']:
        before = row['claim_indices']
        row['claim_indices'] = [renumber[i] for i in before if i in renumber]
        if row['status'] == 'not_representable' and before and not row['claim_indices']:
            for i in before:
                records[i]['predicates'].append(row['predicate'])
            if row['predicate'] not in without_facts:  # Otherwise the row already gives its local reason.
                row['reason'] = '근거 주장의 인용을 원문에서 확인하지 못해 주장을 제외함 (모델 판단이 아닌 로컬 처리)'
    gaps = []
    for gap in result['dictionary_gaps']:
        after = [renumber[i] for i in gap['claim_indices'] if i in renumber]
        if after:
            gaps.append(gap | {'claim_indices': after})
        else:
            records[gap['claim_indices'][0]]['gaps'].append({'code': gap['code'], 'detail': gap['detail']})
    result['dictionary_gaps'] = gaps
    result['excluded_claims'] = [records[i] for i in sorted(records)]


def exclude_off_dictionary_facts(value, dictionary):
    """Fresh responses only: remove facts whose entity spelling or types do not match the approved dictionary, keep accounting true.

    `excluded_facts` and `excluded_claims` are local provenance, so inbound keys are discarded before
    filtering. A response with neither key and no exclusion comes back as the same object. Coverage
    indices in the result number the kept facts.
    """
    if not isinstance(value, dict):
        return value, []
    if 'excluded_facts' in value or 'excluded_claims' in value:
        value = {key: field for key, field in value.items() if key not in ('excluded_facts', 'excluded_claims')}
    facts = value.get('facts')
    if not isinstance(facts, list) or len(facts) > MAX_FACTS:
        return value, []  # Ordinary preflight reports the real shape or count.
    kept: list[dict] = []
    excluded, renumber, gone = [], {}, {}
    for index, fact in enumerate(facts):
        try:
            dictionary_terms(fact, dictionary)
        except VocabularyError as exc:
            quote = fact.get('quote')
            gone[index] = fact['predicate'].strip()
            excluded.append({'code': exc.code, 'subject': fact['subject'], 'predicate': gone[index],
                             'object': fact['object'], 'quote': quote[:MAX_EXCLUDED_QUOTE] if isinstance(quote, str) else ''})
        except ValueError:
            return value, []  # Malformed, or an unapproved predicate: ordinary preflight rejects it.
        else:
            renumber[index] = len(kept)
            kept.append(fact)
    if not excluded:
        return value, []
    result = copy.deepcopy(value)
    result['facts'] = copy.deepcopy(kept)
    result['excluded_facts'] = excluded
    remaining = {fact['predicate'] for fact in kept}
    rows = result.get('coverage')
    for row in rows if isinstance(rows, list) else []:
        before = row.get('fact_indices') if isinstance(row, dict) else None
        if not isinstance(before, list) or any(type(i) is not int for i in before) or len(set(before)) != len(before):
            continue  # validate_accounting rejects this row unchanged.
        predicate, after = row.get('predicate'), []
        for i in before:
            if i not in gone:
                after.append(renumber.get(i, i))  # An out-of-range index stays out of range.
            elif gone[i] != predicate:
                after.append(len(kept) + i)  # A contradiction before removal: validate_accounting rejects it.
        row['fact_indices'] = after
        if not after and predicate in gone.values() and predicate not in remaining:
            row['status'] = 'not_representable'
            row['reason'] = '응답 관계가 승인 사전의 표기·타입과 일치하지 않아 저장에서 제외됨 (모델 판단이 아닌 로컬 처리)'
    return result, excluded


def dictionary_index(dictionary):
    """The lookups both readings use, built once per report read."""
    return {'entities': {row['id']: row for row in dictionary['entities']},
            'aliases': {row['alias']: row['entity'] for row in dictionary['aliases']},
            'relations': {row['id']: row for row in dictionary['relations']},
            'names': _names(dictionary['entities'])}


def _names(entities):
    """Entity IDs by exact name, in dictionary order. For reporting a name match only; nothing is connected by it."""
    names: dict[str, list[str]] = {}
    for row in entities:
        names.setdefault(row.get('name'), []).append(row['id'])
    return names


def _allowed_types(index, predicate, field):
    allowed = index['relations'][predicate][field + '_types']
    if not isinstance(allowed, list) or any(not isinstance(t, str) for t in allowed):
        raise ValueError('allowed types must be a list of strings')
    return allowed


def exclusion_allowed(rows, dictionary, index=None):
    """The allowed types of each predicate the excluded rows use, sent once per chunk instead of inside every reading."""
    index = index or dictionary_index(dictionary)
    return {row['predicate']: {field: _allowed_types(index, row['predicate'], field) for field in ('subject', 'object')}
            for row in rows if row['code'] != 'unverifiable_quote'}


def exclusion_terms(rows, dictionary, index=None):
    """One reading per excluded fact against the dictionary that excluded it, None for a quote exclusion.

    `dictionary_terms` stops at its first failure. This reads both fields, so a row excluded for one unknown spelling still
    shows a type the other field would fail on. Spellings match exactly; only surrounding whitespace is removed, as there.
    `name_of` lists the first 5 entity IDs whose name equals an unknown spelling exactly and `name_of_total` counts all of them;
    it only reports, nothing is connected by it.
    """
    index = index or dictionary_index(dictionary)
    entities, aliases = index['entities'], index['aliases']

    def term(row, field):
        spelling = row[field].strip()
        entity = aliases.get(spelling, spelling)
        known = entity in entities
        allowed = _allowed_types(index, row['predicate'], field)
        named = index['names'].get(spelling, []) if not known else []
        return {'field': field, 'spelling': spelling, 'known': known, 'entity': entity if known else None,
                'type': entities[entity]['type'] if known else None, 'name_of': named[:5], 'name_of_total': len(named),
                'type_fits': entities[entity]['type'] in allowed if known else None}
    return [None if row['code'] == 'unverifiable_quote' else [term(row, 'subject'), term(row, 'object')] for row in rows]


def _row_count_detail(rows, predicates):
    """Variable content is counts and approved IDs, never a model-written string; says the response was not kept as a candidate."""
    tail = ' 이 응답은 후보로 저장하지 않았습니다. 다시 추출하면 재사용할 복구 기록이 없는 청크를 새 모델 호출(비용이 들 수 있음)로 요청하며 같은 결과가 나올 수 있습니다.'
    if not isinstance(rows, list):
        return f' (승인 관계 {len(predicates)}개, coverage 배열 없음)' + tail
    named = [row['predicate'] for row in rows if isinstance(row, dict) and isinstance(row.get('predicate'), str)]
    missing = sorted(predicates - set(named))
    unapproved = len(rows) - sum(name in predicates for name in named)
    duplicated = sum(named.count(p) > 1 for p in predicates)
    parts = [f'승인 관계 {len(predicates)}개', f'응답 행 {len(rows)}개']
    if unapproved:
        parts.append(f'승인되지 않은 행 {unapproved}개')
    if duplicated:
        parts.append(f'중복 관계 {duplicated}개')
    if missing:
        parts.append('행이 없는 관계: ' + ', '.join(missing[:5]) + (f' 외 {len(missing) - 5}개' if len(missing) > 5 else ''))
    return ' (' + ', '.join(parts) + ')' + tail


def validate_accounting(value, dictionary):
    """Called after ordinary shape validation, before any quote-repair request."""
    from .service import text
    predicates = {r['id'] for r in dictionary['relations']}
    rows, gaps = value.get('coverage'), value.get('dictionary_gaps')
    if not isinstance(rows, list) or len(rows) != len(predicates):
        raise ValueError('추출 점검 coverage: 승인 관계마다 정확히 한 행이 필요합니다.' + _row_count_detail(rows, predicates))
    if not isinstance(gaps, list) or len(gaps) > 12:
        raise ValueError('추출 점검 dictionary_gaps: 최대 12개 배열이 필요합니다.')
    seen, covered = set(), set()
    facts, claims = value['facts'], value['claims']
    excluded = validate_exclusions(value.get('excluded_facts', []), len(facts), predicates)
    refused = validate_excluded_claims(value.get('excluded_claims', []), len(claims), predicates)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'predicate', 'fact_indices', 'claim_indices', 'status', 'reason'}:
            raise ValueError('추출 점검 coverage 필드 오류')
        predicate, status = row['predicate'], row['status']
        if not isinstance(predicate, str) or predicate not in predicates or predicate in seen:
            raise ValueError('추출 점검: 승인 관계가 누락·중복되거나 알 수 없는 관계입니다.' + _row_count_detail(rows, predicates))
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError('추출 점검 status 오류')
        text(row['reason'], '추출 점검 reason', 120)
        fi = indices(row['fact_indices'], len(facts))
        ci = indices(row['claim_indices'], len(claims))
        if bool(fi) != (status == 'extracted'):
            raise ValueError('추출 점검: extracted 상태와 fact_indices가 일치하지 않습니다.')
        if status == 'not_representable' and not ci and not any(e['predicate'] == predicate for e in excluded) and not any(predicate in c['predicates'] for c in refused):
            raise ValueError('추출 점검: 표현 제약의 근거 claim_indices가 필요합니다.')
        if status == 'limit_reached' and len(facts) + len(excluded) != MAX_FACTS:
            raise ValueError('추출 점검: 사실 한도에 도달하지 않았습니다.')
        if any(facts[i]['predicate'] != predicate or i in covered for i in fi):
            raise ValueError('추출 점검: fact_indices의 관계가 다르거나 중복됩니다.')
        covered.update(fi)
        seen.add(predicate)
    if covered != set(range(len(facts))):
        raise ValueError('추출 점검: 모든 사실을 같은 관계의 coverage에 포함해야 합니다.')
    for gap in gaps:
        if not isinstance(gap, dict) or set(gap) != {'code', 'detail', 'claim_indices'}:
            raise ValueError('추출 점검 dictionary_gaps 필드 오류')
        if not isinstance(gap['code'], str) or gap['code'] not in GAP_CODES:
            raise ValueError('추출 점검 dictionary_gaps code 오류')
        text(gap['detail'], '추출 점검 detail', 160)
        if not indices(gap['claim_indices'], len(claims)):
            raise ValueError('추출 점검: 사전 제약의 근거 claim_indices가 필요합니다.')
    return copy.deepcopy({'coverage': rows, 'dictionary_gaps': gaps})


def repair_missing_indices(value, dictionary):
    """Normalize only fresh, shape-preflighted output; persisted data stays strict."""
    result = copy.deepcopy(value)
    changes = []
    rows = result.get('coverage')
    predicates = {r['id'] for r in dictionary['relations']}
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if (not isinstance(row, dict) or row.get('status') != 'extracted'
                    or not isinstance(row.get('predicate'), str) or row['predicate'] not in predicates):
                continue
            before = indices(row.get('fact_indices'), len(result['facts']))
            expected = [i for i, fact in enumerate(result['facts']) if fact['predicate'] == row['predicate']]
            if set(before) < set(expected):
                after = before + [i for i in expected if i not in before]
                row['fact_indices'] = after
                changes.append({'row': index, 'predicate': row['predicate'], 'before': before, 'after': after})
    # No partial repair may hide another malformed or contradictory field.
    validate_accounting(result, dictionary)
    return result, changes


def write_repair_audit(root, run, doc, chunk, offset, before, after, changes):
    """Mandatory provenance for a mechanical repair, not acceptance of its facts."""
    from .store import digest
    try:
        if not 1 <= len(changes) <= MAX_FACTS:
            raise ValueError('coverage repair count')
        folder = Path(root)
        for part in (None, 'runs', run['id'], str(chunk)):
            if part is not None:
                folder = folder / part
            if folder.is_symlink():
                raise ValueError('coverage repair symlink')
        path = folder / 'coverage-repair.json'
        if path.is_symlink() or path.with_suffix('.json.tmp').is_symlink():
            raise ValueError('coverage repair symlink')
        audit = {'schema_version': 1, 'identity': identity(run), 'chunk': chunk, 'offset': offset,
                 'source_sha256': doc['sha256'], 'text_sha256': digest(doc['text'].encode()),
                 'before_sha256': digest(before), 'after_sha256': digest(after),
                 'scope': 'mechanical_indices_only_not_chunk_acceptance', 'changes': changes}
        raw = safe_file(folder, 'run.json', 16384)
        try:
            meta = json.loads(raw) if raw else None
        except (ValueError, TypeError, RecursionError):
            meta = None
        if isinstance(meta, dict):
            for key in ('cli_call_id', 'provider_call_id'):
                call_id = meta.get(key)
                if isinstance(call_id, str) and 1 <= len(call_id) <= 200:
                    audit[key] = call_id
        if len(json.dumps(audit, ensure_ascii=False, indent=2).encode()) > MAX_REPAIR_BYTES:
            raise ValueError('coverage repair size')
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(path, audit)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError('추출 점검 인덱스 보정 기록 저장 실패: 이번 청크는 확정하지 않았습니다.') from exc


def identity(run):
    pin = run['ontology']
    return {'run_id': run['id'], 'document_id': run['document_id'], 'version': run['version'],
            'dictionary_id': pin['id'], 'revision': pin['revision'], 'sha256': pin['sha256'],
            'extraction_contract': pin.get('extraction_contract', 0)}


def new_report(run, source_length):
    return {'schema_version': 1, 'identity': identity(run), 'source_length': source_length, 'chunks': []}


def chunk_report(output, offset, length):
    report = {'offset': offset, 'length': length, 'claim_count': len(output['claims']),
              'fact_predicates': [f['predicate'] for f in output['facts']],
              'coverage': copy.deepcopy(output['coverage']), 'dictionary_gaps': copy.deepcopy(output['dictionary_gaps'])}
    if output.get('excluded_facts'):
        report['excluded_facts'] = copy.deepcopy(output['excluded_facts'])
    if output.get('excluded_claims'):
        report['excluded_claims'] = copy.deepcopy(output['excluded_claims'])
    return report


def write_report(root, report):
    """Optional telemetry; never let its failure roll back candidate publication."""
    try:
        if len(json.dumps(report, ensure_ascii=False, indent=2).encode()) > MAX_REPORT_BYTES:
            return False
        folder = Path(root)
        if folder.is_symlink():
            return False
        for part in ('runs', report['identity']['run_id']):
            folder = folder / part
            if folder.is_symlink():
                return False
        if any((folder / name).is_symlink() for name in ('extraction-coverage.json', 'extraction-coverage.json.tmp')):
            return False
        folder.mkdir(parents=True, exist_ok=True)
        atomic_json(folder / 'extraction-coverage.json', report)
        return True
    except (OSError, ValueError, TypeError):
        return False


def read_report(root, store, run_id):
    unavailable = {'available': False, 'warning': '관계별 추출 점검 기록을 사용할 수 없습니다. 누락이 없다는 뜻은 아닙니다.'}
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        return unavailable
    # Recheck DB ownership even when the caller holds a stale run object after deletion.
    with store.connect() as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            return unavailable
        run = store.unpack_run(row)
        source = db.execute('SELECT length(text) FROM versions WHERE document_id=? AND version=?',
                            (run['document_id'], run['version'])).fetchone()
        pages = db.execute("SELECT payload FROM items WHERE run_id=? AND kind='page'", (run_id,)).fetchall()
        saved = db.execute("SELECT count(*) FROM items WHERE run_id=? AND kind='fact'", (run_id,)).fetchone()[0]
    if (not source or run['stage'] != 'extract' or not run['ontology']
            or type(run['ontology'].get('extraction_contract')) is not int
            or run['ontology']['extraction_contract'] not in (1, 2, 3)):
        return unavailable
    raw = safe_file(root, f'runs/{run_id}/extraction-coverage.json', MAX_REPORT_BYTES)
    if raw is None:
        return unavailable
    try:
        report = json.loads(raw)
        if (not isinstance(report, dict) or set(report) != {'schema_version', 'identity', 'source_length', 'chunks'}
                or type(report['schema_version']) is not int or report['schema_version'] != 1
                or report['identity'] != identity(run)
                or not isinstance(report['identity'], dict)
                or any(type(report['identity'].get(k)) is not int for k in ('version', 'revision', 'extraction_contract'))
                or not integer(report['source_length'], 1, 128000) or report['source_length'] != source[0]
                or not isinstance(report['chunks'], list) or len(report['chunks']) > 16):
            return unavailable
        total_chunks = (source[0] + 7999) // 8000
        if len(report['chunks']) > total_chunks:
            return unavailable
        claims, emitted, predicates, gaps, capped, mismatched, unverifiable, refused_claims, refused_gaps = 0, 0, set(), 0, False, 0, 0, 0, 0
        spellings, index = 0, None
        for i, chunk in enumerate(report['chunks']):
            if (not isinstance(chunk, dict) or set(chunk) - {'excluded_facts', 'excluded_claims'} != {'offset', 'length', 'claim_count', 'fact_predicates', 'coverage', 'dictionary_gaps'}
                    or not integer(chunk['offset'], 0, 127999) or chunk['offset'] != i * 8000
                    or not integer(chunk['length'], 1, 8000) or chunk['length'] != min(8000, source[0] - i * 8000)
                    or not integer(chunk['claim_count'], 0 if chunk.get('excluded_claims') else 1, 12)
                    or not isinstance(chunk['fact_predicates'], list) or len(chunk['fact_predicates']) > MAX_FACTS
                    or any(not isinstance(p, str) for p in chunk['fact_predicates'])):
                return unavailable
            validate_accounting(chunk | {'facts': [{'predicate': p} for p in chunk['fact_predicates']],
                                         'claims': [None] * chunk['claim_count']}, run['ontology']['payload'])
            # Indices in reports are local; page claims concatenate without deduplication.
            chunk['claim_base'] = claims
            claims += chunk['claim_count']
            refused = len(chunk.get('excluded_facts', []))
            emitted += len(chunk['fact_predicates']) + refused
            located = sum(row['code'] == 'unverifiable_quote' for row in chunk.get('excluded_facts', []))
            spellings += sum(row['code'] == 'unknown_entity' for row in chunk.get('excluded_facts', []))
            if chunk.get('excluded_facts'):
                index = index or dictionary_index(run['ontology']['payload'])
                chunk['excluded_fact_terms'] = exclusion_terms(chunk['excluded_facts'], None, index)
                chunk['excluded_fact_allowed'] = exclusion_allowed(chunk['excluded_facts'], None, index)
            unverifiable += located
            mismatched += refused - located
            refused_claims += len(chunk.get('excluded_claims', []))
            refused_gaps += sum(len(row['gaps']) for row in chunk.get('excluded_claims', []))
            predicates.update(chunk['fact_predicates'])
            gaps += len(chunk['dictionary_gaps'])
            capped |= len(chunk['fact_predicates']) + refused == MAX_FACTS
        if saved > emitted or (run['status'] == 'success' and (len(report['chunks']) != total_chunks
                or len(pages) != 1 or len(json.loads(pages[0][0])['claims']) != claims)):
            return unavailable
        # After validation, so a pin that fails it stays unavailable instead of failing the read.
        if not isinstance(run['ontology']['id'], str):
            return unavailable
        with store.connect() as db:
            current_version = db.execute('SELECT current_version FROM documents WHERE id=?', (run['document_id'],)).fetchone()
            dictionary_item = db.execute('SELECT revision,status FROM items WHERE id=?', (run['ontology']['id'],)).fetchone()
        return {'available': True, 'status': run['status'], 'published': run['status'] == 'success',
                'total_chunks': total_chunks, 'validated_chunks': len(report['chunks']),
                'emitted_fact_count': emitted, 'excluded_fact_count': mismatched, 'unverifiable_fact_count': unverifiable,
                'excluded_spelling_count': spellings, 'excluded_type_count': mismatched - spellings,
                'dictionary_revision': run['ontology']['revision'],
                'current_dictionary_revision': dictionary_item[0] if dictionary_item else None,
                'current_dictionary_status': dictionary_item[1] if dictionary_item else None, 'run_id': run_id,
                'version_current': bool(current_version) and current_version[0] == run['version'],
                'claim_count': claims, 'excluded_claim_count': refused_claims, 'excluded_gap_count': refused_gaps,
                'saved_fact_count': saved if run['status'] == 'success' else 0,
                'extracted_predicates': len(predicates), 'approved_predicates': len(run['ontology']['payload']['relations']),
                'gap_count': gaps, 'capped': capped, 'chunks': report['chunks']}
    except (ValueError, TypeError, KeyError, RecursionError):
        return unavailable
