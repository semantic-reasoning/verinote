"""Compact, resumable dictionary generation; checkpoints are never approvals."""
import json

from .call_validation import caller_validation
from .dictionary_exclusions import (
    add_chunk,
    identity,
    new_report,
    public_report,
    validate_report,
    write_report,
)
from .dictionary_filter import FILTER_POLICY, merge_generated_dictionary
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
CHECKPOINT_LIMIT = MAX_DICTIONARY_BYTES + 2 * 1024 * 1024


def generate_dictionary(store, cli, root, run, doc):
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
    if run['agent'] == 'openrouter':
        from .transports import job_options
        contract['execution_options'] = job_options(run.get('execution_options'))
    folder = root / 'runs' / run['id']
    folder.mkdir(parents=True, exist_ok=True)
    dictionary: dict[str, list[object]] = {kind: [] for kind in LIMITS}
    exclusions = new_report(run, doc)
    accepted, resumed_from, warnings = 0, None, []
    skipped = 0
    option_mismatches = 0
    for prior in store.dictionary_retries(run):
        raw = safe_file(root, f'runs/{prior["id"]}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        try:
            value = json.loads(raw) if raw else None
            fields = {'schema_version', 'run_id', 'contract', 'offset', 'dictionary', 'resumed_from'}
            if isinstance(value, dict) and value.get('schema_version') == 2:
                fields.add('exclusions')
            if not isinstance(value, dict) or set(value) != fields:
                raise ValueError('checkpoint shape')
            if run['agent'] == 'openrouter' and isinstance(value['contract'], dict) and value['contract'].get('execution_options') != contract['execution_options']:
                option_mismatches += 1
                raise ValueError('checkpoint HTTP options mismatch')
            offset = value['offset']
            expected = dict(contract)
            if value['schema_version'] == 1:
                expected.pop('filter_policy')  # Strict legacy prefixes excluded no rows.
            if type(value['schema_version']) is not int or value['schema_version'] not in (1, 2) or value['run_id'] != prior['id'] or canonical(value['contract']) != canonical(expected) or type(offset) is not int or offset not in boundaries:
                raise ValueError('checkpoint identity/boundary')
            candidate_dictionary = validate_dictionary(value['dictionary'], source[:offset])
            if value['schema_version'] == 2:
                candidate_exclusions = validate_report(value['exclusions'], prior, doc, offset)
                candidate_exclusions['identity'] = identity(run, doc)
            else:
                candidate_exclusions = new_report(run, doc)
                for start, chunk in slices:
                    if start >= offset:
                        break
                    candidate_exclusions = add_chunk(candidate_exclusions, start, len(chunk), [])
            dictionary, exclusions = candidate_dictionary, candidate_exclusions
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

    def checkpoint():
        if not accepted:
            return
        value = {'schema_version': 2, 'run_id': run['id'], 'contract': contract, 'offset': accepted,
                 'dictionary': dictionary, 'resumed_from': resumed_from, 'exclusions': exclusions}
        if len(json.dumps(value, ensure_ascii=False).encode()) > CHECKPOINT_LIMIT:
            warnings.append('복구 기록 크기 초과: 진행 상태를 저장하지 못했습니다.')
            return
        write(folder / 'dictionary-checkpoint.json', value)

    def summary():
        parts = [f'검증 완료 {accepted:,}/{len(source):,}자 · 새 호출 {state["calls_started"]}회']
        audit = public_report(exclusions, False)
        if audit['total_count']:
            parts.insert(0, f'일부 항목 제외 {audit["total_count"]}개 (직접 {audit["direct_count"]}개 · 연쇄 {audit["dependent_count"]}개). 사전 승인 전 제외 내역을 확인하세요.')
        if resumed_from:
            parts.append(f'{state["resumed_chars"]:,}자 재사용 · 이전 실행 {resumed_from}')
        return ' · '.join(parts + warnings)

    def save_exclusions():
        warning = '사전 제외 상세 저장 실패: 제외 건수와 원문을 확인한 뒤 승인하세요.'
        if not write_report(root, exclusions) and warning not in warnings:
            warnings.append(warning)

    # Preserve inherited validated progress even if this retry fails on its first call.
    checkpoint()
    save_exclusions()
    write(folder / 'dictionary-progress.json', state)
    try:
        for index, (offset, chunk) in enumerate(slices, 1):
            if offset < accepted:
                continue
            timeout = cli.effective_timeout(run['agent'], 'ontology') if hasattr(cli, 'effective_timeout') else 600
            store.run_status(run['id'], 'running', f'사전 생성 · 구간 {index}/{len(slices)} · 호출 제한 {timeout}초 · {summary()}')
            spans = source_spans(chunk)
            payload = {'filename': doc['filename'], 'version': run['version'], 'offset': offset,
                       'sources': spans, 'dictionary': dictionary_context(dictionary)}
            prompt = bounded_prompt(DICTIONARY_PROMPT, payload)
            state['calls_started'] += 1
            write(folder / 'dictionary-progress.json', state)
            try:
                output = cli.complete(run['agent'], run['model'], prompt, folder / str(index), phase='ontology')
                with caller_validation(cli, folder / str(index), run['agent']) as validation:
                    merged, excluded = merge_generated_dictionary(dictionary, output, spans, source[:offset + len(chunk)], chunk)
                    if excluded:
                        validation.reject()
                    next_exclusions = add_chunk(exclusions, offset, len(chunk), excluded)
            except Exception as exc:
                raise ValueError(f'사전 구간 {index}/{len(slices)} · {exc}') from exc
            dictionary, accepted = merged, offset + len(chunk)
            exclusions = next_exclusions
            state['completed_chars'] = accepted
            checkpoint()
            save_exclusions()
            write(folder / 'dictionary-progress.json', state)
        state['status'] = 'complete'
        write(folder / 'dictionary-progress.json', state)
        return dictionary, summary()
    except Exception as exc:
        state['status'] = 'error'
        write(folder / 'dictionary-progress.json', state)
        raise ValueError(f'{exc} · {summary()}') from exc
