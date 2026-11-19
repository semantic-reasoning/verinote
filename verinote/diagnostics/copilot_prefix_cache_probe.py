"""#812 mechanism (b): does a dictionary placed before the chunk hit the Copilot prompt cache?

The stop rule is fixed here before any call: with the same pinned model, a ~22,000-token synthetic
dictionary leads the request and only a one-line chunk varies. After one warm-up call per condition,
five calls run with the same work folder and five with a fresh folder each. The mechanism is rejected
when, in BOTH conditions, the median cache_read minus the baseline hit (9,802) is below 50% of the
measured dictionary tokens. If only the same-folder condition passes, pinning the folder is in scope.
Each condition uses its own dictionary so one cannot warm the other's cache.

The run is inconclusive, and no threshold is applied, when: a call reports no cost or token figure, the
resolved model is not MODEL, the measured dictionary is outside 18,000-26,000 tokens, or a warm-up
call already reads more than the observed baseline hit (the dictionary was already cached), or the
warmed baseline (a second call without a dictionary) does not read ~9,802 cached tokens, so the fixed
threshold would be measured against a different baseline. Usage must be marked complete. MAX_CREDITS is
a soft ceiling: a call starts only while spent + RESERVE_CREDITS fits, but one call's cost cannot be
bounded in advance, so the last call may overrun it. The output directory must be new and each run
salts its dictionaries with a fresh nonce so a previous run cannot warm this one.
"""
import json
import statistics
import time
import uuid
from pathlib import Path

from ..agents import CLI
from ..usage import atomic_json

MODEL = 'gpt-6-luna'
BASELINE_HIT = 9802
DICTIONARY_BYTES = 62000
REPS = 5
MAX_CALLS = 14
MAX_CREDITS = 8.0
RESERVE_CREDITS = 1.0
TOKEN_RANGE = (18000, 26000)
THRESHOLD = 0.5
CONDITIONS = ('same_folder', 'different_folder')


def synthetic_dictionary(seed, size=DICTIONARY_BYTES):
    rows, total, i = [], 0, 0
    while total < size:
        row = {'id': f'{seed}-{i}', 'type': f'유형{i % 17}', 'label': f'가상 개체 {seed} {i}번', 'aliases': [f'별칭{seed}{i}a', f'별칭{seed}{i}b'],
               'definition': f'{seed} 실험용 가상 정의 {i}: 이 항목은 측정을 위한 임의 문장이며 실제 의미가 없습니다.'}
        text = json.dumps(row, ensure_ascii=False)
        rows.append(row); total += len(text.encode())
        i += 1
    return json.dumps({'approved_dictionary': rows}, ensure_ascii=False)


def prompt(dictionary, n):
    return f'{dictionary}\n\n입력 청크 {n}번입니다. 위 사전은 무시하고 청크 번호만 {{"n": 번호}} JSON으로 답하세요. 청크 번호: {n}'


def inconclusive(measured, baseline_hit):
    reasons = []
    if not 0.95 * BASELINE_HIT <= baseline_hit <= 1.05 * BASELINE_HIT:
        reasons.append(f'warmed baseline cache_read {baseline_hit} is not ~{BASELINE_HIT}')
    for condition, row in measured.items():
        if not TOKEN_RANGE[0] <= row['dictionary_tokens'] <= TOKEN_RANGE[1]:
            reasons.append(f'{condition}: dictionary_tokens {row["dictionary_tokens"]} outside {TOKEN_RANGE}')
        warm = row['warm_cache_read']
        if not 0.95 * baseline_hit <= warm <= 1.05 * baseline_hit:
            reasons.append(f'{condition}: warm-up cache_read {warm} is not the baseline hit {baseline_hit} (dictionary pre-cached or baseline differs)')
    return reasons


def decide(measured, baseline_hit=BASELINE_HIT):
    """measured: {condition: {'dictionary_tokens': int, 'warm_cache_read': int, 'cache_reads': [int, ...]}}."""
    reasons = inconclusive(measured, baseline_hit)
    if reasons:
        return {'verdict': 'inconclusive', 'reasons': reasons, 'threshold': THRESHOLD, 'baseline_hit': BASELINE_HIT}
    out = {}
    for condition in CONDITIONS:
        row = measured[condition]
        median = statistics.median(row['cache_reads'])
        extra = median - BASELINE_HIT
        out[condition] = {'median_cache_read': median, 'beyond_baseline': extra, 'dictionary_tokens': row['dictionary_tokens'],
                          'share': round(extra / row['dictionary_tokens'], 3), 'passes': extra >= THRESHOLD * row['dictionary_tokens']}
    passing = [c for c in CONDITIONS if out[c]['passes']]
    verdict = 'rejected' if not passing else 'pin_folder_in_scope' if passing == ['same_folder'] else 'adopt_candidate'
    return {'conditions': out, 'verdict': verdict, 'threshold': THRESHOLD, 'baseline_hit': BASELINE_HIT}


def run(root, complete=None, clock=time.monotonic):
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError('output_directory_not_empty')
    root.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    complete = complete or CLI().complete
    calls: list[dict] = []
    spent, started = 0.0, clock()

    def call(condition, index, folder, text):
        nonlocal spent
        if len(calls) >= MAX_CALLS or spent + RESERVE_CREDITS > MAX_CREDITS:
            raise RuntimeError('budget_exhausted')
        for name in ('usage.json', 'provider-usage.json'):
            (Path(folder) / name).unlink(missing_ok=True)
        t0 = clock()
        complete('copilot', MODEL, text, folder)
        raw = {name: json.loads((Path(folder) / name).read_text(encoding='utf-8')) for name in ('usage.json', 'provider-usage.json')}
        usage = raw['usage.json']
        credits, tokens = usage.get('reported_credits'), usage.get('tokens') or {}
        if usage.get('complete') is not True or not isinstance(credits, (int, float)) or not all(isinstance(tokens.get(k), int) for k in ('input', 'cache_read')):
            raise RuntimeError('unknown_usage')
        if usage.get('resolved_model') != MODEL:
            raise RuntimeError(f'model_mismatch: {usage.get("resolved_model")}')
        spent += credits
        record = {'condition': condition, 'index': index, 'seconds': round(clock() - t0, 2), 'credits': credits, 'tokens': tokens}
        calls.append(record)
        atomic_json(root / f'call-{len(calls):02d}.json', record | {'raw': raw})
        return record

    measured = {}
    base = call('baseline', 0, root / 'work-baseline', '{"ok": true} JSON으로만 답하세요.')
    baseline_hit = call('baseline', 1, root / 'work-baseline', '{"ok": true} JSON으로만 답하세요.')['tokens']['cache_read']
    for condition in CONDITIONS if 0.95 * BASELINE_HIT <= baseline_hit <= 1.05 * BASELINE_HIT else ():
        dictionary = synthetic_dictionary(f'{condition}-{nonce}')
        shared = root / f'work-{condition}'
        warm = call(condition, 0, shared, prompt(dictionary, 0))
        tokens = warm['tokens']['input'] - base['tokens']['input']
        reads = []
        for n in range(1, REPS + 1):
            folder = shared if condition == 'same_folder' else root / f'work-{condition}-{n}'
            reads.append(call(condition, n, folder, prompt(dictionary, n))['tokens']['cache_read'])
        measured[condition] = {'dictionary_tokens': tokens, 'warm_cache_read': warm['tokens']['cache_read'], 'cache_reads': reads}
    result = {'model': MODEL, 'calls': len(calls), 'credits': round(spent, 4), 'seconds': round(clock() - started, 1),
              'nonce': nonce, 'warmed_baseline_cache_read': baseline_hit, 'measured': measured} | decide(measured, baseline_hit)
    atomic_json(root / 'analysis.json', result)
    return result


if __name__ == '__main__':
    import sys
    print(json.dumps(run(sys.argv[1]), ensure_ascii=False, indent=2))
