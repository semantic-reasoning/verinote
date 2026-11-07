"""Mandatory whole-input analysis with verifiable processing lineage."""
import copy

from .question_batches import decision_state, leaf_prompt, merge_prompt, plan, units_for
from .question_fragments import reconstruct, usable
from .store import canonical, digest


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def receipt(actual, expected):
    if not strings(actual) or len(actual) != len(set(actual)) or set(actual) != set(expected):
        raise ValueError('분할 분석의 처리 범위가 배정 기록과 일치하지 않습니다.')


def validate(output, task, allowance, nodes):
    if not isinstance(output, dict) or len(canonical(output).encode()) > allowance:
        raise ValueError('분할 분석 응답 형식/바이트 예산 초과')
    def finding(value, allowed):
        if (not isinstance(value.get('text'), str) or not value['text'].strip()
                or not strings(value.get('unknowns')) or not strings(value.get('citations'))
                or not set(value['citations']) <= allowed):
            raise ValueError('분할 분석의 내용/인용 오류')
        return set(value['citations'])
    support: set[str] = set()
    if task['stage'] == 'analysis_leaf':
        if set(output) != {'records'} or not isinstance(output['records'], list):
            raise ValueError('분할 분석 records 형식 오류')
        values = output['records']
        if any(not isinstance(v, dict) or set(v) != {'id', 'text', 'citations', 'unknowns'} for v in values):
            raise ValueError('분할 분석 기록 형식 오류')
        receipt([v['id'] for v in values], [u['id'] for u in task['units']])
        units = {u['id']: u for u in task['units']}
        for value in values:
            u = units[value['id']]
            fragment = u['value'] if u['category'] == 'fragment' else None
            citable = (fragment['source_category'] == 'evidence' and fragment['source_usable']
                       if fragment else u['category'] == 'evidence' and usable(u['value']))
            cited = finding(value, {u['id']} if citable else set())
            support.update({fragment['source_id']} if cited and fragment else cited)
    else:
        if set(output) != {'children', 'text', 'citations', 'unknowns'}:
            raise ValueError('종합 분석 응답 형식 오류')
        receipt(output['children'], task['children'])
        allowed = {e for key in task['children'] for e in nodes[key]['supported_evidence']}
        support = finding(output, allowed)
    return sorted(support)


def public_node(node):
    return {k: node[k] for k in ('id', 'analysis', 'supported_evidence')}


def run(assistant, state, instructions, round_number):
    limit = assistant.context_limit()[0]
    prepared = plan(state, instructions, limit)
    identity = [prepared.base, assistant.job['agent'], assistant.job['model'], 'complete-context-v2']
    tasks = []
    def task(stage, units=(), children=()):
        key = 'A' + digest([identity, stage, units, children])
        result = {'id': key, 'stage': stage, 'units': list(units), 'children': list(children), 'status': 'planned'}
        tasks.append(result)
        return key
    level = [task('analysis_leaf', batch) for batch in prepared.batches]
    while len(level) > 1:
        following = []
        for i in range(0, len(level), 2):
            following.append(task('analysis_merge', children=level[i:i+2]) if i+1 < len(level) else level[i])
        level = following
    root = level[0]
    if len(tasks)+1 > 2*len(prepared.batches):
        raise ValueError('분할 처리 호출 계획 오류')
    assistant.preflight_calls(len(tasks)+1)
    nodes = assistant.job.setdefault('analysis_nodes', {})
    manifest = {u['id']: {'category': u['category'], 'sha256': u['sha256']} for u in prepared.units}
    originals = {u['id']: {'category': u['category'], 'sha256': u['sha256']} for u in units_for(state)}
    fragments = {u['id']: u for u in prepared.units if u['category'] == 'fragment'}
    sources = {u['id']: u['value']['source_id'] if u['category'] == 'fragment' else u['id'] for u in prepared.units}
    tree = {'round': round_number, 'status': 'running', 'manifest': manifest,
            'partition_version': 1, 'originals': originals, 'fragments': fragments,
            'planned_records': len(manifest), 'processed_records': 0, 'planned_batches': len(prepared.batches),
            'processed_batches': 0, 'max_physical_calls': 2*len(prepared.batches),
            'output_byte_limit': prepared.allowance, 'root': root, 'nodes': []}
    assistant.job.setdefault('context_processing', []).append(tree)
    # Save the entire call plan before dispatching its first physical call.
    for i, item in enumerate(tasks, 1):
        item['path'] = f'context-{round_number}-{i}'
        tree['nodes'].append({k: item[k] for k in ('id', 'stage', 'children', 'status', 'path')})
        assistant.register_call(item['path'], item['stage'], round_number, node=item['id'])
    decision_path = f'context-{round_number}-decision'
    assistant.register_call(decision_path, 'question_synthesis', round_number, node=root)
    assistant.progress(assistant.trace, assistant.evidence)
    try:
        for item, status in zip(tasks, tree['nodes'], strict=True):
            if item['id'] in nodes:
                status['status'] = 'cached'
                assistant.call_record(item['path'])['status'] = 'cached'
                node = nodes[item['id']]
            else:
                status['status'] = 'dispatched'
                if item['stage'] == 'analysis_leaf':
                    prompt = leaf_prompt(prepared.base, item['units'], prepared.allowance)
                    coverage = [u['id'] for u in item['units']]
                else:
                    prompt = merge_prompt(prepared.base, [public_node(nodes[c]) for c in item['children']], prepared.allowance)
                    coverage = [key for c in item['children'] for key in nodes[c]['coverage']]
                def accept(output, assigned=item):
                    return validate(output, assigned, prepared.allowance, nodes)
                output, support = assistant.invoke(prompt, item['path'], round_number, accept)
                node = {'id': item['id'], 'stage': item['stage'], 'children': item['children'],
                        'coverage': coverage, 'records': {sources[key]: originals[sources[key]] for key in coverage},
                        'analysis': copy.deepcopy(output), 'analysis_sha256': digest(output),
                        'supported_evidence': support, 'status': 'complete'}
                nodes[item['id']] = node
                status['status'] = 'complete'
            if item['stage'] == 'analysis_leaf':
                tree['processed_records'] += len(item['units'])
                tree['processed_batches'] += 1
            assistant.progress(assistant.trace, assistant.evidence)
        receipt(nodes[root]['coverage'], manifest)
        tree['status'] = 'analyzed'
        prompt = instructions + canonical(decision_state(prepared.base, public_node(nodes[root])))
        output, _ = assistant.invoke(prompt, decision_path, round_number)
        assistant.delivered_evidence = set()
        assistant.delivered_lookups = set()
        assistant.delivered_analysis = {root}
        tree['status'] = 'complete'
        assistant.progress(assistant.trace, assistant.evidence)
        return output, decision_path
    except Exception:
        tree['status'] = 'failed'
        for status in tree['nodes']:
            if status['status'] == 'dispatched':
                status['status'] = 'failed'
        assistant.progress(assistant.trace, assistant.evidence)
        raise


def supports(assistant, key):
    """Recheck server-owned ancestry, exact hashes and current original evidence."""
    nodes = assistant.job.get('analysis_nodes', {})
    tree = next((t for t in assistant.job.get('context_processing', []) if t['root'] == key), None)
    if tree is not None and tree.get('partition_version') == 1:
        if tree['status'] != 'complete':
            raise ValueError('분할 분석이 완료되지 않았습니다.')
        receipt(nodes[key]['coverage'], tree['manifest'])
        fragments = tree['fragments']
        if set(fragments) != {i for i, r in tree['manifest'].items() if r['category'] == 'fragment'}:
            raise ValueError('분할 기록 범위 무결성 오류')
        grouped: dict[str, list[dict]] = {}
        for fragment_id, fragment in fragments.items():
            if fragment['id'] != fragment_id or fragment['sha256'] != tree['manifest'][fragment_id]['sha256']:
                raise ValueError('분할 기록 해시 무결성 오류')
            grouped.setdefault(fragment['value']['source_id'], []).append(fragment)
        for source_id, group in grouped.items():
            original = tree['originals'].get(source_id)
            restored = reconstruct(sorted(group, key=lambda u: u['value']['index']))
            if (original is None or digest(restored) != original['sha256']
                    or group[0]['value']['source_category'] != original['category']):
                raise ValueError('분할 원본 무결성 오류')
            if original['category'] == 'evidence' and (source_id not in assistant.evidence
                    or digest(assistant.evidence[source_id]) != original['sha256']
                    or group[0]['value']['source_usable'] != usable(assistant.evidence[source_id])):
                raise ValueError('분석 원본 근거 무결성 오류')
    visited = set()
    def walk(node_id):
        if node_id in visited:
            return
        visited.add(node_id)
        node = nodes[node_id]
        if node['status'] != 'complete' or digest(node['analysis']) != node['analysis_sha256']:
            raise ValueError('분석 기록 무결성 오류')
        for child in node['children']:
            walk(child)
        if node['children']:
            receipt(node['coverage'], [item for child in node['children'] for item in nodes[child]['coverage']])
        for evidence_id in node['supported_evidence']:
            original = assistant.evidence.get(evidence_id)
            if original is None or not usable(original) or digest(original) != node['records'][evidence_id]['sha256']:
                raise ValueError('분석 원본 근거 무결성 오류')
    walk(key)
    return nodes[key]['supported_evidence']
