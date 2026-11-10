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


MAX_LEAF_RESPONSE_BYTES = 1024 * 1024
SYNTHESIS_VERSION = 3


def partition_response(output, allowance):
    """Whole, already validated records only; never trim text or unknowns."""
    batches: list[dict] = []
    current: list[dict] = []
    for record in output['records']:
        if len(canonical({'records': [record]}).encode()) > allowance:
            raise ValueError('단일 분할 분석 기록이 바이트 예산을 초과했습니다.')
        if current and len(canonical({'records': [*current, record]}).encode()) > allowance:
            batches.append({'records': current}); current = []
        current.append(copy.deepcopy(record))
    if current:
        batches.append({'records': current})
    return batches


def run(assistant, state, instructions, round_number):
    limit = assistant.context_limit()[0]
    prepared = plan(state, instructions, limit)
    identity = [prepared.base, assistant.job['agent'], assistant.job['model'],
                'complete-context-v3', prepared.allowance, MAX_LEAF_RESPONSE_BYTES]
    # Every input unit can become at most one bounded local leaf. Local leaves are not calls.
    bound = len(prepared.batches) + len(prepared.units)
    assistant.preflight_calls(bound)
    nodes = assistant.job.setdefault('analysis_nodes', {})
    responses = assistant.job.setdefault('analysis_leaf_responses', {})
    manifest = {u['id']: {'category': u['category'], 'sha256': u['sha256']} for u in prepared.units}
    originals = {u['id']: {'category': u['category'], 'sha256': u['sha256']} for u in units_for(state)}
    fragments = {u['id']: u for u in prepared.units if u['category'] == 'fragment'}
    sources = {u['id']: u['value']['source_id'] if u['category'] == 'fragment' else u['id'] for u in prepared.units}
    tree = {'round': round_number, 'status': 'running', 'manifest': manifest,
            'partition_version': 1, 'synthesis_version': SYNTHESIS_VERSION,
            'originals': originals, 'fragments': fragments, 'leaf_partitions': {},
            'planned_records': len(manifest), 'processed_records': 0, 'planned_batches': len(prepared.batches),
            'processed_batches': 0, 'max_physical_calls': bound,
            'output_byte_limit': prepared.allowance, 'root': None, 'nodes': []}
    assistant.job.setdefault('context_processing', []).append(tree)
    counter = 0
    def register(stage, key, children=()):
        nonlocal counter
        counter += 1
        path = f'context-{round_number}-{counter}'
        assistant.preflight_calls(1)
        assistant.register_call(path, stage, round_number, node=key)
        status = {'id': key, 'stage': stage, 'children': list(children), 'status': 'planned', 'path': path}
        tree['nodes'].append(status)
        return status

    def save_node(key, stage, analysis, support, coverage, children=(), **extra):
        nodes[key] = {'id': key, 'stage': stage, 'children': list(children), 'coverage': coverage,
                      'records': {sources[k]: originals[sources[k]] for k in coverage},
                      'analysis': copy.deepcopy(analysis), 'analysis_sha256': digest(analysis),
                      'supported_evidence': support, 'status': 'complete', **extra}

    leaves = []
    for batch in prepared.batches:
        key = 'A' + digest([identity, 'analysis_leaf', batch])
        leaves.append((key, batch, register('analysis_leaf', key)))
    assistant.progress(assistant.trace, assistant.evidence)
    try:
        level = []
        for key, batch, status in leaves:
            assigned = {'stage': 'analysis_leaf', 'units': batch}
            def accept(output, task=assigned):
                validate(output, task, MAX_LEAF_RESPONSE_BYTES, nodes)
                return partition_response(output, prepared.allowance)
            if key in responses:
                source = responses[key]
                if digest(source['analysis']) != source['analysis_sha256']:
                    raise ValueError('분석 원본 응답 무결성 오류')
                receipt(source['assigned_ids'], [u['id'] for u in batch])
                output = source['analysis']; pieces = accept(output)
                status['status'] = 'cached'; assistant.call_record(status['path'])['status'] = 'cached'
            else:
                output, pieces = assistant.invoke(leaf_prompt(prepared.base, batch, prepared.allowance),
                                                  status['path'], round_number, accept, mirror=status)
                responses[key] = {'analysis': copy.deepcopy(output), 'analysis_sha256': digest(output),
                                  'assigned_ids': [u['id'] for u in batch], 'path': status['path']}
            unit_map = {u['id']: u for u in batch}
            parts = []
            for i, piece in enumerate(pieces):
                part_key = key if len(pieces) == 1 else 'A' + digest([identity, key, i, piece])
                coverage = [r['id'] for r in piece['records']]
                support = validate(piece, {'stage':'analysis_leaf', 'units':[unit_map[k] for k in coverage]},
                                   prepared.allowance, nodes)
                save_node(part_key, 'analysis_leaf', piece, support, coverage, source_response=key)
                parts.append(part_key)
            tree['leaf_partitions'][key] = parts
            level.extend(parts)
            tree['processed_records'] += len(batch); tree['processed_batches'] += 1
            assistant.progress(assistant.trace, assistant.evidence)
        tree['bounded_leaves'] = len(level)
        while len(level) > 1:
            following = []
            for i in range(0, len(level), 2):
                children = level[i:i+2]
                if len(children) == 1:
                    following.extend(children); continue
                key = 'A' + digest([identity, 'analysis_merge', children])
                status = register('analysis_merge', key, children)
                assistant.progress(assistant.trace, assistant.evidence)
                assigned = {'stage':'analysis_merge', 'children':children}
                def accept_merge(output, task=assigned):
                    return validate(output, task, prepared.allowance, nodes)
                if key in nodes:
                    node = nodes[key]
                    if digest(node['analysis']) != node['analysis_sha256']:
                        raise ValueError('분석 기록 무결성 오류')
                    output = node['analysis']; support = accept_merge(output)
                    status['status'] = 'cached'; assistant.call_record(status['path'])['status'] = 'cached'
                else:
                    output, support = assistant.invoke(merge_prompt(prepared.base, [public_node(nodes[c]) for c in children], prepared.allowance),
                                                       status['path'], round_number, accept_merge, mirror=status)
                coverage = [k for c in children for k in nodes[c]['coverage']]
                save_node(key, 'analysis_merge', output, support, coverage, children)
                following.append(key)
                assistant.progress(assistant.trace, assistant.evidence)
            level = following
        root = level[0]; tree['root'] = root
        receipt(nodes[root]['coverage'], manifest)
        tree['status'] = 'analyzed'
        decision_path = f'context-{round_number}-decision'
        assistant.preflight_calls(1)
        assistant.register_call(decision_path, 'question_synthesis', round_number, node=root)
        assistant.progress(assistant.trace, assistant.evidence)
        prompt = instructions + canonical(decision_state(prepared.base, public_node(nodes[root])))
        output, _ = assistant.invoke(prompt, decision_path, round_number)
        assistant.delivered_evidence = set(); assistant.delivered_lookups = set(); assistant.delivered_analysis = {root}
        tree['status'] = 'complete'
        assistant.progress(assistant.trace, assistant.evidence)
        return output, decision_path
    except Exception:
        tree['status'] = 'failed'
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
        if tree.get('synthesis_version') == SYNTHESIS_VERSION:
            responses = assistant.job.get('analysis_leaf_responses', {})
            assigned_all = []
            partition_keys: set[str] = set()
            for source_id, parts in tree['leaf_partitions'].items():
                source = responses[source_id]
                if digest(source['analysis']) != source['analysis_sha256']:
                    raise ValueError('분석 원본 응답 무결성 오류')
                units = []
                for unit_id in source['assigned_ids']:
                    meta = tree['manifest'][unit_id]
                    category = meta['category']
                    value = (tree['fragments'][unit_id]['value'] if category == 'fragment'
                             else assistant.evidence[unit_id] if category == 'evidence' else {})
                    if category == 'evidence' and digest(value) != meta['sha256']:
                        raise ValueError('분석 원본 근거 무결성 오류')
                    units.append({'id': unit_id, 'category': category, 'value': value})
                validate(source['analysis'], {'stage':'analysis_leaf', 'units':units}, MAX_LEAF_RESPONSE_BYTES, nodes)
                expected = partition_response(source['analysis'], tree['output_byte_limit'])
                if len(parts) != len(expected) or len(set(parts)) != len(parts) or partition_keys.intersection(parts):
                    raise ValueError('분석 응답 분할 범위 무결성 오류')
                partition_keys.update(parts); assigned_all.extend(source['assigned_ids'])
                unit_map = {u['id']: u for u in units}
                for part_id, analysis in zip(parts, expected, strict=True):
                    node = nodes[part_id]
                    coverage = [r['id'] for r in analysis['records']]
                    supported = validate(analysis, {'stage':'analysis_leaf', 'units':[unit_map[k] for k in coverage]},
                                         tree['output_byte_limit'], nodes)
                    if (node.get('source_response') != source_id or node['analysis'] != analysis
                            or node['coverage'] != coverage or node['children'] or node['supported_evidence'] != supported):
                        raise ValueError('분석 응답 조각 무결성 오류')
            receipt(assigned_all, tree['manifest'])
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
            if tree is not None and tree.get('synthesis_version') == SYNTHESIS_VERSION:
                supported = validate(node['analysis'], {'stage':'analysis_merge', 'children':node['children']},
                                     tree['output_byte_limit'], nodes)
                if node['supported_evidence'] != supported:
                    raise ValueError('종합 분석 인용 무결성 오류')
        for evidence_id in node['supported_evidence']:
            original = assistant.evidence.get(evidence_id)
            if original is None or not usable(original) or digest(original) != node['records'][evidence_id]['sha256']:
                raise ValueError('분석 원본 근거 무결성 오류')
    walk(key)
    if tree is not None and tree.get('synthesis_version') == SYNTHESIS_VERSION:
        if {k for k in visited if not nodes[k]['children']} != partition_keys:
            raise ValueError('분석 응답 조각 연결 무결성 오류')
    return nodes[key]['supported_evidence']
