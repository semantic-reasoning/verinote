"""Compact, resumable dictionary generation; checkpoints are never approvals."""
import json

from . import dictionary_reconciliation as reconciliation
from .call_validation import caller_validation
from .dictionary_exclusions import (
    add_chunk,
    identity,
    new_report,
    public_report,
    validate_report,
    write_report,
)
from .dictionary_filter import (
    FILTER_POLICY,
    SUPPORTED_FILTER_POLICIES,
    reconcile_generated_dictionary,
)
from .dictionary_type_warnings import (
    DEFAULT_THRESHOLD,
    POLICY,
    type_warnings,
    validate_threshold,
)
from .documents import bounded_spans
from .ontology import (
    DICTIONARY_CHUNK_SIZE,
    DICTIONARY_PROMPT,
    LIMITS,
    MAX_DICTIONARY_BYTES,
    MAX_DICTIONARY_CALLS,
    MERGE_CONTRACT_VERSION,
    SOURCE_SPAN_MIN_SIZE,
    SOURCE_SPAN_SIZE,
    bounded_prompt,
    dictionary_context,
    source_spans,
    validate_dictionary,
)
from .store import canonical, digest
from .usage import atomic_json, safe_file

PIPELINE = 'source-spans-v1'
CHECKPOINT_LIMIT = MAX_DICTIONARY_BYTES + 6 * 1024 * 1024
# The immediate predecessor differs only in describing per-chunk counts as
# document totals. Its validated prefixes remain valid under the larger totals.
PREVIOUS_COUNT_LIMIT_PROMPT = '566ed32738c94c60f2fa2c059683260c9859356c671b60bc73ccd2cf9ba5018f'
PREVIOUS_IMMUTABLE_PROMPT = '0e2a3892fe24a0ca48994dfa347ee40c9dc9a06b14ed76e53a43002cdfccba63'


def generate_dictionary(store, cli, root, run, doc, *, relation_type_warning_threshold=DEFAULT_THRESHOLD):
    threshold = validate_threshold(relation_type_warning_threshold)
    source = doc['text']
    slices = list(bounded_spans(source, DICTIONARY_CHUNK_SIZE))
    if len(slices) > MAX_DICTIONARY_CALLS:
        raise ValueError('사전 생성은 최대 64개 구간까지 지원합니다.')
    boundaries = {offset + len(chunk) for offset, chunk in slices}
    contract = {'pipeline': PIPELINE, 'prompt': digest(DICTIONARY_PROMPT.encode()), 'merge_contract': MERGE_CONTRACT_VERSION,
                'chunk_size': DICTIONARY_CHUNK_SIZE, 'span_size': SOURCE_SPAN_SIZE, 'span_min_size': SOURCE_SPAN_MIN_SIZE,
                'document_id': run['document_id'], 'version': run['version'],
                'source_sha256': doc['sha256'], 'text_sha256': digest(source.encode()),
                'agent': run['agent'], 'model': run['model']}
    contract['filter_policy'] = FILTER_POLICY
    contract['reconciliation_policy'] = reconciliation.POLICY
    contract.update(type_warning_policy=POLICY, relation_type_warning_threshold=threshold)
    if run['agent'] == 'openrouter':
        from .transports import job_options
        contract['execution_options'] = job_options(run.get('execution_options'))
    if run.get('ontology'):
        contract['regeneration_target'] = run['ontology']
    folder = root / 'runs' / run['id']
    folder.mkdir(parents=True, exist_ok=True)
    dictionary: dict[str, list[object]] = {kind: [] for kind in LIMITS}
    exclusions = new_report(run, doc)
    reconciliations = reconciliation.new_report(run, doc)
    accepted, resumed_from, warnings = 0, None, []
    skipped = 0
    option_mismatches = 0
    for prior in store.dictionary_retries(run):
        raw = safe_file(root, f'runs/{prior["id"]}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        try:
            value = json.loads(raw) if raw else None
            fields = {'schema_version', 'run_id', 'contract', 'offset', 'dictionary', 'resumed_from'}
            if isinstance(value, dict) and value.get('schema_version') in (2, 3):
                fields.add('exclusions')
            if isinstance(value, dict) and value.get('schema_version') == 3:
                fields.add('reconciliations')
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError('checkpoint shape')
            if run['agent'] == 'openrouter' and isinstance(value['contract'], dict) and value['contract'].get('execution_options') != contract['execution_options']:
                option_mismatches += 1
                raise ValueError('checkpoint HTTP options mismatch')
            offset = value['offset']
            expected = dict(contract)
            if value['schema_version'] in (1, 2):
                expected.pop('reconciliation_policy')
                if isinstance(value['contract'], dict) and value['contract'].get('prompt') in (PREVIOUS_COUNT_LIMIT_PROMPT, PREVIOUS_IMMUTABLE_PROMPT):
                    expected['prompt'] = value['contract']['prompt']
            if (isinstance(value['contract'], dict) and 'type_warning_policy' not in value['contract']
                    and 'relation_type_warning_threshold' not in value['contract'] and threshold == DEFAULT_THRESHOLD):
                expected.pop('type_warning_policy')
                expected.pop('relation_type_warning_threshold')
            if (value['schema_version'] in (1, 2) and isinstance(value['contract'], dict) and type(value['contract'].get('filter_policy')) is int
                    and value['contract']['filter_policy'] in SUPPORTED_FILTER_POLICIES):
                expected['filter_policy'] = value['contract']['filter_policy']  # Revalidate known older prefixes.
            if value['schema_version'] == 1:
                expected.pop('filter_policy')  # Strict legacy prefixes excluded no rows.
            if type(value['schema_version']) is not int or value['schema_version'] not in (1, 2, 3) or value['run_id'] != prior['id'] or canonical(value['contract']) != canonical(expected) or type(offset) is not int or offset not in boundaries:
                raise ValueError('checkpoint identity/boundary')
            candidate_dictionary = validate_dictionary(value['dictionary'], source[:offset])
            if value['schema_version'] in (2, 3):
                candidate_exclusions = validate_report(value['exclusions'], prior, doc, offset, filter_policy=value['contract']['filter_policy'])
                candidate_exclusions['identity'] = identity(run, doc)
            else:
                candidate_exclusions = new_report(run, doc)
                for start, chunk in slices:
                    if start >= offset:
                        break
                    candidate_exclusions = add_chunk(candidate_exclusions, start, len(chunk), [])
            if value['schema_version'] == 3:
                candidate_reconciliations = reconciliation.validate_report(value['reconciliations'], prior, doc, offset)
                candidate_reconciliations['identity'] = reconciliation.identity(run, doc)
            else:
                candidate_reconciliations = reconciliation.new_report(run, doc)
                for start, chunk in slices:
                    if start >= offset: break
                    candidate_reconciliations = reconciliation.add_chunk(candidate_reconciliations, start, len(chunk), [])
                candidate_reconciliations['legacy_chars'] = offset
            dictionary, exclusions, reconciliations = candidate_dictionary, candidate_exclusions, candidate_reconciliations
            accepted, resumed_from = offset, prior['id']
            break
        except (ValueError, TypeError, KeyError, UnicodeError):
            skipped += 1
    if option_mismatches:
        warnings.append(f'OpenRouter 고정 실행 옵션이 다른 복구 기록 {option_mismatches}개 제외')
    if skipped:
        warnings.append(f'이전 복구 기록 {skipped}개가 없거나 호환·검증 조건을 충족하지 않아 제외')
    state = {'schema_version': 1, 'pipeline': PIPELINE, 'run_id': run['id'],
             'calls_started': 0, 'resumed_from': resumed_from, 'resumed_chars': accepted,
             'completed_chars': accepted, 'total_chars': len(source), 'status': 'running'}

    def write(path, value):
        try:
            atomic_json(path, value)
            return True
        except OSError:
            warning = '진행 기록 저장 실패: 재시도 시 일부 구간을 다시 처리할 수 있습니다.'
            if warning not in warnings:
                warnings.append(warning)
            return False

    def checkpoint(candidate, candidate_exclusions, candidate_reconciliations, offset):
        if not offset:
            return
        validate_report(candidate_exclusions, run, doc, offset)
        reconciliation.validate_report(candidate_reconciliations, run, doc, offset)
        value = {'schema_version': 3, 'run_id': run['id'], 'contract': contract, 'offset': offset,
                 'dictionary': candidate, 'resumed_from': resumed_from, 'exclusions': candidate_exclusions,
                 'reconciliations': candidate_reconciliations}
        if len(json.dumps(value, ensure_ascii=False).encode()) > CHECKPOINT_LIMIT:
            raise ValueError('사전 조정·복구 기록 크기 제한 초과')
        try:
            atomic_json(folder / 'dictionary-checkpoint.json', value)
        except OSError as exc:
            raise ValueError('사전 조정·복구 기록 저장 실패: 이번 구간은 확정하지 않았습니다. 재시도하세요.') from exc

    def summary():
        parts = [f'검증 완료 {accepted:,}/{len(source):,}자 · 새 호출 {state["calls_started"]}회']
        audit = public_report(exclusions, False)
        changes = reconciliation.public_report(reconciliations, False)
        if changes['total_count']:
            parts.insert(0, f'사전 조정 {changes["applied"]}건 · 보류 {changes["deferred"]}건. 승인 전 변경 전후와 근거를 확인하세요.')
        if audit['total_count']:
            parts.insert(0, f'일부 항목 제외 {audit["total_count"]}개 (직접 {audit["direct_count"]}개 · 연쇄 {audit["dependent_count"]}개). 사전 승인 전 제외 내역을 확인하세요.')
        breadth = type_warnings(dictionary, threshold)
        if breadth['field_count']:
            parts.insert(0, f'관계 타입 권장 기준 {threshold}개 초과: {breadth["relation_count"]}개 관계 · {breadth["field_count"]}개 필드, 모든 타입 보존. 승인 전 확인하세요.')
        if resumed_from:
            parts.append(f'{state["resumed_chars"]:,}자 재사용 · 이전 실행 {resumed_from}')
        return ' · '.join(parts + warnings)

    def save_exclusions():
        warning = '사전 제외 상세 저장 실패: 제외 건수와 원문을 확인한 뒤 승인하세요.'
        if not write_report(root, exclusions) and warning not in warnings:
            warnings.append(warning)

    # Preserve inherited validated progress even if this retry fails on its first call.
    save_exclusions()
    write(folder / 'dictionary-progress.json', state)
    try:
        checkpoint(dictionary, exclusions, reconciliations, accepted)
        for index, (offset, chunk) in enumerate(slices, 1):
            if offset < accepted:
                continue
            timeout = cli.effective_timeout(run['agent'], 'ontology') if hasattr(cli, 'effective_timeout') else 600
            store.run_status(run['id'], 'running', f'사전 생성 · 구간 {index}/{len(slices)} · 호출 제한 {timeout}초 · {summary()}')
            spans = source_spans(chunk)
            payload = {'filename': doc['filename'], 'version': run['version'], 'offset': offset,
                       'sources': spans, 'dictionary': dictionary_context(dictionary),
                       'relation_type_warning_threshold': threshold}
            prompt = bounded_prompt(DICTIONARY_PROMPT, payload)
            state['calls_started'] += 1
            write(folder / 'dictionary-progress.json', state)
            try:
                output = cli.complete(run['agent'], run['model'], prompt, folder / str(index), phase='ontology')
                with caller_validation(cli, folder / str(index), run['agent']) as validation:
                    merged, excluded, changes = reconcile_generated_dictionary(dictionary, output, spans, source[:offset + len(chunk)], chunk)
                    if excluded or any(row['status'] == 'deferred' for row in changes):
                        validation.reject()
                    next_exclusions = add_chunk(exclusions, offset, len(chunk), excluded)
                    next_reconciliations = reconciliation.add_chunk(reconciliations, offset, len(chunk), changes)
                    checkpoint(merged, next_exclusions, next_reconciliations, offset + len(chunk))
            except Exception as exc:
                raise ValueError(f'사전 구간 {index}/{len(slices)} · {exc}') from exc
            dictionary, accepted = merged, offset + len(chunk)
            exclusions = next_exclusions
            reconciliations = next_reconciliations
            state['completed_chars'] = accepted
            save_exclusions()
            write(folder / 'dictionary-progress.json', state)
        state['status'] = 'complete'
        write(folder / 'dictionary-progress.json', state)
        return dictionary, summary()
    except Exception as exc:
        state['status'] = 'error'
        write(folder / 'dictionary-progress.json', state)
        raise ValueError(f'{exc} · {summary()}') from exc
