"""Evidence-backed draft revisions and bounded review history; never approvals."""
import copy
import functools
import json
import re
import sys
import unicodedata

from .documents import bounded_spans
from .ontology import (
    DICTIONARY_CHUNK_SIZE,
    DICTIONARY_TOTAL_LIMITS,
    LIMITS,
    _dictionary_definition,
    validate_dictionary,
)
from .usage import safe_file

POLICY = 1
MAX_REVISIONS = 40
MAX_DETAILS = 20
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_EVENTS = sum(DICTIONARY_TOTAL_LIMITS.values()) + 2 * sum(LIMITS.values()) + MAX_REVISIONS
FIELDS = {'entities': {'id', 'name', 'type'}, 'aliases': {'alias', 'entity'},
          'relations': {'id', 'description', 'subject_types', 'object_types'}}


# Hangul fillers render as blanks but are letters (Lo), so neither str.isspace nor the format category Cf covers them.
HANGUL_FILLERS = frozenset('\u3164\u115f\u1160\uffa0')


def blank(char):
    """Whether `char` counts as filling a gap when refusing or listing spellings that differ in spacing: whitespace (including U+0085),
    a zero-width or other format character (Cf), or a Hangul filler. Never used to find, suggest or link a spelling."""
    return char.isspace() or unicodedata.category(char) == 'Cf' or char in HANGUL_FILLERS


def compact(value):
    return ''.join(char for char in value if not blank(char))


LINE_BREAKS = set('\n\r\v\f\x1c\x1d\x1e\x85  ')


def _runs(value):
    """The characters of the stripped `value` that are not blank, and the run of blank characters between each consecutive pair."""
    chars: list[str] = []
    gaps: list[str] = []
    run = ''
    for char in value.strip():
        if blank(char):
            run += char
        else:
            if chars:
                gaps.append(run)
            chars.append(char)
            run = ''
    return chars, gaps


def spacing_kind(a, b):
    """'spacing' if `a` and `b` differ only in blank characters (`blank`) and some position has a whitespace character on one side and none on the other.

    'layout' if they differ only in blank characters and no position does. A position with a line break on either side is
    layout: a line break in extracted text erases whether a space was there. A position where both sides hold whitespace
    of a different width or kind, or differ only in zero-width characters, is layout too. None if they are equal or differ in
    any other character. For refusing or listing, never for linking.
    """
    if a == b:
        return None
    chars_a, gaps_a = _runs(a)
    chars_b, gaps_b = _runs(b)
    if chars_a != chars_b:
        return None
    for x, y in zip(gaps_a, gaps_b, strict=True):
        if not LINE_BREAKS & set(x + y) and any(c.isspace() for c in x) != any(c.isspace() for c in y):
            return 'spacing'
    return 'layout'


def spacing_name_rows(dictionary):
    """Entities (1-based position, ID, name) whose ID and name differ only in spacing, as `spacing_kind` defines it."""
    return [{'index': index, 'id': row['id'], 'name': row['name']} for index, row in enumerate(dictionary['entities'], 1)
            if spacing_kind(row['id'], row['name']) == 'spacing']


MAX_FACT_STEPS = 100_000
MAX_REQUEST_STEPS = 2_000_000
SCAN_UNIT = 512
LINE_BREAK_CODE = '\x01'
MISSING_CODE = '\x00'


class _OutOfBudget(Exception):
    pass


def new_request_budget():
    """A work budget shared by every fact of one document request (see `fact_spacing_warnings`)."""
    return {'steps': MAX_REQUEST_STEPS}


@functools.cache
def _blank_pattern():
    """The compiled class of every `blank` character (str.isspace is what `\\s` matches), for C-speed `compact`."""
    extra = ''.join(re.escape(chr(c)) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == 'Cf')
    return re.compile(f'[\\s{extra}{re.escape("".join(HANGUL_FILLERS))}]+')


def compact_fast(value):
    """`compact(value)` without a Python-level loop."""
    return _blank_pattern().sub('', value)


def quote_layout(quote):
    """The quote analysed once: its non-blank characters joined, where each stands, a code per gap after a character, and the code table.

    A gap's code is one character per distinct whitespace run; a gap with a line break has LINE_BREAK_CODE. Equal code strings mean
    equal gaps everywhere a line break does not hide the position, which is how `_search` skips occurrences at C speed.
    """
    chars, where, codes = [], [], []
    table: dict[str, str] = {}
    last = None
    for index, char in enumerate(quote):
        if blank(char):
            continue
        if last is not None:
            seen = quote[last + 1:index]
            if LINE_BREAKS & set(seen):
                codes.append(LINE_BREAK_CODE)
            else:
                white = ''.join(c for c in seen if c.isspace())
                codes.append(table.setdefault(white, chr(0x100 + len(table))))
        chars.append(char)
        where.append(index)
        last = index
    return ''.join(chars), where, ''.join(codes), table


def _charge(state, steps, scan=False):
    """Spend `steps` Python-level steps (or, for a C-level scan, steps are the scanned length) from the fact and the request budget."""
    steps = steps // SCAN_UNIT + 1 if scan else steps
    state['request']['steps'] -= steps
    if not scan:
        state['budget'] -= steps
    if state['budget'] < 0 or state['request']['steps'] < 0:
        raise _OutOfBudget


def _gap_pattern(spelling, cache):
    """The non-blank characters of `spelling` joined, and its gaps as (whitespace, has a line break); cached per index."""
    if cache is not None and spelling in cache:
        return cache[spelling]
    chars, gaps = _runs(spelling)
    value = (''.join(chars), [(''.join(c for c in gap if c.isspace()), bool(LINE_BREAKS & set(gap))) for gap in gaps])
    if cache is not None:
        cache[spelling] = value
    return value


def _search(spelling, quote, state, cache=None):
    """`quote_spacing_variant` against shared `state`: dict with 'budget' (this fact's steps left), 'request' and 'layout' (the quote's, built on demand)."""
    needle, wanted = _gap_pattern(spelling, cache)
    _charge(state, len(spelling))
    if not wanted:
        return None
    if state['layout'] is None:
        _charge(state, len(quote))
        state['layout'] = quote_layout(quote)
    compact_quote, where, quote_codes, table = state['layout']
    _charge(state, len(wanted))
    codes = ''.join(LINE_BREAK_CODE if lb else table.get(white, MISSING_CODE) for white, lb in wanted)
    start = compact_quote.find(needle)
    while start >= 0:
        _charge(state, 1)
        seen = quote_codes[start:start + len(codes)]
        if seen != codes:
            _charge(state, len(codes))
            if any(a != b and a != LINE_BREAK_CODE and b != LINE_BREAK_CODE for a, b in zip(codes, seen, strict=True)):
                return quote[where[start]:where[start + len(needle) - 1] + 1]
        start = compact_quote.find(needle, start + 1)
    return None


def quote_spacing_variant(spelling, quote):
    """The quote passage that spells `spelling` with different spacing, or None. Display only: never used to find or link an entity.

    None when `spelling` appears exactly in `quote`, or when no passage matches. Occurrences are searched in order and completely, so
    a passage that differs only at a line break (ignored, as in #828 and `spacing_kind`) or only in invisible characters does not hide
    a later one that differs in whitespace presence or run width. A passage counts when some gap between two characters holds
    different whitespace on the two sides and neither side has a line break there. Gives up (None) past MAX_FACT_STEPS steps;
    `fact_spacing_warnings` reports that as incomplete.
    """
    if not isinstance(spelling, str) or not isinstance(quote, str) or spelling in quote or not compact_fast(spelling):
        return None
    try:
        return _search(spelling, quote, {'budget': MAX_FACT_STEPS, 'request': new_request_budget(), 'layout': None})
    except _OutOfBudget:
        return None


INDEX_ROW_STEPS = 20
OUT_OF_BUDGET = {'out of budget': True}


def fact_endpoint_index(pin, request=None):
    """{'ids': {entity ID: its spellings}, 'aliases': {alias: entity ID}, 'compact': {spelling: its non-blank characters}, 'gaps': {}} of an
    extraction pin's dictionary, or None if unreadable. 'gaps' is a cache `_search` fills.

    Unreadable: a row that is not a dict of strings, a spelling with no non-blank character (empty, whitespace or invisible only: it
    would match every quote), or an alias of an unknown entity.

    With a `request` budget (`new_request_budget`), the index costs INDEX_ROW_STEPS per spelling row (two per entity, one per alias),
    checked before any work: if that does not fit, nothing is built or charged and OUT_OF_BUDGET is returned, which
    `fact_spacing_warnings` reports as incomplete. The index is built in locals and returned whole, never half-built.
    """
    try:
        entities, aliases = pin['payload']['entities'], pin['payload']['aliases']
        if not isinstance(entities, list) or not isinstance(aliases, list):
            return None
        if request is not None:
            cost = INDEX_ROW_STEPS * (2 * len(entities) + len(aliases))
            if request['steps'] < cost:
                return OUT_OF_BUDGET
            request['steps'] -= cost
        spellings, compacts = {}, {}
        by_alias: dict[str, str] = {}
        for row in entities:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('id', 'name')):
                return None
            spellings[row['id']] = [row['id'], row['name']]
        for row in aliases:
            if not isinstance(row, dict) or not all(isinstance(row.get(k), str) for k in ('alias', 'entity')) or row['entity'] not in spellings:
                return None
            spellings[row['entity']].append(row['alias'])
            by_alias.setdefault(row['alias'], row['entity'])
        for names in spellings.values():
            for name in names:
                if name not in compacts:
                    compacts[name] = compact_fast(name)
                    if not compacts[name]:
                        return None
        return {'ids': {k: list(dict.fromkeys(v)) for k, v in spellings.items()}, 'aliases': by_alias, 'compact': compacts, 'gaps': {}}
    except (KeyError, TypeError):
        return None


def fact_spacing_warnings(payload, index, request=None):
    """For a fact's subject and object, the spellings of that entity found in the quote only with different spacing.

    `index` is `fact_endpoint_index` of the pin of the fact's run; None (no readable pin) gives no warnings and 'available' false, and
    OUT_OF_BUDGET (the index did not fit the request budget) gives none and 'incomplete' true.
    An endpoint is found by its exact ID or alias; one with an exact spelling in the quote gives no warning. A spelling whose
    non-blank characters are not in the quote's is skipped by a C-level test. Occurrences of the rest are compared by gap codes at
    C speed and searched completely.

    Work is bounded twice: MAX_FACT_STEPS Python-level steps for this fact, and `request` (a `new_request_budget`, shared by the facts
    of one request; fresh if omitted) of MAX_REQUEST_STEPS, which also charges each C-level scan one step per SCAN_UNIT characters
    of quote plus one. Past either bound the warnings found so far are returned with 'incomplete' true, and so is every later
    fact of a spent request.
    """
    unavailable = {'available': False, 'warnings': []}
    if index is OUT_OF_BUDGET:
        return {'available': True, 'incomplete': True, 'warnings': []}
    if index is None or not isinstance(payload, dict):
        return unavailable
    quote, warnings = payload.get('quote'), []
    state = {'budget': MAX_FACT_STEPS, 'request': request if request is not None else new_request_budget(), 'layout': None}
    incomplete, searched = False, {}
    try:
        if isinstance(quote, str):
            _charge(state, len(quote), scan=True)
            compact_quote = compact_fast(quote)
            for field in ('subject', 'object'):
                value = payload.get(field)
                entity = value if value in index['ids'] else index['aliases'].get(value)
                spellings = index['ids'].get(entity, [])
                _charge(state, len(quote) * len(spellings), scan=True)
                if any(s in quote for s in spellings):
                    continue
                _charge(state, len(quote) * len(spellings), scan=True)
                for spelling in spellings:
                    if index['compact'][spelling] not in compact_quote:
                        continue
                    if spelling not in searched:
                        searched[spelling] = _search(spelling, quote, state, index['gaps'])
                    found = searched[spelling]
                    if found is not None:
                        warnings.append({'field': field, 'entity': entity, 'spelling': spelling, 'found': found})
    except _OutOfBudget:
        incomplete = True
    except TypeError:
        return unavailable
    return {'available': True, 'incomplete': incomplete, 'warnings': warnings}


def spacing_link(spelling, target):
    """Whether `spelling` differs from the target's ID only in whitespace, or from its name only in whitespace.

    An exact match of the name is not a difference, unless that name itself differs from the ID only in whitespace:
    spellings resolve to the ID, so such an alias still links two spellings. For refusing a link, never for making one.
    """
    return spelling != target['id'] and (compact(spelling) == compact(target['id'])
                                         or (spelling != target['name'] and compact(spelling) == compact(target['name'])))


def name_link(name, target):
    """Whether a demoted entity's `name` differs from the target's ID or name only in whitespace.

    Unlike an alias spelling, a name does not resolve to the ID, so the target's own name is not compared with its own ID here.
    """
    return name != target['name'] and spacing_link(name, target)


def spacing_conflict(alias, entity_id, entities, aliases):
    """Whether a model alias would tie spellings that differ only in whitespace: to its target, by demoting a same-spelled entity, or to a sibling alias."""
    target = entities[entity_id]
    demoted = entities.get(alias)
    return (spacing_link(alias, target) or (demoted is not None and name_link(demoted['name'], target))
            or (alias not in (target['id'], target['name'])
                and any(row['entity'] == entity_id and row['alias'] != alias and compact(row['alias']) == compact(alias) for row in aliases)))


def spacing_conflicts(dictionary):
    """(alias, entity) pairs of `dictionary` that hold such a tie. A tie moved onto another target is a new pair."""
    entities = {row['id']: row for row in dictionary['entities']}
    groups: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for row in dictionary['aliases']:
        groups.setdefault((row['entity'], compact(row['alias'])), []).append((row['alias'], row['entity']))
    return ({(row['alias'], row['entity']) for row in dictionary['aliases'] if spacing_link(row['alias'], entities[row['entity']])}
            | {pair for group in groups.values() if len(group) > 1 for pair in group})


def demoted_name_tie(redirects, entities, candidate):
    """Whether a demoted entity's name differs only in whitespace from another spelling its final target ends up holding."""
    for alias, target_id in redirects.items():
        name = entities[alias]['name']
        spellings = {r['alias'] for r in candidate['aliases'] if r['entity'] == target_id and r['alias'] != alias}
        spellings |= {entities[other]['name'] for other, final in redirects.items() if final == target_id}
        if any(s != name and compact(s) == compact(name) for s in spellings):
            return True
    return False


SPACING_REFUSAL = ('이 묶음에 공백만 다른 표기를 모델 제안으로 연결하게 되는 제안이 있어 묶음 전체를 보류했습니다. '
                   '이 제안 자체에는 공백 차이가 없을 수 있습니다. 같은 대상이면 사람이 사전에 추가하세요.')
SPACING_RENAME_REFUSAL = ('이 묶음에 개체 이름을 그 개체 ID와 띄어쓰기만 다른 표기로 바꾸는 제안이 있어 묶음 전체를 보류했습니다. '
                          '이 제안 자체에는 그런 변경이 없을 수 있습니다. 같은 대상이면 사람이 내용 수정으로 이름을 바꾸세요.')


def key_field(kind):
    return 'alias' if kind == 'aliases' else 'id'


def quote_for(ref, spans):
    if not isinstance(ref, list) or len(ref) != 2 or any(not isinstance(v, str) for v in ref):
        raise ValueError('quote_ref에는 시작·끝 ID 문자열 두 개가 필요합니다.')
    positions = {s['id']: i for i, s in enumerate(spans)}
    if ref[0] not in positions or ref[1] not in positions or positions[ref[0]] > positions[ref[1]]:
        return ''
    return ''.join(s['text'] for s in spans[positions[ref[0]]:positions[ref[1]]+1]).strip()


def event(kind, key, before, after, status, reason, origin):
    return copy.deepcopy(dict(kind=kind, key=key, before=before, after=after,
                              status=status, reason=reason[:300], origin=origin))


def parse_revisions(rows, spans):
    from .service import text
    if not isinstance(rows, list) or len(rows) > MAX_REVISIONS:
        raise ValueError('사전 revisions는 최대 40개 배열이어야 합니다.')
    result = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'kind', 'key', 'before', 'after', 'reason'}:
            raise ValueError('revisions에는 kind, key, before, after, reason이 필요합니다.')
        kind = row['kind']
        if not isinstance(kind, str) or kind not in FIELDS:
            raise ValueError('알 수 없는 사전 조정 종류입니다.')
        if (not isinstance(row['before'], dict) or set(row['before']) != FIELDS[kind]
                or not isinstance(row['after'], dict) or set(row['after']) != FIELDS[kind] | {'quote_ref'}):
            raise ValueError('조정 before는 기존 정의, after는 전체 정의와 quote_ref여야 합니다.')
        before = _dictionary_definition(kind, row['before'])
        after = _dictionary_definition(kind, row['after']) | {'quote': quote_for(row['after']['quote_ref'], spans)}
        result.append(dict(kind=kind, key=text(row['key'], 'key'), before=before, after=after,
                           reason=text(row['reason'], 'reason', 300)))
    return result


def revise(previous, baseline, proposals, source, blocked_keys=()):
    if not proposals:
        return baseline, []
    candidate = copy.deepcopy(baseline)
    seen = set()
    failure = None
    for row in proposals:
        kind, key = row['kind'], row['key']
        old = next((r for r in previous[kind] if r[key_field(kind)] == key), None)
        if ((kind, key) in seen or (kind, key) in blocked_keys or old is None
                or _dictionary_definition(kind, old) != row['before']
                or row['before'][key_field(kind)] != key or row['after'][key_field(kind)] != key):
            failure = '기존 정의 불일치·중복 조정·정규화와의 충돌로 조정 묶음을 보류했습니다.'
        seen.add((kind, key))
        if not row['after']['quote']:
            failure = '현재 구간의 유효한 근거가 없어 조정 묶음을 보류했습니다.'
        for index, current in enumerate(candidate[kind]):
            if current[key_field(kind)] == key:
                candidate[kind][index] = row['after']
                break
        else:
            failure = '조정 대상이 현재 초안에 없어 조정 묶음을 보류했습니다.'
    if failure is None:
        try:
            candidate = validate_dictionary(candidate, source)
        except ValueError:
            failure = '변경한 이름·타입·별칭의 근거 또는 의존 정의가 맞지 않아 조정 묶음을 보류했습니다.'
        else:
            if spacing_conflicts(candidate) - spacing_conflicts(baseline):
                failure = SPACING_REFUSAL
            elif ({(r['id'], r['name']) for r in spacing_name_rows(candidate)}
                  - {(r['id'], r['name']) for r in spacing_name_rows(baseline)}):
                failure = SPACING_RENAME_REFUSAL
    events = []
    for index, row in enumerate(proposals, 1):
        old = next((r for r in previous[row['kind']] if r[key_field(row['kind'])] == row['key']), None)
        events.append(event(row['kind'], row['key'], old, row['after'], 'deferred' if failure else 'applied',
                            failure or row['reason'], f'revisions[{index}]'))
    return (baseline if failure else candidate), events


def canonicalize(dictionary, proposals, source, revised_keys=()):
    """Only explicit grounded same-type entity-ID aliases can demote entities."""
    result = dictionary
    events = []
    pending = {r['alias']: r for r in proposals}
    while pending:
        component = {min(pending)}
        while True:
            expanded = component | {r['entity'] for key, r in pending.items() if key in component}
            expanded |= {key for key, r in pending.items() if r['entity'] in expanded}
            if expanded == component:
                break
            component = expanded
        rows = [pending.pop(key) for key in sorted(component & pending.keys())]
        entities = {r['id']: r for r in result['entities']}
        edges = {r['alias']: r['entity'] for r in rows}
        redirects = {}
        failure = None
        for row in rows:
            old, target = entities[row['alias']], entities[row['entity']]
            if (old['type'] != target['type'] or old['name'] not in row['quote']
                    or target['name'] not in row['quote']
                    or ('entities', row['alias']) in revised_keys or ('entities', row['entity']) in revised_keys
                    or ('aliases', row['alias']) in revised_keys):
                failure = '동일 타입·양쪽 이름의 근거가 부족하거나 명시적 조정과 겹쳐 통합을 보류했습니다.'
            visited = set()
            target_id = row['alias']
            while target_id in edges and target_id not in visited:
                visited.add(target_id); target_id = edges[target_id]
            if target_id in visited:
                failure = '순환하는 정규 이름 제안이므로 통합을 보류했습니다.'
            redirects[row['alias']] = target_id
            if failure is None and target_id in entities and name_link(old['name'], entities[target_id]):
                failure = SPACING_REFUSAL
        candidate = copy.deepcopy(result)
        candidate['entities'] = [r for r in candidate['entities'] if r['id'] not in redirects]
        dependent_events = []
        for row in candidate['aliases']:
            if row['entity'] in redirects:
                before = dict(row); row['entity'] = redirects[row['entity']]
                dependent_events.append(event('aliases', row['alias'], before, row, 'applied',
                                              '정규 개체 통합에 따라 같은 근거를 유지하며 대상을 조정했습니다.', 'canonicalization'))
        candidate['aliases'].extend(dict(row, entity=redirects[row['alias']]) for row in rows)
        if failure is None:
            try:
                candidate = validate_dictionary(candidate, source)
            except ValueError:
                failure = '기존 별칭·관계의 근거 또는 의존 정의가 유지되지 않아 통합을 보류했습니다.'
            else:
                if spacing_conflicts(candidate) - spacing_conflicts(result) or demoted_name_tie(redirects, entities, candidate):
                    failure = SPACING_REFUSAL
        for row in rows:
            events.append(event('entities', row['alias'], entities[row['alias']],
                                dict(row, entity=redirects[row['alias']]), 'deferred' if failure else 'applied',
                                failure or '원문의 명시적 별칭 제안에 따라 중복 개체를 정규 개체의 별칭으로 통합했습니다.', 'canonicalization'))
        if failure is None:
            result = candidate; events.extend(dependent_events)
    return result, events


def identity(run, doc):
    return dict(run_id=run['id'], document_id=run['document_id'], version=run['version'], source_sha256=doc['sha256'])


def new_report(run, doc):
    return dict(schema_version=1, identity=identity(run, doc), chunks=[], items=[], legacy_chars=0)


def add_chunk(report, offset, length, events):
    result = copy.deepcopy(report)
    counts = {state: sum(e['status'] == state for e in events) for state in ('applied', 'deferred')}
    result['chunks'].append(dict(offset=offset, length=length, **counts))
    room = MAX_DETAILS - len(result['items'])
    result['items'].extend(dict(e, chunk=len(result['chunks']), number=i) for i, e in enumerate(events[:room], 1))
    return result


def validate_report(value, run, doc, accepted):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'identity', 'chunks', 'items', 'legacy_chars'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or not isinstance(value['identity'], dict) or type(value['identity'].get('version')) is not int
            or value['identity'] != identity(run, doc)):
        raise ValueError('사전 조정 기록의 실행·원문 정보 오류')
    slices = list(bounded_spans(doc['text'], DICTIONARY_CHUNK_SIZE))
    chunks, items = value['chunks'], value['items']
    if not isinstance(chunks, list) or len(chunks) > len(slices) or not isinstance(items, list):
        raise ValueError('사전 조정 기록의 구간 오류')
    total = 0
    for entry, (offset, text) in zip(chunks, slices, strict=False):
        if (not isinstance(entry, dict) or set(entry) != {'offset', 'length', 'applied', 'deferred'}
                or type(entry['offset']) is not int or entry['offset'] != offset
                or type(entry['length']) is not int or entry['length'] != len(text)
                or any(type(entry[k]) is not int or not 0 <= entry[k] <= MAX_EVENTS for k in ('applied', 'deferred'))):
            raise ValueError('사전 조정 집계 오류')
        total += entry['applied'] + entry['deferred']
    if sum(r['length'] for r in chunks) != accepted or len(items) != min(total, MAX_DETAILS):
        raise ValueError('사전 조정 위치·상세 개수 오류')
    legacy = value['legacy_chars']
    if type(legacy) is not int or legacy not in {0, *(r['offset']+r['length'] for r in chunks)}:
        raise ValueError('이전 사전 조정 기록 범위 오류')
    expected: list[tuple[int, int]] = []
    for i, row in enumerate(chunks, 1):
        if row['offset'] < legacy and row['applied'] + row['deferred']:
            raise ValueError('이전 형식에 조정 기록이 있습니다.')
        expected.extend((i, n) for n in range(1, min(row['applied']+row['deferred'], MAX_DETAILS-len(expected))+1))
        if len(expected) == MAX_DETAILS: break
    counts = {i: {'applied': 0, 'deferred': 0} for i in range(1, len(chunks)+1)}
    for row, position in zip(items, expected, strict=True):
        if (not isinstance(row, dict) or set(row) != {'kind', 'key', 'before', 'after', 'status', 'reason', 'origin', 'chunk', 'number'}
                or type(row['chunk']) is not int or type(row['number']) is not int
                or (row['chunk'], row['number']) != position
                or not isinstance(row['kind'], str) or row['kind'] not in LIMITS
                or not isinstance(row['status'], str) or row['status'] not in ('applied', 'deferred')
                or any(not isinstance(row[k], str) or not 1 <= len(row[k]) <= limit for k, limit in (('key',200),('reason',300),('origin',80)))):
            raise ValueError('사전 조정 상세 오류')
        end = chunks[row['chunk']-1]['offset'] + chunks[row['chunk']-1]['length']
        for side in ('before', 'after'):
            record = row[side]
            if record is None:
                if side == 'after': raise ValueError('조정 제안 누락')
                continue
            if not isinstance(record, dict): raise ValueError('조정 정의 오류')
            kind = next((k for k, fields in FIELDS.items() if set(record) == fields | {'quote'}), None)
            if kind is None or _dictionary_definition(kind, record) != {k:v for k,v in record.items() if k!='quote'}:
                raise ValueError('조정 정의 형식 오류')
            quote = record['quote']
            if not isinstance(quote, str) or len(quote) > 8000 or (quote and quote not in doc['text'][:end]) or (not quote and row['status']=='applied'):
                raise ValueError('조정 근거 오류')
        counts[row['chunk']][row['status']] += 1
    for i, row in enumerate(chunks, 1):
        for status in ('applied', 'deferred'):
            if counts[i][status] > row[status] or (total <= MAX_DETAILS and counts[i][status] != row[status]):
                raise ValueError('조정 상세와 집계 불일치')
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_REPORT_BYTES:
        raise ValueError('사전 조정 기록 크기 제한')
    return copy.deepcopy(value)


def public_report(report, include_items=True):
    counts = {s: sum(r[s] for r in report['chunks']) for s in ('applied', 'deferred')}
    total = sum(counts.values())
    return dict(available=True, **counts, total_count=total, truncated_count=total-len(report['items']),
                legacy_chars=report['legacy_chars'], details_loaded=include_items,
                processed_chars=sum(r['length'] for r in report['chunks']),
                items=report['items'] if include_items else [])


def read_report(root, store, run_id, *, document_id=None, version=None, include_items=True):
    unavailable: dict = dict(available=False, total_count=None, items=[], reason='사전 조정 기록을 확인할 수 없습니다. 조정이 없었다는 뜻은 아닙니다.')
    try:
        from .dictionary_pipeline import CHECKPOINT_LIMIT
        run = store.run(run_id)
        if run['stage'] != 'ontology' or (document_id is not None and run['document_id'] != document_id) or (version is not None and run['version'] != version):
            return unavailable
        with store.connect() as db:
            row = db.execute('SELECT text,sha256 FROM versions WHERE document_id=? AND version=?', (run['document_id'],run['version'])).fetchone()
        if row is None: return unavailable
        raw = safe_file(root, f'runs/{run_id}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        saved = json.loads(raw) if raw else None
        if (not isinstance(saved, dict) or saved.get('schema_version') != 3 or saved.get('run_id') != run_id
                or type(saved.get('offset')) is not int or not isinstance(saved.get('contract'), dict)
                or type(saved['contract'].get('reconciliation_policy')) is not int
                or saved['contract']['reconciliation_policy'] != POLICY):
            return unavailable
        report = validate_report(saved.get('reconciliations'), run, dict(row), saved['offset'])
        if run['status']=='success' and saved['offset']!=len(row['text']):return unavailable
        result = public_report(report, include_items)
        return result | {'partial': saved['offset'] < len(row['text'])}
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return unavailable
