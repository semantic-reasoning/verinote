"""Lossless, deterministic model context; persisted evidence is never rewritten."""
import json
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from typing import Any

from .store import canonical

PROJECTION_VERSION = 2
PROTOCOL = '''Model-context protocol v2: {"$r":"key"} means the complete value of
context_tables[key].value. Expand references recursively, including references
inside table values. {"$o":[shape,values]} is an object: pair the ordered keys
in context_shapes[shape] with values in order, then recursively expand values.
Short keys are local encoding IDs, not semantic entity:/relation: refs.
Resolve tables first; query_graph uses only resolved semantic refs, never short keys.
Tables share exact JSON objects, arrays and strings; no content is summarized.
context_raw_sha256 binds the fully expanded state, including its snapshot.
Each E retains its query/status, rows, scoped identities and proof fact membership.
Meaning/fact/rules kinds describe shared values, not new evidence or approval.
Dictionary guidance is not evidence. Cite only E IDs; K IDs preserve lookup scope
and completeness and are not citations. All offsets, quotes and warnings apply
to expanded values. No omitted or inferred content.
'''
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_NODES = 200000
MAX_DEPTH = 96
_KINDS = {'meaning', 'fact', 'rules', 'object', 'array', 'string'}
_META = {'context_projection_version', 'context_tables', 'context_raw_sha256', 'context_shapes'}
_V1_MARKER = '$context_ref'
_MARKER = '$r'
_OBJECT = '$o'
_RESERVED = {_V1_MARKER, _MARKER, _OBJECT}


@dataclass(frozen=True)
class Projection:
    state: dict[str, Any]
    evidence_ids: frozenset[str]
    lookup_ids: frozenset[str]


def _key(snapshot, kind, value):
    return sha256(canonical([snapshot, kind, value]).encode()).hexdigest()


def _validate_graph(result):
    """Scoped proofs must retain their endpoint and fact membership records."""
    if 'row_refs' not in result:
        return  # Persisted legacy graphs have the original string contract.
    entities = {item['ref'] for item in result['entities']}
    relations = {item['ref'] for item in result['relations']}
    facts = {item['id'] for item in result['facts']}
    edges = {item['id']: item for item in result['fact_refs']}
    if len(result['rows']) != len(result['row_refs']):
        raise ValueError('Unresolvable context rows')
    for row in result['row_refs']:
        if len(row) != 3 or row[0] not in entities or row[1] not in relations or row[2] not in entities:
            raise ValueError('Unresolvable context meaning reference')
    for edge in edges.values():
        if (edge['id'] not in facts or edge['subject_ref'] not in entities or
                edge['object_ref'] not in entities or edge['predicate_ref'] not in relations):
            raise ValueError('Unresolvable context fact reference')
    for proof in result['proofs']:
        if proof['row_refs'] not in result['row_refs'] or any(i not in edges for i in proof['fact_ids']):
            raise ValueError('Unresolvable context proof')


def _resolve_v1(state):
    """Validate table integrity and reconstruct the exact original JSON state."""
    if state.get('context_projection_version') != 1:
        raise ValueError('Unsupported context projection')
    tables = state['context_tables']
    snapshot = state['snapshot']
    for key, entry in tables.items():
        if set(entry) != {'kind', 'value'} or _key(snapshot, entry['kind'], entry['value']) != key:
            raise ValueError('Context table content mismatch')

    def expand(value, in_table=False):
        if isinstance(value, list):
            return [expand(item, in_table) for item in value]
        if isinstance(value, dict):
            if _V1_MARKER in value:
                if in_table or set(value) != {_V1_MARKER} or value[_V1_MARKER] not in tables:
                    raise ValueError('Unresolvable context table reference')
                return expand(tables[value[_V1_MARKER]]['value'], True)
            return {k: expand(v, in_table) for k, v in value.items()}
        return value

    raw = expand({k: v for k, v in state.items() if k not in ('context_projection_version', 'context_tables')})
    for evidence in raw['evidence'].values():
        if evidence['kind'] == 'graph':
            _validate_graph(evidence['result'])
    return raw


def _measure(value, tables=None, shapes=None, marker=_MARKER):
    """Compute logical expanded costs before allocating an expanded tree.

    Memoization stores costs, not a discount: every occurrence adds the full
    expanded node/byte count, so an exponentially expanding DAG is rejected.
    """
    memo = {}
    visiting = set()

    def walk(item, depth=0):
        if depth > MAX_DEPTH:
            raise ValueError('Context depth limit exceeded')
        if isinstance(item, dict) and tables is not None and marker in item:
            key = item[marker]
            if set(item) != {marker} or not isinstance(key, str) or key not in tables:
                raise ValueError('Unresolvable context table reference')
            if key in visiting:
                raise ValueError('Cyclic context reference')
            if key not in memo:
                visiting.add(key)
                memo[key] = walk(tables[key]['value'], depth + 1)
                visiting.remove(key)
            stats = memo[key]
            if depth + stats[2] > MAX_DEPTH:
                raise ValueError('Context depth limit exceeded')
            return stats
        if isinstance(item, dict) and shapes is not None and _OBJECT in item:
            shape = item[_OBJECT]
            if (set(item) != {_OBJECT} or not isinstance(shape, list) or len(shape) != 2 or
                    not isinstance(shape[0], str) or shape[0] not in shapes or
                    not isinstance(shape[1], list) or len(shape[1]) != len(shapes[shape[0]])):
                raise ValueError('Invalid context object shape')
            keys, values = shapes[shape[0]], shape[1]
            children = ((len(canonical(k).encode()) + 1, v) for k, v in zip(keys, values, strict=True))
            length = len(keys)
        elif isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError('Context object keys must be strings')
            children = ((len(canonical(k).encode()) + 1, v) for k, v in item.items())
            length = len(item)
        elif isinstance(item, list):
            children = ((0, v) for v in item)
            length = len(item)
        else:
            if item is not None and type(item) not in (str, int, float, bool):
                raise ValueError('Context must contain JSON values')
            if isinstance(item, float) and not isfinite(item):
                raise ValueError('Context must contain finite numbers')
            size = len(canonical(item).encode())
            if size > MAX_EXPANDED_BYTES:
                raise ValueError('Context expansion byte limit exceeded')
            return size, 1, 0
        size, nodes, height = 2 + max(0, length - 1), 1, 0
        for prefix, child in children:
            child_size, child_nodes, child_height = walk(child, depth + 1)
            size += prefix + child_size
            nodes += child_nodes
            height = max(height, child_height + 1)
            if size > MAX_EXPANDED_BYTES or nodes > MAX_EXPANDED_NODES:
                raise ValueError('Context expansion size limit exceeded')
        if height > MAX_DEPTH:
            raise ValueError('Context depth limit exceeded')
        return size, nodes, height

    if tables is not None:
        for key in tables:
            walk({marker: key})  # Also validate unreachable entries.
    return walk(value)


def _kind(value, field=None):
    if isinstance(value, dict):
        if 'ref' in value and 'id' in value:
            return 'meaning'
        if 'id' in value and 'payload' in value:
            return 'fact'
        return 'object'
    if isinstance(value, list):
        return 'array'
    return 'rules' if field == 'rules' else 'string'


def resolve(state):
    """Bounded v1/v2 reader; never allocates an unvalidated expanded DAG."""
    _measure(state)
    if not isinstance(state, dict) or type(state.get('context_projection_version')) is not int:
        raise ValueError('Invalid context projection schema')
    version = state['context_projection_version']
    if version not in (1, PROJECTION_VERSION):
        raise ValueError('Unsupported context projection')
    tables = state.get('context_tables')
    if not isinstance(tables, dict) or not isinstance(state.get('snapshot'), str):
        raise ValueError('Invalid context table schema')
    shapes = state.get('context_shapes') if version == 2 else None
    if version == 2:
        if not isinstance(shapes, dict):
            raise ValueError('Invalid context shapes schema')
        for key, keys in shapes.items():
            if (not isinstance(key, str) or not key or not isinstance(keys, list) or
                    any(not isinstance(k, str) or k in _RESERVED for k in keys) or len(set(keys)) != len(keys)):
                raise ValueError('Invalid context shape keys')
    for key, entry in tables.items():
        if (not isinstance(key, str) or not key or not isinstance(entry, dict) or
                set(entry) != {'kind', 'value'} or not isinstance(entry['kind'], str) or
                entry['kind'] not in _KINDS):
            raise ValueError('Invalid context table entry')
        value = entry['value']
        if not isinstance(value, (dict, list, str)) or (isinstance(value, dict) and _MARKER in value):
            raise ValueError('Invalid context table value')
        kind_value = value
        if isinstance(value, dict) and _OBJECT in value and shapes is not None:
            shape = value[_OBJECT]
            if not isinstance(shape, list) or len(shape) != 2 or not isinstance(shape[0], str) or shape[0] not in shapes:
                raise ValueError('Invalid context object shape')
            kind_value = dict.fromkeys(shapes[shape[0]])
        if entry['kind'] != _kind(kind_value, 'rules' if entry['kind'] == 'rules' else None):
            raise ValueError('Invalid context table kind')
    metadata = _META if version == 2 else {'context_projection_version', 'context_tables'}
    body = {k: v for k, v in state.items() if k not in metadata}
    _measure(body, tables, shapes, _V1_MARKER if version == 1 else _MARKER)
    if version == 1:
        return _resolve_v1(state)
    digest = state.get('context_raw_sha256')
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError('Invalid context raw digest')

    def expand(value):
        if isinstance(value, dict):
            if _MARKER in value:
                return expand(tables[value[_MARKER]]['value'])
            if _OBJECT in value:
                shape, values = value[_OBJECT]
                assert shapes is not None
                return {k: expand(v) for k, v in zip(shapes[shape], values, strict=True)}
            return {k: expand(v) for k, v in value.items()}
        if isinstance(value, list):
            return [expand(v) for v in value]
        return value

    raw = expand(body)
    if sha256(canonical(raw).encode()).hexdigest() != digest:
        raise ValueError('Context raw content mismatch')
    if not isinstance(raw.get('evidence'), dict) or not isinstance(raw.get('trace'), list):
        raise ValueError('Invalid context evidence schema')
    for evidence in raw['evidence'].values():
        if evidence['kind'] == 'graph':
            _validate_graph(evidence['result'])
    return raw


def project(state):
    """Losslessly share repeated nested values, retaining complete E/K records.

    Selection uses actual nested reference counts and serialized table overhead.
    A final complete serialization check prevents interacting pools from making
    the representation larger than its unshared v2 representation.
    """
    _measure(state)
    if not isinstance(state, dict) or _META.intersection(state):
        raise ValueError('Raw state already projected or invalid')
    raw_bytes = canonical(state).encode()
    digest = sha256(raw_bytes).hexdigest()
    shape_counts: dict[tuple[str, ...], int] = {}
    def count_shapes(value):
        if isinstance(value, dict):
            if _RESERVED.intersection(value):
                raise ValueError('Reserved context reference in raw state')
            keys = tuple(value)
            shape_counts[keys] = shape_counts.get(keys, 0) + 1
            for child in value.values():
                count_shapes(child)
        elif isinstance(value, list):
            for child in value:
                count_shapes(child)
    count_shapes(state)
    shape_ids = {keys: format(i, 'x') for i, keys in enumerate(sorted(
        keys for keys, count in shape_counts.items() if count > 1 and len(canonical(keys).encode()) > 100))}
    shapes = {key: list(keys) for keys, key in shape_ids.items()}

    def pack(value):
        if isinstance(value, dict):
            if tuple(value) in shape_ids:
                return {_OBJECT: [shape_ids[tuple(value)], [pack(v) for v in value.values()]]}
            return {k: pack(v) for k, v in value.items()}
        if isinstance(value, list):
            return [pack(v) for v in value]
        return value

    records: dict[str, Any] = {}
    counts: dict[str, int] = {}
    encodings: dict[str, str] = {}

    def identity(value, field=None):
        kind_value = dict.fromkeys(shapes[value[_OBJECT][0]]) if isinstance(value, dict) and _OBJECT in value else value
        kind = _kind(kind_value, field)
        encoded = json.dumps([state['snapshot'], kind, value], ensure_ascii=False, separators=(',', ':'))
        key = sha256(encoded.encode()).hexdigest()
        if key in encodings and encodings[key] != encoded:
            raise ValueError('Context content-key collision')
        encodings[key] = encoded
        records.setdefault(key, (kind, value))
        return key

    def eligible(value):
        return isinstance(value, (dict, list)) or isinstance(value, str) and len(value.encode()) >= 12

    def visit(value, field=None):
        if isinstance(value, dict):
            if _MARKER in value:
                raise ValueError('Reserved context reference in raw state')
            if _OBJECT in value:
                for v in value[_OBJECT][1]:
                    visit(v)
            else:
                for k, v in value.items():
                    visit(v, k)
        elif isinstance(value, list):
            for v in value:
                visit(v, field)
        if eligible(value):
            key = identity(value, field)
            counts[key] = counts.get(key, 0) + 1

    # Structural envelopes stay inline. Sources/wiki and search results retain
    # their existing model protocol; graph/catalog subtrees may use nested refs.
    def map_roots(transform):
        def copy(value):
            if isinstance(value, dict):
                if _MARKER in value:
                    raise ValueError('Reserved context reference in raw state')
                return {k: copy(v) for k, v in value.items()}
            if isinstance(value, list):
                return [copy(v) for v in value]
            return value
        result = copy(state)
        catalog = result.get('catalog', {})
        for field in ('entity_meanings', 'relation_meanings'):
            if field in catalog:
                catalog[field] = transform(pack(state['catalog'][field]), field)
        for eid, evidence in state['evidence'].items():
            if evidence['kind'] == 'graph':
                result['evidence'][eid]['result'] = {
                    k: transform(pack(v), k) for k, v in evidence['result'].items()}
        return result

    def collect(value, field):
        visit(value, field)
        return value
    map_roots(collect)
    selected = {key for key, count in counts.items() if count > 1}
    # Stable short IDs are prompt-local, not meaning/fact identity or hashes.
    ids = {key: format(i, 'x') for i, key in enumerate(sorted(selected))}

    def build(active):
        tables: dict[str, Any] = {}
        uses: dict[str, int] = {}

        def compact(value, field=None, defining=None):
            key = identity(value, field) if eligible(value) else None
            if key is not None and key in active and key != defining:
                uses[key] = uses.get(key, 0) + 1
                if ids[key] not in tables:
                    kind, original = records[key]
                    tables[ids[key]] = None
                    tables[ids[key]] = {'kind': kind, 'value': compact(original, field, key)}
                return {_MARKER: ids[key]}
            if isinstance(value, dict):
                if _OBJECT in value:
                    shape, values = value[_OBJECT]
                    return {_OBJECT: [shape, [compact(v) for v in values]]}
                return {k: compact(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [compact(v, field) for v in value]
            return value

        result = map_roots(compact)
        result.update(context_projection_version=PROJECTION_VERSION, context_tables=tables,
                      context_raw_sha256=digest, context_shapes=shapes)
        return result, uses

    projected, uses = build(selected)
    # Nested ancestor sharing reduces descendant occurrences. Recompute actual
    # uses after each removal; never count memoized subtrees as repeated output.
    while True:
        removable = []
        for key in selected:
            if key not in uses:
                removable.append(key)
                continue
            entry = projected['context_tables'][ids[key]]
            size = len(canonical(entry['value']).encode())
            ref_size = len(canonical({_MARKER: ids[key]}).encode())
            entry_size = len(canonical({ids[key]: entry}).encode()) - 1
            saving = uses[key] * (size - ref_size) - entry_size
            if saving <= 0:
                removable.append(key)
        if not removable:
            break
        # Deterministic single removals account for changed descendant costs.
        selected.remove(sorted(removable)[0])
        projected, uses = build(selected)
    # Shapes also pay for their declarations and every wrapper. Remove a shape
    # only when a complete serialization proves the alternative is smaller.
    def inline_shape(value, shape_id):
        if isinstance(value, dict):
            if _OBJECT in value and value[_OBJECT][0] == shape_id:
                values = value[_OBJECT][1]
                return {k: inline_shape(v, shape_id) for k, v in zip(shapes[shape_id], values, strict=True)}
            return {k: inline_shape(v, shape_id) for k, v in value.items()}
        if isinstance(value, list):
            return [inline_shape(v, shape_id) for v in value]
        return value
    for shape_id in sorted(shapes):
        alternative = inline_shape(projected, shape_id)
        del alternative['context_shapes'][shape_id]
        if len(canonical(alternative).encode()) <= len(canonical(projected).encode()):
            projected = alternative
    unshared = dict(state)
    unshared.update(context_projection_version=PROJECTION_VERSION, context_tables={}, context_raw_sha256=digest, context_shapes={})
    if len(canonical(projected).encode()) >= len(canonical(unshared).encode()):
        projected = unshared
    raw = resolve(projected)
    if canonical(raw).encode() != raw_bytes:
        raise ValueError('Context projection is not lossless')
    evidence_ids = frozenset(raw['evidence'])
    lookup_ids = frozenset(entry['result']['lookup_id'] for entry in raw['trace']
                           if entry.get('status') == 'success' and 'lookup_id' in entry.get('result', {}))
    return Projection(projected, evidence_ids, lookup_ids)
