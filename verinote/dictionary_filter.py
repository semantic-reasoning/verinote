"""Generation filtering and evidence-backed draft reconciliation."""
from .ontology import (
    LIMITS,
    _dictionary_definition,
    _dictionary_shape,
    validate_dictionary,
)

FILTER_POLICY = 4
SUPPORTED_FILTER_POLICIES = (1, 2, 3, FILTER_POLICY)
REASONS = {
    'definition_conflict': '충돌 제안을 조정 검토 내역에 보류했습니다. 기존 정의가 옳다고 확정한 것은 아닙니다.',
    'entity_alias_conflict': '개체 ID가 별칭과 겹쳐 조정 검토 내역에 보류했습니다.',
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
    dictionary, excluded, _ = reconcile_generated_dictionary(previous, output, spans, source, chunk)
    return dictionary, excluded


def reconcile_generated_dictionary(previous, output, spans, source, chunk):
    from .dictionary_reconciliation import (
        FIELDS,
        canonicalize,
        event,
        key_field,
        parse_revisions,
        quote_for,
        revise,
    )
    if not isinstance(output, dict) or set(output) not in (set(LIMITS), set(LIMITS) | {'revisions'}):
        raise ValueError('사전에는 entities, aliases, relations와 선택적 revisions만 필요합니다.')
    _dictionary_shape({k: output[k] for k in LIMITS}, per_chunk=True)
    previous = validate_dictionary(previous, source)
    revisions = parse_revisions(output.get('revisions', []), spans)
    for proposal in revisions:
        if proposal['after']['quote'] not in chunk:
            proposal['after']['quote'] = ''
    definitions: dict[str, list[dict]] = {kind: [] for kind in LIMITS}
    conflicts = set()
    for kind in LIMITS:
        seen: dict[str, dict] = {}
        for index, row in enumerate(output[kind], 1):
            if set(row) != FIELDS[kind] | {'quote_ref'}:
                raise ValueError(f'{kind}[{index}]: quote_ref와 지정된 필드만 필요합니다.')
            definition = _dictionary_definition(kind, row)
            quote = quote_for(row['quote_ref'], spans)
            key = definition[key_field(kind)]
            if key in seen and seen[key] != definition:
                conflicts.add((kind, key))
            seen[key] = definition
            definitions[kind].append(definition | {'quote': quote})
    # Unknown references are structural faults, even when another row conflicts.
    all_entities = {r['id']: r for r in previous['entities'] + definitions['entities']}
    all_types = {r['type'] for r in previous['entities'] + definitions['entities']}
    for index, row in enumerate(definitions['aliases'], 1):
        if row['entity'] not in all_entities:
            raise ValueError(f'aliases[{index}]: 선언되지 않은 개체를 가리킵니다.')
    result = {kind: list(previous[kind]) for kind in LIMITS}
    excluded, changes, canonical = [], [], []
    canonical_rows = set()
    for kind in LIMITS:
        if kind == 'aliases':
            # Add entities first, then reconcile old definitions before checking
            # new aliases/relations against their names and types. Reserve the
            # same grounded canonical candidates so overlapping edits defer.
            entities = {r['id']: r for r in result['entities']}
            canonical_keys: set[tuple[str, str]] = set()
            target_names = {key: {row['name']} for key, row in entities.items()}
            for proposal in revisions:
                if proposal['kind'] == 'entities' and proposal['key'] in target_names:
                    target_names[proposal['key']].add(proposal['after']['name'])
            for row in definitions['aliases']:
                if (row['alias'] in entities and row['entity'] in entities and row['alias'] != row['entity']
                        and ('aliases', row['alias']) not in conflicts and row['quote'] and row['quote'] in chunk
                        and row['alias'] in row['quote'] and any(name in row['quote'] for name in target_names[row['entity']])):
                    canonical_keys.update((('entities', row['alias']), ('entities', row['entity']), ('aliases', row['alias'])))
            result, revision_events = revise(previous, validate_dictionary(result, source), revisions, source, canonical_keys)
            changes.extend(revision_events)
            all_types.update(r['type'] for r in result['entities'])
        for index, row in enumerate(definitions[kind], 1):
            key = row[key_field(kind)]
            old = next((r for r in result[kind] if r[key_field(kind)] == key), None)
            entities = {r['id']: r for r in result['entities']}
            code = None
            if not row['quote'] or row['quote'] not in chunk:
                code = 'reference'
            elif kind == 'entities' and row['name'] not in row['quote']:
                code = 'entity_evidence'
            elif kind == 'aliases':
                target = entities.get(row['entity'])
                if target is None:
                    code = 'excluded_entity'
                elif row['alias'] not in row['quote'] or (row['alias'] != row['entity'] and target['name'] not in row['quote']):
                    code = 'alias_evidence'
            elif kind == 'relations':
                types = set(row['subject_types']) | set(row['object_types'])
                if types - all_types:
                    code = 'relation_undeclared_type'
                elif types - {r['type'] for r in entities.values()}:
                    code = 'excluded_type'
            semantic = None
            if (kind, key) in conflicts:
                semantic = 'definition_conflict'
            elif old is not None and _dictionary_definition(kind, old) != _dictionary_definition(kind, row):
                semantic = 'alias_existing_target' if kind == 'aliases' else 'definition_conflict'
            elif kind == 'entities':
                alias = next((r for r in result['aliases'] if r['alias'] == key), None)
                if alias is not None:
                    semantic, old = 'entity_alias_conflict', alias
            if code is None and semantic is not None:
                code = semantic
                changes.append(event(kind, key, old, row, 'deferred',
                                     '일반 추가와 다른 정의가 충돌했습니다. 근거와 before를 갖춘 명시적 revisions 또는 승인 전 수정이 필요합니다.', f'{kind}[{index}]'))
            if code is not None:
                excluded.append(dict(kind=kind, row=index, identifier=key[:80], code=code))
                continue
            if kind == 'aliases' and row['alias'] == row['entity']:
                continue
            if old is not None:
                continue  # Exact definition replay keeps its original evidence.
            if kind == 'aliases' and key in entities:
                canonical.append(row)
                canonical_rows.add(index)
                continue
            result[kind].append(row)
    result = validate_dictionary(result, source)
    result, canonical_events = canonicalize(result, canonical, source, {(r['kind'], r['key']) for r in revisions})
    changes.extend(canonical_events)
    # Keep legacy exclusion counts for an alias proposal that could not be merged.
    deferred_ids = {e['key'] for e in canonical_events if e['status']=='deferred' and e['origin']=='canonicalization'}
    previous_ids = {r['id'] for r in previous['entities']}
    for index, row in enumerate(definitions['aliases'], 1):
        if index in canonical_rows and row['alias'] in deferred_ids:
            excluded.append(dict(kind='aliases', row=index, identifier=row['alias'][:80],
                                 code='alias_existing_entity' if row['alias'] in previous_ids else 'definition_conflict'))
    excluded.sort(key=lambda row: (list(LIMITS).index(row['kind']), row['row']))
    return result, excluded, changes
