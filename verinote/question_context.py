"""Lossless, deterministic model context; persisted evidence is never rewritten."""
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from .store import canonical

PROJECTION_VERSION = 1
PROTOCOL = '''Model-context protocol v1: context_tables contains shared immutable records.
A {"$context_ref": key} stands for the complete value at context_tables[key].value;
resolve it wherever it occurs, including catalog meanings, graph facts and rules.
Keys bind the snapshot and complete content, not merely a fact or meaning ID.
E IDs retain their own query, status, rows, scoped refs and proof membership.
Shared dictionary guidance is not evidence; only E IDs may be cited. K IDs retain
lookup scope/completeness and are not citations. No omitted or inferred content.
'''
_MARKER = '$context_ref'


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


def resolve(state):
    """Validate table integrity and reconstruct the exact original JSON state."""
    if state.get('context_projection_version') != PROJECTION_VERSION:
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
            if _MARKER in value:
                if in_table or set(value) != {_MARKER} or value[_MARKER] not in tables:
                    raise ValueError('Unresolvable context table reference')
                return expand(tables[value[_MARKER]]['value'], True)
            return {k: expand(v, in_table) for k, v in value.items()}
        return value

    raw = expand({k: v for k, v in state.items() if k not in ('context_projection_version', 'context_tables')})
    for evidence in raw['evidence'].values():
        if evidence['kind'] == 'graph':
            _validate_graph(evidence['result'])
    return raw


def project(state):
    """Return a complete model projection plus exactly its delivered E/K IDs.

    Shared values are stored whole (no nested pointers). Sources, wiki slices,
    lookup offsets and truncation metadata stay inline and unchanged.
    """
    tables: dict[str, Any] = {}

    def shared(kind, value):
        key = _key(state['snapshot'], kind, value)
        entry = {'kind': kind, 'value': copy(value)}
        if key in tables and tables[key] != entry:
            raise ValueError('Context content-key collision')
        tables[key] = entry
        return {_MARKER: key}

    def copy(value):
        if isinstance(value, list):
            return [copy(v) for v in value]
        if isinstance(value, dict):
            if _MARKER in value:
                raise ValueError('Reserved context reference in raw state')
            return {k: copy(v) for k, v in value.items()}
        return value

    def compact(value, field=None):
        if isinstance(value, dict):
            if _MARKER in value:
                raise ValueError('Reserved context reference in raw state')
            if 'ref' in value and 'id' in value:
                return shared('meaning', value)
            if 'id' in value and 'payload' in value:
                return shared('fact', value)
            return {k: compact(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [compact(v, field) for v in value]
        if field == 'rules' and isinstance(value, str):
            return shared('rules', value)
        return value

    if 'context_tables' in state or 'context_projection_version' in state:
        raise ValueError('Raw state already projected')
    projected = compact(state)
    projected.update(context_projection_version=PROJECTION_VERSION, context_tables=tables)
    raw = resolve(projected)
    if raw != state:
        raise ValueError('Context projection is not lossless')
    evidence_ids = frozenset(raw['evidence'])
    lookup_ids = frozenset(entry['result']['lookup_id'] for entry in raw['trace']
                           if entry.get('status') == 'success' and 'lookup_id' in entry.get('result', {}))
    return Projection(projected, evidence_ids, lookup_ids)
