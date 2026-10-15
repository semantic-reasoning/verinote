"""Compact, resumable dictionary generation; checkpoints are never approvals."""
import json

from .documents import bounded_spans
from .ontology import (DICTIONARY_CHUNK_SIZE, DICTIONARY_PROMPT, LIMITS,
                       MAX_DICTIONARY_CALLS, SOURCE_SPAN_SIZE, SOURCE_SPAN_MIN_SIZE, bounded_prompt,
                       dictionary_context, merge_dictionary, resolve_dictionary,
                       source_spans, validate_dictionary)
from .store import canonical, digest
from .usage import atomic_json, safe_file

PIPELINE = 'source-spans-v1'
CHECKPOINT_LIMIT = 128_000


def generate_dictionary(store, cli, root, run, doc):
    source = doc['text']
    slices = list(bounded_spans(source, DICTIONARY_CHUNK_SIZE))
    if len(slices) > MAX_DICTIONARY_CALLS:
        raise ValueError('사전 생성은 최대 64개 구간까지 지원합니다.')
    boundaries = {offset + len(chunk) for offset, chunk in slices}
    contract = {'pipeline': PIPELINE, 'prompt': digest(DICTIONARY_PROMPT.encode()),
                'chunk_size': DICTIONARY_CHUNK_SIZE, 'span_size': SOURCE_SPAN_SIZE, 'span_min_size': SOURCE_SPAN_MIN_SIZE,
                'document_id': run['document_id'], 'version': run['version'],
                'source_sha256': doc['sha256'], 'text_sha256': digest(source.encode()),
                'agent': run['agent'], 'model': run['model']}
    folder = root / 'runs' / run['id']
    folder.mkdir(parents=True, exist_ok=True)
    dictionary = {kind: [] for kind in LIMITS}
    accepted, resumed_from, warnings = 0, None, []
    skipped = 0
    for prior in store.dictionary_retries(run):
        raw = safe_file(root, f'runs/{prior["id"]}/dictionary-checkpoint.json', CHECKPOINT_LIMIT)
        try:
            value = json.loads(raw) if raw else None
            if not isinstance(value, dict) or set(value) != {'schema_version', 'run_id', 'contract', 'offset', 'dictionary', 'resumed_from'}:
                raise ValueError('checkpoint shape')
            offset = value['offset']
            if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['run_id'] != prior['id'] or canonical(value['contract']) != canonical(contract) or type(offset) is not int or offset not in boundaries:
                raise ValueError('checkpoint identity/boundary')
            dictionary = validate_dictionary(value['dictionary'], source[:offset])
            accepted, resumed_from = offset, prior['id']
            break
        except (ValueError, TypeError, KeyError, UnicodeError):
            skipped += 1
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
        value = {'schema_version': 1, 'run_id': run['id'], 'contract': contract, 'offset': accepted,
                 'dictionary': dictionary, 'resumed_from': resumed_from}
        if len(json.dumps(value, ensure_ascii=False).encode()) > CHECKPOINT_LIMIT:
            warnings.append('복구 기록 크기 초과: 진행 상태를 저장하지 못했습니다.')
            return
        write(folder / 'dictionary-checkpoint.json', value)

    def summary():
        parts = [f'검증 완료 {accepted:,}/{len(source):,}자 · 새 호출 {state["calls_started"]}회']
        if resumed_from:
            parts.append(f'{state["resumed_chars"]:,}자 재사용 · 이전 실행 {resumed_from}')
        return ' · '.join(parts + warnings)

    # Preserve inherited validated progress even if this retry fails on its first call.
    checkpoint()
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
                delta = resolve_dictionary(output, spans)
                merged = merge_dictionary(dictionary, delta, source[:offset + len(chunk)], chunk)
            except Exception as exc:
                raise ValueError(f'사전 구간 {index}/{len(slices)} · {exc}') from exc
            dictionary, accepted = merged, offset + len(chunk)
            state['completed_chars'] = accepted
            checkpoint()
            write(folder / 'dictionary-progress.json', state)
        state['status'] = 'complete'
        write(folder / 'dictionary-progress.json', state)
        return dictionary, summary()
    except Exception as exc:
        state['status'] = 'error'
        write(folder / 'dictionary-progress.json', state)
        raise ValueError(f'{exc} · {summary()}') from exc
