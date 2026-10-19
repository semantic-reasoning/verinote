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


def query(snapshot, action='direct', subject='', predicate='', target=''):
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
