"""Fit complete evidence records without changing the persisted knowledge."""
import copy
from dataclasses import dataclass
from typing import Any

from .question_context import PROTOCOL, Projection, project
from .store import canonical, digest

WINDOW_INSTRUCTIONS = (
    '입력 예산에 맞춰 완전한 기록만 선택했습니다. 생략 목록은 근거가 아닙니다. '
    '현재 evidence의 E만 인용하세요. 필요하면 {"tools":[{"name":"recall_context",'
    '"arguments":{"id":"E1"}}]}로 생략된 E 또는 K 하나를 다시 요청하세요. '
    '재호출도 도구 한도를 소비하며 다음 회차에 완전한 내용이 전달되어야 사용할 수 있습니다. '
    'unavailable_requests는 이번 입력에 담을 수 없는 기록입니다. 더 좁게 조회하세요. '
    'answer_only이면 추가 도구 없이 현재 근거로 답하고 생략·미확인 범위는 unknowns에 적으세요.'
)


@dataclass(frozen=True)
class Window:
    prompt: str
    projection: Projection
    report: dict


class WindowTooLarge(ValueError):
    def __init__(self, size, limit):
        super().__init__(f'질문과 필수 근거를 입력 예산에 담을 수 없습니다: {size:,} / {limit:,} UTF-8 바이트. 질문 범위를 좁혀 주세요.')
        self.size, self.limit = size, limit


def fit(state, instructions, limit, *, requested=(), fresh=()):
    """Use real serialization for every candidate; never split a proof or quote."""
    def encode(value):
        projection = project(value)
        return PROTOCOL + instructions + canonical(projection.state), projection

    prompt, projection = encode(state)
    full_size = len(prompt.encode('utf-8'))
    report = {'full_prompt_bytes': full_size, 'selection': 'full',
              'model_token_admission': 'unknown', 'included_evidence': sorted(projection.evidence_ids),
              'included_lookups': sorted(projection.lookup_ids)}
    if full_size <= limit:
        return Window(prompt, projection, report)

    selected = copy.deepcopy(state)
    manifest: dict[str, Any] = {'instructions': WINDOW_INSTRUCTIONS, 'citable': False,
                'omitted_evidence': {}, 'omitted_lookups': {}, 'omitted_catalog': {},
                'omitted_trace_results': {}, 'unavailable_requests': []}
    selected['context_window'] = manifest
    requested, fresh = set(requested), set(fresh)
    lookups = {entry['result']['lookup_id']: i for i, entry in enumerate(state['trace'])
               if entry.get('status') == 'success' and 'lookup_id' in entry.get('result', {})}

    def candidate():
        nonlocal prompt, projection
        prompt, projection = encode(selected)
        return len(prompt.encode('utf-8')) <= limit

    def finished():
        report.update(selection='windowed', **{k: copy.deepcopy(v) for k, v in manifest.items()
                                               if k not in ('instructions', 'citable')},
                      included_evidence=sorted(projection.evidence_ids),
                      included_lookups=sorted(projection.lookup_ids))
        return Window(prompt, projection, report)

    # Guidance is not proof. Keep complete E/K records as long as possible.
    for field in ('entity_meanings', 'relation_meanings', 'entities', 'predicates'):
        if field in selected['catalog']:
            manifest['omitted_catalog'][field] = digest(selected['catalog'].pop(field))
            if candidate():
                return finished()

    # Old lookup bodies are independently recallable; descriptors are never K delivery.
    def priority(key):
        return (key in requested, key in fresh)

    for key in sorted(lookups, key=priority):
        if key in requested or (key in fresh and not state.get('answer_only')):
            continue
        index = lookups[key]
        body = selected['trace'][index].pop('result')
        manifest['omitted_lookups'][key] = {'sha256': digest(body)}
        if candidate():
            return finished()
    for index, entry in enumerate(selected['trace']):
        body = entry.get('result')
        if body is not None and 'lookup_id' not in body:
            manifest['omitted_trace_results'][str(index+1)] = digest(entry.pop('result'))
            if candidate():
                return finished()
    if selected['catalog'].get('documents'):
        manifest['omitted_catalog']['documents'] = digest(selected['catalog'].pop('documents'))
        if candidate():
            return finished()

    # Preserve at least one whole actual graph. A smaller graph can leave room
    # for complementary wiki evidence even if the newest graph is very large.
    graphs = [key for key, item in selected['evidence'].items() if item['kind'] == 'graph']
    mandatory = min(graphs, key=lambda key: (len(canonical(selected['evidence'][key]).encode()), key)) if graphs else None
    records = [('E', key) for key in selected['evidence'] if key != mandatory]
    records += [('K', key) for key, index in lookups.items() if 'result' in selected['trace'][index]]
    records.sort(key=lambda pair: (*priority(pair[1]), int(pair[1][1:])))
    for kind, key in records:
        if kind == 'E':
            body = selected['evidence'].pop(key)
            manifest['omitted_evidence'][key] = {'kind': body['kind'], 'sha256': digest(body)}
        else:
            body = selected['trace'][lookups[key]].pop('result')
            manifest['omitted_lookups'][key] = {'sha256': digest(body)}
        if key in requested:
            manifest['unavailable_requests'].append(key)
        if candidate():
            return finished()
    # Raw size is only a priority heuristic: repeated proof metadata can make
    # a larger graph serialize smaller. Try every complete graph before failing.
    current = mandatory
    for alternative in graphs:
        if alternative == current:
            continue
        body = selected['evidence'].pop(current)
        manifest['omitted_evidence'][current] = {'kind': body['kind'], 'sha256': digest(body)}
        if current in requested and current not in manifest['unavailable_requests']:
            manifest['unavailable_requests'].append(current)
        selected['evidence'][alternative] = copy.deepcopy(state['evidence'][alternative])
        manifest['omitted_evidence'].pop(alternative, None)
        if alternative in manifest['unavailable_requests']:
            manifest['unavailable_requests'].remove(alternative)
        current = alternative
        if candidate():
            return finished()
    raise WindowTooLarge(len(prompt.encode('utf-8')), limit)
