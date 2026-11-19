"""#811 gate A: does a fact-bearing passage get extracted less often when it sits at the back of a chunk?

The decision rule is fixed here before any call. One passage with four directly stated relations
(TARGET) is placed at the front, in the middle and at the back of an otherwise identical 8,000-character
chunk, and the request is the production one (extraction prompt, chunk payload, pinned approved dictionary,
the same prompt builder). Each position runs REPS times, interleaved and rotated so a slow drift in the
provider cannot line up with one position. The chunk is built from whole filler lines, so every position
joins the passage at a line boundary and the chunk is CHUNK_CHARS minus less than one line. A call's score is
how many of the four expected (subject, predicate, object) triples appear in its facts WITH a quote that
occurs exactly once in the chunk and inside the passage (the product drops a row without such a quote).
A triple's quote must lie inside the sentence that states it. A response the product's own preflight
(`extraction_quote_issues` with coverage, contract 3) rejects, or one that states an expected triple with an
ungrounded quote, makes the whole run `inconclusive`: it is not evidence either way. Every planned repeat runs.

"Late-part loss" is adopted only when the back position scores lowest, its gap to the better of the other
two positions is at least one triple AND larger than the largest within-position spread (max - min over
the reps). A position effect whose lowest position is not the back, or with no effect beyond the spread,
is not adoption. Outcomes: `adopt_late_loss`, `rejected` (no effect beyond the repeat variation: keep 8,000),
`inconclusive` (invalid or ungrounded responses, the target was never extracted, a position effect that is not at the back, or the prompt
is outside the production size range). Usage must be complete and the resolved model must equal MODEL.
MAX_CREDITS is a soft ceiling: a call starts only while spent + RESERVE_CREDITS fits.
"""
import json
import time
from pathlib import Path

from ..agents import CLI
from ..usage import atomic_json

MODEL = 'gpt-6-luna'
POSITIONS = ('front', 'middle', 'back')
REPS = 3
MAX_CALLS = 9
MAX_CREDITS = 12.0
RESERVE_CREDITS = 1.5
CHUNK_CHARS = 8000
PROMPT_BYTES_RANGE = (78_000, 115_000)
DEFINITION_BYTES = 58_000
MIN_GAP = 1.0

TARGET = ('청람서비스는 은하저장소에 의존한다. 청람서비스는 별빛큐를 사용한다. '
          '은하저장소는 달무리인증을 호출한다. 별빛큐는 노을감시기로 모니터링된다.\n')
EXPECTED = (('cheongram', 'depends_on', 'galaxy-store'), ('cheongram', 'uses', 'starlight-queue'),
            ('galaxy-store', 'calls', 'moonhalo-auth'), ('starlight-queue', 'monitored_by', 'sunset-monitor'))
TARGET_ENTITIES = (('cheongram', '청람서비스'), ('galaxy-store', '은하저장소'), ('starlight-queue', '별빛큐'),
                   ('moonhalo-auth', '달무리인증'), ('sunset-monitor', '노을감시기'))
TARGET_RELATIONS = (('depends_on', '의존한다'), ('uses', '사용한다'), ('calls', '호출한다'), ('monitored_by', '모니터링된다'))
TYPES = ('시스템', '부서', '문서', '장비', '절차', '사람')


def dictionary_payload(size=DEFINITION_BYTES):
    entities = [{'id': i, 'name': n, 'type': '시스템'} for i, n in TARGET_ENTITIES]
    aliases: list[dict] = []
    relations = [{'id': i, 'description': f'{d}: 주어 시스템이 목적어 시스템에 대해 직접 {d} 관계를 가질 때만 사용한다.',
                  'subject_types': ['시스템'], 'object_types': ['시스템']} for i, d in TARGET_RELATIONS]
    relations += [{'id': f'rel_{k}', 'description': f'가상 관계 {k}: 측정용 임의 정의이며 실제 의미가 없다.',
                   'subject_types': [TYPES[k % 6]], 'object_types': [TYPES[(k + 1) % 6]]} for k in range(8)]
    n = 0
    while len(json.dumps({'entities': entities, 'aliases': aliases, 'relations': relations}, ensure_ascii=False, separators=(',', ':')).encode()) < size:
        entities.append({'id': f'ent-{n:04d}', 'name': f'가상개체{n:04d}호', 'type': TYPES[n % 6]})
        aliases.append({'alias': f'가상별칭{n:04d}', 'entity': f'ent-{n:04d}'})
        n += 1
    return {'entities': entities, 'aliases': aliases, 'relations': relations}


def check_dictionary():
    """The synthetic dictionary must be one the product would accept, or the probe measures a malformed request."""
    from ..ontology import _dictionary_definition, lookup_spelling_problems
    payload = dictionary_payload()
    for kind, rows in payload.items():
        for row in rows:
            _dictionary_definition(kind, row)
    if lookup_spelling_problems(payload):
        raise RuntimeError('dictionary_invalid: lookup spelling problems')
    types = {e['type'] for e in payload['entities']}
    ids = {e['id'] for e in payload['entities']}
    if any(not set(r[k]) <= types for r in payload['relations'] for k in ('subject_types', 'object_types')) \
            or any(a['entity'] not in ids for a in payload['aliases']):
        raise RuntimeError('dictionary_invalid: undeclared type or entity')


def filler(length):
    """Whole numbered lines, as many as fit in `length` characters."""
    lines: list[str] = []
    total, n = 0, 0
    while True:
        line = f'{n + 1}번 점검 기록: {n % 7 + 1}분기 일정과 담당 회의 결과를 정리했고 세부 수치는 별도 표를 따른다.\n'
        if total + len(line) > length:
            return ''.join(lines)
        lines.append(line); total += len(line); n += 1


def chunk_text(position):
    rest = filler(CHUNK_CHARS - len(TARGET))
    if position == 'front':
        cut = 0
    elif position == 'back':
        cut = len(rest)
    else:
        cut = rest.rfind('\n', 0, len(rest) // 2) + 1
    text = rest[:cut] + TARGET + rest[cut:]
    assert CHUNK_CHARS - 200 < len(text) <= CHUNK_CHARS and text.count(TARGET) == 1 and text.endswith('\n')
    return text, text.index(TARGET)


def build_prompt(position, folder, cli=None):
    from ..dictionary_context import (
        chunk_payload,
        extraction_pin,
        pinned_dictionary,
        prepare,
    )
    from ..ontology import MAX_DICTIONARY_CONTEXT_BYTES, extraction_prompts
    text, offset = chunk_text(position)
    pin = extraction_pin('probe-dictionary', 1, dictionary_payload(), 3)
    payload = chunk_payload({'filename': 'probe.md', 'version': 1}, 0, text) | pinned_dictionary(pin)
    prompt = prepare(cli, 'copilot', extraction_prompts(3)[0], payload, MAX_DICTIONARY_CONTEXT_BYTES, Path(folder))
    return prompt, offset


EVIDENCE = tuple(x.strip() for x in TARGET.replace('. ', '.\n').split('\n') if x.strip())


def usable(answer, text):
    """The product's own sequence for a fresh answer (service.extract): off-dictionary facts excluded, quote preflight,
    index normalization, then the full coverage check. Returns the normalized output; raises ValueError when the
    product would reject it. An unresolved quote issue is also unusable: the probe never repairs."""
    from ..extraction_coverage import (
        exclude_off_dictionary_facts,
        repair_missing_indices,
    )
    from ..ontology import extraction_quote_issues
    dictionary = dictionary_payload()
    output, excluded = exclude_off_dictionary_facts(answer, dictionary)
    if extraction_quote_issues(output, text, dictionary, contract_version=3, skip_off_dictionary=True):
        raise ValueError('a quote is missing from or ambiguous in the chunk')
    normalized, _ = repair_missing_indices(output, dictionary, allow_model_range=not excluded, prune_claim_references=True)
    if extraction_quote_issues(normalized, text, dictionary, require_coverage=True, contract_version=3, skip_off_dictionary=True):
        raise ValueError('a quote is missing from or ambiguous in the chunk')
    return normalized


def supports(index, quote):
    """The quote names both entities and the relation verb of the triple it is offered for."""
    names = dict(TARGET_ENTITIES)
    subject, predicate, obj = EXPECTED[index]
    return all(part in quote for part in (names[subject], names[obj], dict(TARGET_RELATIONS)[predicate]))


def score(answer, text):
    """(grounded expected triples, facts, ungrounded expected triples). A triple is grounded only by a quote inside the
    sentence that states it and occurring once in the chunk."""
    if not isinstance(answer, dict) or not isinstance(answer.get('facts'), list):
        raise ValueError('answer has no facts list')
    grounded: set[tuple] = set()
    ungrounded: set[tuple] = set()
    for fact in answer['facts']:
        if not isinstance(fact, dict):
            continue
        triple, quote = (fact.get('subject'), fact.get('predicate'), fact.get('object')), fact.get('quote')
        if triple not in EXPECTED:
            continue
        ok = isinstance(quote, str) and bool(quote.strip()) and text.count(quote) == 1 and quote in EVIDENCE[EXPECTED.index(triple)] and supports(EXPECTED.index(triple), quote)
        (grounded if ok else ungrounded).add(triple)
    return len(grounded), len(answer['facts']), len(ungrounded - grounded)


def decide(scores, prompt_bytes, ungrounded=0, invalid=0):
    """scores: {position: [recovered triples per rep]}."""
    reasons = []
    if invalid:
        reasons.append(f'{invalid} responses were not usable extraction output')
    if ungrounded:
        reasons.append(f'{ungrounded} expected triples were stated with an ungrounded quote')
    if any(not PROMPT_BYTES_RANGE[0] <= b <= PROMPT_BYTES_RANGE[1] for b in prompt_bytes):
        reasons.append(f'prompt bytes outside {PROMPT_BYTES_RANGE}: {sorted(set(prompt_bytes))}')
    if all(s == 0 for rows in scores.values() for s in rows):
        reasons.append('the target was never extracted, so no position can be compared')
    if reasons:
        return {'verdict': 'inconclusive', 'reasons': reasons}
    means = {p: sum(rows) / len(rows) for p, rows in scores.items()}
    spread = max(max(rows) - min(rows) for rows in scores.values())
    gap = max(means['front'], means['middle']) - means['back']
    other = max(means.values()) - min(means.values())
    base = {'means': means, 'max_within_position_spread': spread, 'back_gap': round(gap, 3), 'min_gap': MIN_GAP}
    if means['back'] < min(means['front'], means['middle']) and gap >= MIN_GAP and gap > spread:
        return base | {'verdict': 'adopt_late_loss'}
    if other >= MIN_GAP and other > spread:
        return base | {'verdict': 'inconclusive', 'reasons': ['a position effect exists but the back position is not the lowest']}
    return base | {'verdict': 'rejected'}


def schedule():
    return [(rep, POSITIONS[(i + rep) % 3]) for rep in range(REPS) for i in range(3)]


def run(root, complete=None, cli=None, clock=time.monotonic):
    root = Path(root)
    if root.exists() and any(root.iterdir()):
        raise RuntimeError('output_directory_not_empty')
    root.mkdir(parents=True, exist_ok=True)
    cli = cli or (CLI() if complete is None else None)
    complete = complete or cli.complete
    calls: list[dict] = []
    scores: dict[str, list[int]] = {p: [] for p in POSITIONS}
    prompt_bytes: list[int] = []
    ungrounded_total = invalid_total = 0
    spent, started = 0.0, clock()
    check_dictionary()

    for rep, position in schedule():
        if len(calls) >= MAX_CALLS or spent + RESERVE_CREDITS > MAX_CREDITS:
            raise RuntimeError('budget_exhausted')
        folder = root / f'work-{len(calls) + 1:02d}-{position}'
        prompt, offset = build_prompt(position, folder, cli)
        text = chunk_text(position)[0]
        for name in ('usage.json', 'provider-usage.json'):
            (folder / name).unlink(missing_ok=True)
        t0 = clock()
        record = {'rep': rep, 'position': position, 'target_offset': offset, 'prompt_bytes': len(prompt.encode())}
        try:
            answer = complete('copilot', MODEL, prompt, folder)
            try:
                normalized = usable(answer, text)
            except ValueError as exc:
                record['invalid'] = str(exc)
                invalid_total += 1
                recovered, fact_count, ungrounded = 0, 0, 0
            else:
                recovered, fact_count, ungrounded = score(normalized, text)
        except Exception as exc:
            record['error'] = f'{type(exc).__name__}: {exc}'
            answer = None
        raw = {}
        for name in ('usage.json', 'provider-usage.json'):
            path = folder / name
            raw[name] = json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
        usage = raw['usage.json'] or {}
        credits = usage.get('reported_credits')
        record |= {'seconds': round(clock() - t0, 2), 'credits': credits, 'tokens': usage.get('tokens')}
        if isinstance(credits, (int, float)):
            spent += credits
        calls.append(record)
        atomic_json(root / f'call-{len(calls):02d}.json', record | {'answer': answer, 'raw': raw})
        if 'error' in record:
            raise RuntimeError(f'call_failed: {record["error"]}')
        if usage.get('complete') is not True or not isinstance(credits, (int, float)):
            raise RuntimeError('unknown_usage')
        if usage.get('resolved_model') != MODEL:
            raise RuntimeError(f'model_mismatch: {usage.get("resolved_model")}')
        record |= {'recovered': recovered, 'facts': fact_count, 'ungrounded': ungrounded}
        ungrounded_total += ungrounded
        atomic_json(root / f'call-{len(calls):02d}.json', record | {'answer': answer, 'raw': raw})
        scores[position].append(recovered)
        prompt_bytes.append(record['prompt_bytes'])
        if 'invalid' in record or ungrounded:
            break  # the verdict is already inconclusive, so further paid calls cannot change it
    result = {'model': MODEL, 'calls': len(calls), 'credits': round(spent, 4), 'seconds': round(clock() - started, 1),
              'scores': scores, 'prompt_bytes': prompt_bytes} | decide(scores, prompt_bytes, ungrounded_total, invalid_total)
    atomic_json(root / 'analysis.json', result)
    return result


if __name__ == '__main__':
    import sys
    print(json.dumps(run(sys.argv[1]), ensure_ascii=False, indent=2))
