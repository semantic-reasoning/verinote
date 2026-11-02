"""Snapshot-local meanings. Labels and aliases are selectors, never graph identity."""
from .store import digest

VERSION = 1
CONTRACT = 'scoped-ontology-v1'
PREFIXES = ('entity:', 'relation:')


def _pin(pin):
    # A matching checksum proves retention, not a valid dictionary. Validate
    # source-independent invariants before assigning any pinned identity.
    from .ontology import _dictionary_definition, _dictionary_shape
    from .service import text
    if (not isinstance(pin, dict) or not isinstance(pin.get('id'), str) or not pin['id']
            or type(pin.get('revision')) is not int or pin['revision'] < 1
            or not isinstance(pin.get('payload'), dict)):
        return None
    payload = pin['payload']
    try:
        if pin.get('sha256') != digest(payload): return None
        _dictionary_shape(payload)
        parsed: dict[str, list[dict]] = {}
        for kind in ('entities', 'aliases', 'relations'):
            parsed[kind] = []
            for row in payload[kind]:
                definition = _dictionary_definition(kind, row)
                expected = definition | {'quote': text(row.get('quote'), 'quote', 8000, allow_cr=True)}
                if row != expected: return None  # Retained dictionaries must already be normalized.
                parsed[kind].append(definition)
        entity_ids = [r['id'] for r in parsed['entities']]
        relation_ids = [r['id'] for r in parsed['relations']]
        if len(set(entity_ids)) != len(entity_ids) or len(set(relation_ids)) != len(relation_ids): return None
        types = {r['type'] for r in parsed['entities']}
        if any(set(r[key]) - types for r in parsed['relations'] for key in ('subject_types', 'object_types')): return None
        names = set(entity_ids)
        for alias in parsed['aliases']:
            if alias['entity'] not in entity_ids or alias['alias'] in names: return None
            names.add(alias['alias'])
    except (ValueError, TypeError, KeyError):
        return None
    return pin


def identity_graph(snapshot):
    """Return entities/relations metadata and approved fact edges with opaque refs.

    Meaning metadata: ref/id/label/name/type/aliases/quote, document_id/version,
    ontology_id/ontology_revision/ontology_sha256, scope_ref, identity_status,
    guidance_sources, current_approved, historical_pin, approval, fact_count.
    Current guidance and historical extraction approval remain separate flags;
    the same frozen pin is represented once. Missing pins use only doc/version.
    """
    if type(snapshot.get('graph_identity_version')) is not int or snapshot['graph_identity_version'] != VERSION:
        raise ValueError('지원하지 않는 그래프 개체 식별 계약입니다.')
    entities: dict[str, dict] = {}; relations: dict[str, dict] = {}
    scopes: dict[tuple, tuple] = {}; scope_sources: dict[tuple, set[str]] = {}
    parsed_pins: dict[int, tuple] = {}
    documents = {d['id']: d for d in snapshot.get('documents', [])}

    def meaning(kind, document_id, version, pin, definition, source):
        scope = {'document_id': document_id, 'version': version, 'ontology_id': pin['id'] if pin else None,
                 'ontology_revision': pin['revision'] if pin else None, 'ontology_sha256': pin['sha256'] if pin else None}
        ref = kind+':'+digest(scope | {'id': definition['id']})
        table = entities if kind == 'entity' else relations
        if ref not in table:
            table[ref] = {'ref': ref, 'id': definition['id'], 'label': definition['id'],
                          'name': definition.get('name', definition['id']), 'type': definition.get('type', ''),
                          'aliases': [], 'quote': definition.get('quote', ''), 'description': definition.get('description', ''),
                          'subject_types': definition.get('subject_types', []), 'object_types': definition.get('object_types', []),
                          **scope, 'scope_ref': 'scope:'+digest(scope),
                          'document_title': documents.get(document_id, {}).get('title', ''),
                          'identity_status': 'pinned' if pin else 'legacy_unresolved',
                          'guidance_sources': [], 'current_approved': False, 'historical_pin': False,
                          'approval': source, 'fact_count': 0}
        row = table[ref]
        if source not in row['guidance_sources']: row['guidance_sources'].append(source)
        row['current_approved'] |= source == 'current_approved'
        row['historical_pin'] |= source == 'historical_pin'
        row['guidance_sources'].sort()
        row['approval'] = 'current_approved' if row['current_approved'] else 'historical_pin' if row['historical_pin'] else 'legacy_unresolved'
        return ref

    def dictionary(document_id, version, raw, source):
        cached = parsed_pins.get(id(raw))
        if cached is None:
            cached = (raw, _pin(raw)); parsed_pins[id(raw)] = cached
        pin = cached[1]
        if pin is None: return None
        key = (document_id, version, pin['id'], pin['revision'], pin['sha256'])
        if key in scopes:
            if source not in scope_sources[key]:
                entity_map, relation_map = scopes[key]
                for ref in set(entity_map.values()) | set(relation_map.values()):
                    row = entities.get(ref) or relations[ref]
                    row['guidance_sources'] = sorted(set(row['guidance_sources']) | {source})
                    row['current_approved'] |= source == 'current_approved'
                    row['historical_pin'] |= source == 'historical_pin'
                    row['approval'] = 'current_approved' if row['current_approved'] else 'historical_pin'
                scope_sources[key].add(source)
            return scopes[key]
        entity_map = {}; relation_map = {}
        for row in pin['payload']['entities']:
            ref = meaning('entity', document_id, version, pin, row, source)
            entity_map[row['id']] = ref
            entities[ref]['aliases'] = sorted({a['alias'] for a in pin['payload']['aliases'] if a['entity'] == row['id']})
        for alias in pin['payload']['aliases']:
            if alias['entity'] in entity_map: entity_map[alias['alias']] = entity_map[alias['entity']]
        for row in pin['payload']['relations']:
            relation_map[row['id']] = meaning('relation', document_id, version, pin, row, source)
        scopes[key] = (entity_map, relation_map); scope_sources[key] = {source}
        return scopes[key]

    for item in snapshot.get('ontologies', []):
        dictionary(item['document_id'], item['version'],
                   {'id': item['id'], 'revision': item['revision'], 'payload': item['payload'], 'sha256': digest(item['payload'])},
                   'current_approved')
    edges = []; counts: dict[str, set] = {}
    for fact in snapshot['facts']:
        document_id, version = fact['document_id'], fact['version']
        frozen = snapshot.get('fact_ontologies', {}).get(fact['id'])
        if isinstance(frozen, str): frozen = snapshot.get('ontology_pins', {}).get(frozen)
        maps = dictionary(document_id, version, frozen, 'historical_pin')
        payload = fact['payload']; refs = []
        for field, kind in (('subject', 'entity'), ('predicate', 'relation'), ('object', 'entity')):
            table = maps[0 if kind == 'entity' else 1] if maps else {}
            ref = table.get(payload[field])
            if ref is None:
                ref = meaning(kind, document_id, version, None, {'id': payload[field]}, 'legacy_unresolved')
            refs.append(ref); counts.setdefault(ref, set()).add(fact['id'])
        edges.append({'id': fact['id'], 'subject_ref': refs[0], 'predicate_ref': refs[1], 'object_ref': refs[2]})
    for row in [*entities.values(), *relations.values()]: row['fact_count'] = len(counts.get(row['ref'], set()))
    def order(row): return (row['label'], row['document_id'], row['version'], row['ref'])
    return {'identity_contract': CONTRACT, 'entities': sorted(entities.values(), key=order),
            'relations': sorted(relations.values(), key=order), 'facts': sorted(edges, key=lambda r: r['id'])}


def candidates(graph, value, kind='entity'):
    """Resolve exact id/name/alias or a ref. Unknown explicit refs fail closed."""
    if kind not in ('entity', 'relation'): raise ValueError('개체 또는 관계 후보가 필요합니다.')
    rows = graph['entities' if kind == 'entity' else 'relations']
    if value.startswith(PREFIXES):
        selected = [row for row in rows if row['ref'] == value]
        if not selected: raise ValueError('이 스냅샷에서 유효하지 않은 개체·관계 참조입니다.')
        return selected
    return [row for row in rows if not value or value in (row['id'], row['name'], *row['aliases'])]


def meaning_candidates(snapshot, value, kind='entity'):
    return candidates(identity_graph(snapshot), value, kind)
