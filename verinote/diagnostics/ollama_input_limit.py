"""Measure how many Ollama prompt tokens real extraction and dictionary prompts use per UTF-8 byte (#826).

Measurement only: it calls the local Ollama /api/generate with the product's num_ctx and num_predict and the product's
prompt texts, and changes no limit. Every call writes its raw evidence under a new result folder.
Run: python3 -m verinote.diagnostics.ollama_input_limit --model qwen2.5:7b --out benchmarks/extraction/results/826-ollama-input-limit
"""
import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from verinote.dictionary_context import chunk_payload
from verinote.ollama import DEFAULTS, catalog, input_byte_limit, request
from verinote.ontology import (
    DICTIONARY_PROMPT,
    EXTRACTION_CONTRACT_VERSION,
    extraction_prompts,
    source_spans,
)

SIZES = (8192, 27648, 40960, 81920, 122880)
KINDS = ('korean', 'ascii')
STAGES = ('extract', 'dictionary')
DICTIONARY = {'entities': [{'id': 'service', 'name': 'service', 'type': 'System'}, {'id': '서비스', 'name': '서비스', 'type': 'System'}],
              'aliases': [], 'relations': [{'id': 'depends_on', 'description': 'depends on', 'subject_types': ['System'], 'object_types': ['System']}]}


def serialize(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def marker_line(marker):
    return f'\n끝 표식: {marker}. 응답 JSON 최상위에 "end_marker" 필드로 이 표식을 그대로 쓰세요.'


def corpus(kind):
    if kind == 'korean':
        text = Path('docs/architecture.md').read_text(encoding='utf-8')
    else:
        import argparse as module
        import typing
        text = Path(module.__file__).read_text(encoding='utf-8') + Path(typing.__file__).read_text(encoding='utf-8')
        text = ''.join(c for c in text if ord(c) < 128)
    return text


def build(stage, kind, target, marker):
    """The product prompt for `stage` whose UTF-8 size, with the end marker, is `target` bytes (or the closest below it)."""
    source = corpus(kind)

    def prompt(count):
        text = (source * (count // len(source) + 1))[:count]
        if stage == 'extract':
            payload = chunk_payload({'filename': 'measure.md', 'version': 1}, 0, text) | {
                'approved_dictionary': {'id': 'a' * 32, 'revision': 2, 'payload': DICTIONARY}}
            return extraction_prompts(EXTRACTION_CONTRACT_VERSION)[0] + serialize(payload) + marker_line(marker)
        payload = {'filename': 'measure.md', 'version': 1, 'offset': 0, 'sources': source_spans(text), 'dictionary': DICTIONARY,
                   'relation_type_warning_threshold': 3}
        return DICTIONARY_PROMPT + serialize(payload) + marker_line(marker)

    low, high = 0, target
    while low < high:
        middle = (low + high + 1) // 2
        if len(prompt(middle).encode()) <= target:
            low = middle
        else:
            high = middle - 1
    return prompt(low), low


def run(model, options, out, sizes, kinds, stages, timeout):
    identity = next((m for m in catalog() if m['id'] == model), None)
    if identity is None:
        raise SystemExit(f'model not installed: {model}')
    out.mkdir(parents=True)
    environment = {'model': model, 'model_digest': identity['digest'], 'ollama_version': request('/api/version')['version'],
                   'num_ctx': options['num_ctx'], 'num_predict': options['num_predict'], 'temperature': options['temperature'],
                   'product_input_byte_limit': input_byte_limit(), 'sizes': list(sizes), 'kinds': list(kinds), 'stages': list(stages),
                   'extraction_contract_version': EXTRACTION_CONTRACT_VERSION, 'format': 'json', 'keep_alive': options['keep_alive']}
    (out / 'environment.json').write_text(json.dumps(environment, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (out / 'calls').mkdir()
    number = 0
    for stage in stages:
        for kind in kinds:
            for size in sizes:
                number += 1
                marker = f'END-{hashlib.sha256(f"{stage}{kind}{size}".encode()).hexdigest()[:10]}'
                prompt, chars = build(stage, kind, size, marker)
                record = {'number': number, 'stage': stage, 'kind': kind, 'target_bytes': size, 'text_chars': chars,
                          'prompt_bytes': len(prompt.encode()), 'prompt_chars': len(prompt), 'marker': marker,
                          'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
                (out / 'calls' / f'{number:02}-prompt.txt').write_text(prompt, encoding='utf-8')
                started = time.monotonic()
                try:
                    value = request('/api/generate', {'model': model, 'prompt': prompt, 'stream': False, 'format': 'json',
                                                      'keep_alive': options['keep_alive'],
                                                      'options': {k: options[k] for k in ('num_ctx', 'num_predict', 'temperature')}},
                                    timeout=timeout, limit=8 * 1024 * 1024)
                except Exception as exc:
                    value = {}
                    record['error'] = f'{type(exc).__name__}: {exc}'
                record['seconds'] = round(time.monotonic() - started, 2)
                text = value.get('response') if isinstance(value.get('response'), str) else None
                parsed = None
                if text is not None:
                    try:
                        parsed = json.loads(text)
                    except ValueError:
                        parsed = None
                record.update(prompt_eval_count=value.get('prompt_eval_count'), eval_count=value.get('eval_count'),
                              done=value.get('done'), done_reason=value.get('done_reason'),
                              response_json_valid=None if text is None else parsed is not None,
                              end_marker_echoed=None if not isinstance(parsed, dict) else parsed.get('end_marker') == marker,
                              response_keys=sorted(parsed) if isinstance(parsed, dict) else None)
                count = record['prompt_eval_count']
                record['tokens_per_byte'] = round(count / record['prompt_bytes'], 4) if isinstance(count, int) else None
                record['fits_num_ctx_minus_num_predict'] = (count <= options['num_ctx'] - options['num_predict']) if isinstance(count, int) else None
                (out / 'calls' / f'{number:02}-response.json').write_text(json.dumps(value, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
                (out / 'calls' / f'{number:02}.json').write_text(json.dumps(record, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
                print(json.dumps({k: record.get(k) for k in ('number', 'stage', 'kind', 'prompt_bytes', 'prompt_eval_count', 'tokens_per_byte',
                                                             'done_reason', 'end_marker_echoed', 'seconds', 'error')}, ensure_ascii=False), flush=True)


REQUIRED_KEYS = {'extract': ('claims', 'coverage', 'dictionary_gaps', 'facts', 'title'),
                 'dictionary': ('aliases', 'entities', 'relations')}


RATIO_MAX_BYTES = 81920  # the 122,880-byte calls are not used for ratios


def analyze(folder):
    """Derive the schema and fit verdicts from saved calls; the raw records stay untouched."""
    rows = []
    for path in sorted((folder / 'calls').glob('[0-9][0-9].json')):
        record = json.loads(path.read_text(encoding='utf-8'))
        keys = record.get('response_keys')
        complete = None if keys is None else all(k in keys for k in REQUIRED_KEYS[record['stage']])
        count = record.get('prompt_eval_count')
        fits = record.get('fits_num_ctx_minus_num_predict')
        ratio_sizes = record['target_bytes'] <= RATIO_MAX_BYTES
        rows.append({'number': record['number'], 'top_level_schema_keys_present': complete,
                     'product_validation': 'not evaluated',
                     'fits_budget': None if complete is False else fits,
                     'tokens_per_byte_used_for_ratio': record.get('tokens_per_byte') if ratio_sizes and complete is not False else None,
                     'prompt_eval_count': count})
    (folder / 'analysis.json').write_text(json.dumps(rows, indent=1) + '\n', encoding='utf-8')
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sizes', type=lambda s: tuple(int(v) for v in s.split(',')), default=SIZES)
    parser.add_argument('--kinds', default=','.join(KINDS))
    parser.add_argument('--stages', default=','.join(STAGES))
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--analyze', type=Path)
    args = parser.parse_args(argv)
    if args.analyze:
        analyze(args.analyze)
        return
    if not args.model or not args.out:
        raise SystemExit('--model and --out are required')
    if args.out.exists():
        raise SystemExit('the result folder must be new')
    run(args.model, DEFAULTS, args.out, args.sizes, args.kinds.split(','), args.stages.split(','), args.timeout)


if __name__ == '__main__':
    sys.exit(main())
