"""Bounded lexical guidance from a frozen graph; never relation evidence."""
import re
import unicodedata
from typing import Any

from .entity_identity import identity_graph
from .store import canonical

MAX_MEANINGS = 20
MAX_GUIDANCE_BYTES = 10000


def mentions(term, question):
    """Recognize names and Korean particles; this is navigation, not intent."""
    def norm(s): return unicodedata.normalize('NFC', s).casefold()
    term, question = norm(term), norm(question)
    if not term: return False
    particles = r'(?:은|는|을|를|이|가|과|와|도|만|의|에|에서|에게|으로|로|처럼|보다|까지|부터)'
    return re.search(r'(?<!\w)'+re.escape(term)+r'(?:'+particles+r')?(?!\w)', question) is not None


def compact(row, graph, facts):
    keys = ('ref', 'id', 'name', 'type', 'document_id', 'document_title', 'version', 'ontology_id',
            'ontology_revision', 'approval', 'current_approved', 'historical_pin', 'identity_status', 'fact_count')
    nodes = {n['ref']: n for n in graph['entities']}
    related = [e for e in graph['facts'] if row['ref'] in (e['subject_ref'], e['object_ref'])]
    items = []
    for edge in related[:3]:
        fact = facts[edge['id']]
        subject, obj = nodes[edge['subject_ref']], nodes[edge['object_ref']]
        items.append({k: fact[k] for k in ('id', 'revision', 'document_id', 'version')} |
                     {'subject': subject['name'], 'subject_type': subject['type'], 'predicate': fact['payload']['predicate'],
                      'object': obj['name'], 'object_type': obj['type'], 'quote_excerpt': fact['payload']['quote'][:160],
                      'quote_truncated': len(fact['payload']['quote']) > 160})
    return {k: row[k] for k in keys} | {'quote_excerpt': row['quote'][:200],
                                     'quote_truncated': len(row['quote']) > 200,
                                     'approved_facts': items, 'facts_truncated': len(related) > len(items)}


def context(snapshot, question):
    """No LLM or live-store access; immutable, bounded, non-citable guidance."""
    if 'graph_identity_version' not in snapshot:
        return {'contract': 'legacy', 'groups': [], 'relations': [], 'truncated': False,
                'notice': '이전 스냅샷은 문자열 개체를 사용하므로 동명 의미를 구분하지 못합니다.', 'citable': False}
    graph = identity_graph(snapshot)
    facts = {f['id']: f for f in snapshot['facts']}
    terms: dict[str, dict[str, Any]] = {}
    for row in graph['entities']:
        for term in {row['id'], row['name'], *row['aliases']}:
            if mentions(term, question): terms.setdefault(term, {})[row['ref']] = row
    result: dict[str, Any] = {'contract': 'scoped-ontology-v1', 'groups': [], 'relations': [], 'truncated': False,
              'notice': '이름 일치로 찾은 의미 후보입니다. 질문의 의도를 확정하지 않으며 사전 안내는 관계 사실의 근거가 아닙니다.', 'citable': False}
    count = 0; docs = set()
    for term, entries in sorted(terms.items()):
        group: dict[str, Any] = {'term': term, 'total_candidates': len(entries), 'ambiguous': len(entries) > 1,
                 'candidates': [], 'truncated': False}
        result['groups'].append(group)
        for row in sorted(entries.values(), key=lambda r: (r['type'], r['document_id'], r['ref'])):
            candidate = compact(row, graph, facts); group['candidates'].append(candidate)
            if count >= MAX_MEANINGS or len(canonical(result).encode()) > MAX_GUIDANCE_BYTES - 1000:
                group['candidates'].pop(); group['truncated'] = result['truncated'] = True; break
            count += 1; docs.add(row['document_id'])
        if result['truncated']: break
    # Actual approved relation IDs prevent translating identifiers by intuition.
    for row in graph['relations']:
        if row['document_id'] not in docs and not mentions(row['id'], question): continue
        relation = {k: row[k] for k in ('ref', 'id', 'document_id', 'scope_ref', 'approval')}
        relation['description'] = row['description'][:200]
        result['relations'].append(relation)
        if len(result['relations']) > MAX_MEANINGS or len(canonical(result).encode()) > MAX_GUIDANCE_BYTES - 500:
            result['relations'].pop(); result['truncated'] = True; break
    return result
