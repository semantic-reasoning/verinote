"""Generation-only filtering that preserves prior definitions and audits exclusions."""
from .ontology import (
    LIMITS,
    _dictionary_definition,
    _dictionary_shape,
    merge_dictionary,
    validate_dictionary,
)

FILTER_POLICY = 3
SUPPORTED_FILTER_POLICIES = (1, 2, FILTER_POLICY)
REASONS = {
    'reference': '선택한 원문 구간이 없거나 순서가 잘못되었습니다.',
    'entity_evidence': '선택한 원문에 개체 이름이 없습니다.',
    'alias_existing_entity': '기존 개체 ID와 별칭 이름이 같아 새 별칭만 제외했습니다.',
    'alias_existing_target': '기존 별칭과 다른 개체를 가리켜 새 별칭만 제외했습니다. 기존 매핑을 보존했으므로 승인 전 동음이의 별칭의 의미와 범위를 확인하세요.',
    'relation_undeclared_type': '기존·신규 개체에 선언되지 않은 타입을 사용하는 관계 후보를 제외했습니다. 승인 전 관계 정의와 필요한 개체를 확인하세요.',
    'alias_evidence': '선택한 원문에 별칭과 정규 이름의 근거가 없습니다.',
    'excluded_entity': '근거 부족으로 제외된 개체를 가리키는 별칭입니다.',
    'excluded_type': '근거 부족으로 제외된 개체의 타입에 의존하는 관계입니다.',
}
DEPENDENT = {'excluded_entity', 'excluded_type'}


def merge_generated_dictionary(previous, output, spans, source, chunk):
    """Preflight everything first; preserve prior entities and discard unsafe aliases."""
    _dictionary_shape(output, per_chunk=True)
    previous = validate_dictionary(previous, source)
    definitions: dict[str, list[dict]] = {kind: [] for kind in LIMITS}
    fields = {'entities': {'id', 'name', 'type'}, 'aliases': {'alias', 'entity'},
              'relations': {'id', 'description', 'subject_types', 'object_types'}}
    all_entities = {row['id']: row for row in previous['entities']}
    target_exclusions = set()
    for kind in LIMITS:
        identity = 'alias' if kind == 'aliases' else 'id'
        prior = {row[identity]: _dictionary_definition(kind, row) for row in previous[kind]}
        seen: dict[str, dict] = {}
        for index, row in enumerate(output[kind], 1):
            if set(row) != fields[kind] | {'quote_ref'}:
                raise ValueError(f'{kind}[{index}]: quote_ref와 지정된 필드만 필요합니다.')
            ref = row['quote_ref']
            if not isinstance(ref, list) or len(ref) != 2 or any(not isinstance(value, str) for value in ref):
                raise ValueError(f'{kind}[{index}]: quote_ref에는 시작·끝 ID 문자열 두 개가 필요합니다.')
            definition = _dictionary_definition(kind, row)
            key = definition[identity]
            if key in seen and seen[key] != definition:
                raise ValueError(f'사전 정의 충돌 (이번 응답 내부): {kind}[{index}] · {key}')
            if key in prior and prior[key] != definition:
                if kind == 'aliases':
                    target_exclusions.add(index)
                else:
                    raise ValueError(f'사전 정의 충돌 (기존 사전과 현재 구간 사이): {kind}[{index}] · {key}')
            seen[key] = definition
            definitions[kind].append(definition)
            if kind == 'entities':
                all_entities[key] = definition
    # Check all dependencies before any evidence-based exclusion can hide them.
    all_types = {row['type'] for row in all_entities.values()}
    aliases = {row['alias']: row['entity'] for row in previous['aliases']}
    previous_entity_ids = {row['id'] for row in previous['entities']}
    namespace_exclusions = set()
    for index, row in enumerate(definitions['aliases'], 1):
        if row['entity'] not in all_entities:
            raise ValueError(f'aliases[{index}]: 선언되지 않은 개체를 가리킵니다.')
        if index in target_exclusions:
            continue
        if row['alias'] != row['entity']:
            if row['alias'] in previous_entity_ids:
                namespace_exclusions.add(index)
            elif row['alias'] in all_entities or aliases.get(row['alias'], row['entity']) != row['entity']:
                raise ValueError(f'사전 정의 충돌: aliases[{index}] · 별칭 이름이 중복됩니다.')
            else:
                aliases[row['alias']] = row['entity']
    if set(aliases) & set(all_entities):
        raise ValueError('사전 정의 충돌: 별칭과 개체 ID가 중복됩니다.')
    undeclared_types = {index for index, row in enumerate(definitions['relations'], 1)
                        if (set(row['subject_types']) | set(row['object_types'])) - all_types}

    positions = {span['id']: i for i, span in enumerate(spans)}
    kept: dict[str, list[dict]] = {kind: [] for kind in LIMITS}
    entities = {row['id']: row for row in previous['entities']}
    excluded = []
    for kind in LIMITS:
        for index, (row, definition) in enumerate(zip(output[kind], definitions[kind], strict=True), 1):
            start, end = row['quote_ref']
            code = None
            if kind == 'aliases' and index in target_exclusions:
                code = 'alias_existing_target'
                quote = ''
            elif kind == 'aliases' and index in namespace_exclusions:
                code = 'alias_existing_entity'
                quote = ''
            elif kind == 'relations' and index in undeclared_types:
                code = 'relation_undeclared_type'
                quote = ''
            elif start not in positions or end not in positions or positions[start] > positions[end]:
                code = 'reference'
                quote = ''
            else:
                quote = ''.join(span['text'] for span in spans[positions[start]:positions[end] + 1])
                if kind == 'entities' and definition['name'] not in quote:
                    code = 'entity_evidence'
                elif kind == 'aliases':
                    target = definition['entity']
                    if target not in entities:
                        code = 'excluded_entity'
                    elif definition['alias'] not in quote or (definition['alias'] != target and entities[target]['name'] not in quote):
                        code = 'alias_evidence'
                elif kind == 'relations':
                    types = {entity['type'] for entity in entities.values()}
                    if (set(definition['subject_types']) | set(definition['object_types'])) - types:
                        code = 'excluded_type'
            if code:
                excluded.append({'kind': kind, 'row': index, 'identifier': definition['alias' if kind == 'aliases' else 'id'][:80], 'code': code})
                continue
            kept[kind].append(definition | {'quote': quote})
            if kind == 'entities':
                entities[definition['id']] = definition
    return merge_dictionary(previous, kept, source, chunk), excluded
