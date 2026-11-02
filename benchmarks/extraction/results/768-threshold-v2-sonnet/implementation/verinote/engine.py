"""Real Wirelog evaluation with deterministic, source-backed witness paths."""
import threading
from collections import deque
from importlib.metadata import version
from pathlib import Path

from .store import digest

RULES = Path(__file__).with_name('rules.dl').read_text()
LOCK = threading.Lock()


def witness(facts, source, target, predicate):
    queue: deque[tuple[str, list[str]]] = deque([(source, [])])
    seen = {source}
    while queue:
        node, path = queue.popleft()
        for fact in facts:
            p = fact['payload']
            if p['subject'] != node or p['predicate'] != predicate:
                continue
            new_path = path + [fact['id']]
            if p['object'] == target:
                return new_path
            if p['object'] not in seen:
                seen.add(p['object'])
                queue.append((p['object'], new_path))
    raise RuntimeError('Wirelog 결과에 대응하는 근거 경로가 없습니다.')


def _legacy_query(snapshot, action='direct', subject='', predicate='', target=''):
    if action not in {'direct', 'downstream', 'upstream', 'path'}:
        raise ValueError('지원 질의: direct, downstream, upstream, path')
    for value in (subject, predicate, target):
        if not isinstance(value, str) or len(value) > 200:
            raise ValueError('질의 값은 200자 이하 문자열이어야 합니다.')
    if action != 'direct' and (not subject or not predicate):
        raise ValueError('전이 질의에는 기준 개체와 관계를 지정하세요.')
    if action == 'path' and not target:
        raise ValueError('경로 질의에는 도착 개체가 필요합니다.')
    from pyrewire import EasySession
    facts = sorted(snapshot['facts'], key=lambda f: (f['payload']['subject'], f['payload']['predicate'], f['payload']['object'], f['id']))
    # Native engine calls are serialized; parameter rows never become program text.
    with LOCK, EasySession(RULES) as session:
        for f in facts:
            p = f['payload']
            session.insert('edge', (p['subject'], p['predicate'], p['object']))
        rows = sorted(set(session.snapshot('direct' if action == 'direct' else 'reach')))
    selected = []
    for a, p, b in rows:
        if predicate and p != predicate:
            continue
        if action == 'upstream':
            include = b == subject and a != subject
        elif action == 'path':
            include = a == subject and b == target
        else:
            include = (not subject or a == subject) and (action == 'direct' or b != subject)
        if include:
            selected.append([a, p, b])
    proofs = []
    for a, p, b in selected:
        if action == 'direct':
            ids = [f['id'] for f in facts if f['payload']['subject'] == a and f['payload']['predicate'] == p and f['payload']['object'] == b]
        else:
            ids = witness(facts, a, b, p)
        proofs.append({'row': [a, p, b], 'fact_ids': ids})
    used = {i for proof in proofs for i in proof['fact_ids']}
    return {'status': 'success' if selected else 'empty', 'snapshot': snapshot['id'], 'engine': 'pyrewire', 'engine_version': version('pyrewire'),
            'rule_id': 'triple-v1', 'rule_sha256': digest(RULES.encode()), 'rules': RULES,
            'query': {'action': action, 'subject': subject, 'predicate': predicate, 'target': target},
            'rows': selected, 'proofs': proofs, 'facts': [f for f in facts if f['id'] in used],
            'scope': '전이는 선택한 관계의 연결 경로입니다. 인과관계나 현실의 사건 발생을 보장하지 않습니다.'}



def query(snapshot, action='direct', subject='', predicate='', target=''):
    if 'graph_identity_version' not in snapshot:
        return _legacy_query(snapshot, action, subject, predicate, target)
    from .entity_identity import CONTRACT, candidates, identity_graph
    if action not in {'direct', 'downstream', 'upstream', 'path'}:
        raise ValueError('지원 질의: direct, downstream, upstream, path')
    if any(not isinstance(v, str) or len(v) > 200 for v in (subject, predicate, target)):
        raise ValueError('질의 값은 200자 이하 문자열이어야 합니다.')
    if action != 'direct' and (not subject or not predicate):
        raise ValueError('전이 질의에는 기준 개체와 관계를 지정하세요.')
    if action == 'path' and not target: raise ValueError('경로 질의에는 도착 개체가 필요합니다.')
    if action == 'direct' and target: raise ValueError('직접 질의에는 target을 지정하지 마세요.')
    graph = identity_graph(snapshot)
    subjects = candidates(graph, subject) if subject else []
    targets = candidates(graph, target) if target else []
    predicates = candidates(graph, predicate, 'relation') if predicate else []
    # One selected meaning narrows bare relation labels to that same dictionary
    # scope; callers can still provide a relation ref to make the choice explicit.
    if subject and len(subjects) == 1 and predicate and not predicate.startswith('relation:'):
        predicates = [p for p in predicates if p['scope_ref'] == subjects[0]['scope_ref']]
    result = {'status': 'empty', 'snapshot': snapshot['id'], 'engine': 'pyrewire', 'engine_version': version('pyrewire'),
              'rule_id': 'triple-v1', 'rule_sha256': digest(RULES.encode()), 'rules': RULES,
              'query': {'action': action, 'subject': subject, 'predicate': predicate, 'target': target},
              'rows': [], 'row_refs': [], 'proofs': [], 'facts': [], 'fact_refs': [], 'identity_contract': CONTRACT,
              'entity_candidates': {'subject': subjects, 'target': targets}, 'predicate_candidates': predicates,
              'entities': list({r['ref']: r for r in subjects+targets}.values()), 'relations': predicates,
              'warnings': [], 'scope': '사전 고정 의미별 연결 경로입니다. 문서 간 동명 개체를 동일시하지 않으며 인과관계를 보장하지 않습니다.'}
    if action in ('downstream', 'path') and (len(subjects) > 1 or len(predicates) > 1 or action == 'path' and len(targets) > 1):
        result.update(status='ambiguous', warnings=['동일한 이름의 여러 의미가 있습니다. 개체·관계 참조를 선택하세요.'])
        return result
    facts = sorted(snapshot['facts'], key=lambda f: f['id']); original = {f['id']: f for f in facts}
    scoped = [{'id': edge['id'], 'payload': {'subject': edge['subject_ref'], 'predicate': edge['predicate_ref'],
                                          'object': edge['object_ref']}} for edge in graph['facts']]
    from pyrewire import EasySession
    with LOCK, EasySession(RULES) as session:
        for fact in scoped:
            p = fact['payload']; session.insert('edge', (p['subject'], p['predicate'], p['object']))
        rows = sorted(set(session.snapshot('direct' if action == 'direct' else 'reach')))
    subject_refs = {r['ref'] for r in subjects}; target_refs = {r['ref'] for r in targets}; predicate_refs = {r['ref'] for r in predicates}
    labels = {r['ref']: r['label'] for r in [*graph['entities'], *graph['relations']]}
    selected = []
    for a, p, b in rows:
        if predicate and p not in predicate_refs: continue
        if action == 'upstream': include = b in subject_refs and a != b
        elif action == 'path': include = a in subject_refs and b in target_refs
        else: include = (not subject or a in subject_refs) and (action == 'direct' or b != a) and (not target or b in target_refs)
        if include: selected.append((a, p, b))
    selected.sort(key=lambda r: (*[labels[v] for v in r], *r))
    used: set[str] = set()
    for a, p, b in selected:
        ids = ([f['id'] for f in scoped if f['payload'] == {'subject': a, 'predicate': p, 'object': b}]
               if action == 'direct' else witness(scoped, a, b, p))
        displayed = [labels[a], labels[p], labels[b]]; refs = [a, p, b]
        result['rows'].append(displayed); result['row_refs'].append(refs)
        result['proofs'].append({'row': displayed, 'row_refs': refs, 'fact_ids': ids}); used.update(ids)
    result['facts'] = [original[i] for i in sorted(used)]
    proof_edges = [f for f in graph['facts'] if f['id'] in used]
    result['fact_refs'] = proof_edges
    entity_refs = subject_refs | target_refs | {f[k] for f in proof_edges for k in ('subject_ref', 'object_ref')}
    relation_refs = predicate_refs | {f['predicate_ref'] for f in proof_edges}
    result['entities'] = [r for r in graph['entities'] if r['ref'] in entity_refs]
    result['relations'] = [r for r in graph['relations'] if r['ref'] in relation_refs]
    result['status'] = 'success' if selected else 'empty'
    if any(r['identity_status'] == 'legacy_unresolved' for r in graph['entities'] if r['ref'] in {v for row in selected for v in (row[0], row[2])}):
        result['warnings'].append('사전 고정 정보가 없는 사실은 문서·버전 내부의 미해결 의미로만 연결했습니다.')
    return result
