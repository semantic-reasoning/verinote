"""Dictionary definition wire-form threshold and lossless definition tables."""
import copy
import json

from .store import canonical, digest

COLUMNS = {'entities': ['id', 'name', 'type'], 'aliases': ['alias', 'entity'],
           'relations': ['id', 'description', 'subject_types', 'object_types']}
PROTOCOL = '''Dictionary wire v1: a dictionary object marked dictionary_wire_version=1
is an exact table encoding of definitions, not a summary. For each kind, pair
columns[kind] with each row in rows[kind]. Every integer is a zero-based index
into strings; arrays of integers restore ordered type arrays. Expand all values
before reasoning. Use original string IDs/types in responses, never table indices.
Definitions and their order are complete; sha256 binds the expanded definition
object. Source quotations remain in sources/text/issues; the approved pin hash
identifies the unchanged stored dictionary including its original quotations.
'''
MAX_DECODE_BYTES = 24 * 1024 * 1024
# Serialized size of a dictionary with no rows: what no revision can remove from the transmitted definitions.
EMPTY_DEFINITION_BYTES = len(json.dumps({kind: [] for kind in COLUMNS}, separators=(',', ':')).encode())


class RequestTooLarge(ValueError):
    """Both lossless forms exceed the budget; sizes are the smaller form's, and excess is how far its request is over."""

    def __init__(self, request_bytes, limit, definition_bytes, advice=''):
        self.request_bytes, self.limit, self.definition_bytes = request_bytes, limit, definition_bytes
        super().__init__(f'무손실 표현을 포함한 전체 요청 {request_bytes:,}바이트가 입력 예산 {limit:,}바이트에 들어가지 않습니다. 검증된 사전은 보존됩니다.'
                         + (f' {advice}' if advice else ''))

    @property
    def excess(self):
        return self.request_bytes - self.limit

    def with_advice(self, advice):
        return RequestTooLarge(self.request_bytes, self.limit, self.definition_bytes, advice)

    def _other_agent_step(self, available):
        from .ontology import MAX_PROMPT_BYTES
        if available and self.request_bytes <= MAX_PROMPT_BYTES:
            return (f'입력 예산이 {MAX_PROMPT_BYTES:,}바이트인 다른 에이전트로 처음부터 실행'
                    '(완료분을 재사용하지 않고 새로 호출, 원문과 사전이 해당 제공자에게 전달됨)')
        return None

    def _pinned_steps(self, other_agent, revision):
        steps = [self._other_agent_step(other_agent)]
        # Revising and approving each add one to the pin's revision, which the request carries.
        needed = self.excess + len(str(revision + 2)) - len(str(revision))
        if needed <= self.definition_bytes - EMPTY_DEFINITION_BYTES:
            steps.append(f'사전을 수정해 전송 정의 {self.definition_bytes:,}바이트 중 {needed:,}바이트 이상을 줄이고 재승인'
                         '(모든 청크를 새로 호출하며 뺀 정의는 추출에 쓸 수 없음)')
        steps.append('문서를 나누어 각각 사전 생성·승인'
                     '(승인된 사전은 재사용되지 않음, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함)')
        return ' 또는 '.join(step for step in steps if step)

    def generation_advice(self, accepted, other_agent):
        size = f'앞에서 통과한 {accepted:,}자 이하로' if accepted else '더 작게'
        split = (f'문서를 {size} 나누어 각각 사전 생성'
                 '(검증된 구간은 재사용되지 않고 새로 호출, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함, '
                 '추출은 완성된 사전 전체를 보내므로 별도로 거부될 수 있음)')
        steps = [self._other_agent_step(other_agent), split]
        return ('복구 기록이 유효하면 같은 에이전트·모델로 다시 실행해도 같은 크기로 거부됩니다. 다음 단계: '
                + ' 또는 '.join(step for step in steps if step)
                + '. 위 단계를 따라도 사전 생성의 뒤 구간이나 더 조밀한 부분에서 다시 거부될 수 있으며 그 전 호출 비용은 발생합니다.')

    def extraction_advice(self, other_agent, revision):
        return f'같은 문서 버전·승인 사전·에이전트로 다시 추출해도 같은 크기로 거부됩니다. 다음 단계: {self._pinned_steps(other_agent, revision)}.'

    def repair_advice(self, other_agent, revision):
        return ('보정 요청 크기는 문제 인용에 따라 달라지므로 같은 문서 버전·승인 사전·에이전트·모델로 다시 추출하면 통과할 수 있지만 보장되지 않고 이 청크를 새로 호출합니다. '
                f'반복되면 다음 단계: {self._pinned_steps(other_agent, revision)}.')


def validate_context_bytes(value):
    from .ontology import MAX_PROMPT_BYTES
    if type(value) is not int or not 1 <= value <= MAX_PROMPT_BYTES:
        raise ValueError(f'사전 표 전환 기준은 1~{MAX_PROMPT_BYTES:,} 정수 바이트여야 합니다.')
    return value


def encode(definitions):
    strings: list[str] = []
    positions = {}
    def ref(value):
        if isinstance(value, list): return [ref(v) for v in value]
        if not isinstance(value, str): raise ValueError('사전 문맥 문자열 형식 오류')
        if value not in positions:
            positions[value] = len(strings); strings.append(value)
        return positions[value]
    if set(definitions) != set(COLUMNS): raise ValueError('사전 문맥 종류 오류')
    rows: dict[str, list] = {}
    for kind, columns in COLUMNS.items():
        rows[kind] = []
        for row in definitions[kind]:
            if set(row) != set(columns): raise ValueError('사전 문맥 필드 오류')
            rows[kind].append([ref(row[c]) for c in columns])
    result = {'dictionary_wire_version': 1, 'columns': copy.deepcopy(COLUMNS), 'strings': strings,
              'rows': rows, 'sha256': digest(definitions)}
    if decode(result) != definitions: raise ValueError('사전 문맥 무손실 복원 불일치')
    return result


def decode(value):
    from .ontology import DICTIONARY_TOTAL_LIMITS
    if (not isinstance(value, dict) or set(value) != {'dictionary_wire_version', 'columns', 'strings', 'rows', 'sha256'}
            or type(value['dictionary_wire_version']) is not int or value['dictionary_wire_version'] != 1
            or value['columns'] != COLUMNS or not isinstance(value['strings'], list)
            or any(not isinstance(s, str) for s in value['strings'])
            or not isinstance(value['rows'], dict) or set(value['rows']) != set(COLUMNS)):
        raise ValueError('사전 문맥 표 형식 오류')
    if len(canonical(value).encode()) > MAX_DECODE_BYTES:
        raise ValueError('사전 문맥 표 크기 초과')
    table = value['strings']; used_bytes = 0
    if len(table) > MAX_DECODE_BYTES or sum(len(s.encode()) for s in table) > MAX_DECODE_BYTES:
        raise ValueError('사전 문맥 복원 크기 초과')
    def expand(index):
        nonlocal used_bytes
        if type(index) is not int or not 0 <= index < len(table): raise ValueError('사전 문맥 참조 오류')
        result = table[index]; used_bytes += len(result.encode())
        if used_bytes > MAX_DECODE_BYTES: raise ValueError('사전 문맥 복원 크기 초과')
        return result
    result: dict[str, list] = {}
    for kind, columns in COLUMNS.items():
        rows = value['rows'][kind]
        if not isinstance(rows, list) or len(rows) > DICTIONARY_TOTAL_LIMITS[kind]: raise ValueError('사전 문맥 행 개수 오류')
        result[kind] = []; keys = set()
        for row in rows:
            if not isinstance(row, list) or len(row) != len(columns): raise ValueError('사전 문맥 행 너비 오류')
            restored = {}
            for column, item in zip(columns, row, strict=True):
                if column in ('subject_types', 'object_types'):
                    if not isinstance(item, list) or not item or len(item) > DICTIONARY_TOTAL_LIMITS['entities']:
                        raise ValueError('사전 문맥 타입 배열 오류')
                    restored[column] = [expand(i) for i in item]
                else:
                    restored[column] = expand(item)
            key = restored['alias' if kind == 'aliases' else 'id']
            if key in keys: raise ValueError('사전 문맥 중복 ID 오류')
            keys.add(key); result[kind].append(restored)
    if len(canonical(result).encode()) > MAX_DECODE_BYTES or digest(result) != value['sha256']:
        raise ValueError('사전 문맥 복원 해시/크기 불일치')
    return result


def build(instructions, payload, *, context_limit, prompt_limit, diagnostics):
    from .ontology import dictionary_context
    serialize = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))  # noqa: E731
    raw = instructions + serialize(payload)
    selected = payload.get('dictionary')
    if selected is None and isinstance(payload.get('approved_dictionary'), dict):
        selected = payload['approved_dictionary'].get('payload')
    if selected is not None and (not isinstance(selected, dict)
            or any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) for rows in selected.values())):
        raise ValueError('사전 문맥 행 형식 오류')
    definitions = dictionary_context(selected) if selected is not None else None
    raw_size = len(raw.encode()); context_size = len(serialize(definitions).encode()) if definitions is not None else 0
    diagnostics.update(schema_version=1, context_limit_bytes=context_limit, effective_prompt_limit_bytes=prompt_limit,
                       raw_definition_bytes=context_size, raw_prompt_bytes=raw_size,
                       model_token_admission='unknown', encoding='plain', status='unfit')
    if context_size <= context_limit and raw_size <= prompt_limit:
        diagnostics.update(status='within_limit', transmitted_definition_bytes=context_size, prompt_bytes=raw_size)
        return raw
    if definitions is None:
        raise ValueError(f'전체 요청 {raw_size:,}바이트가 입력 예산 {prompt_limit:,}바이트를 초과합니다.')
    encoded = encode(definitions)
    packed = copy.deepcopy(payload)
    if 'dictionary' in packed: packed['dictionary'] = encoded
    else: packed['approved_dictionary']['payload'] = encoded
    prompt = PROTOCOL + instructions + serialize(packed)
    encoded_size, size = len(serialize(encoded).encode()), len(prompt.encode())
    diagnostics.update(encoded_definition_bytes=encoded_size, encoded_prompt_bytes=size, definition_sha256=digest(definitions))
    if encoded_size <= context_limit and size <= prompt_limit:
        diagnostics.update(status='within_limit', encoding='dictionary-wire-v1',
                           transmitted_definition_bytes=encoded_size, prompt_bytes=size)
        return prompt
    if min(raw_size, size) > prompt_limit:
        raise RequestTooLarge(min(raw_size, size), prompt_limit, encoded_size if size <= raw_size else len(serialize(selected).encode()))
    # context_limit only selects the form; it never refuses. The smaller prompt necessarily fits prompt_limit here.
    wire = size <= raw_size
    diagnostics.update(status='over_context_limit', encoding='dictionary-wire-v1' if wire else 'plain',
                       transmitted_definition_bytes=encoded_size if wire else context_size,
                       prompt_bytes=size if wire else raw_size)
    return prompt if wire else raw


def refusal(cli, agent, instructions, payload, context_limit):
    """Same admission as prepare, with no record written: the RequestTooLarge it would raise, or None."""
    from .ontology import bounded_prompt
    reader = getattr(cli, 'prompt_byte_limit', None)
    try:
        bounded_prompt(instructions, payload, context_limit=context_limit, prompt_limit=reader(agent) if callable(reader) else None)
    except RequestTooLarge as exc:
        return exc
    return None


def prepare(cli, agent, instructions, payload, context_limit, folder):
    from .ontology import bounded_prompt
    from .usage import atomic_json
    reader = getattr(cli, 'prompt_byte_limit', None)
    transport_limit = reader(agent) if callable(reader) else None
    report = {'transport_limit_bytes': transport_limit}
    folder.mkdir(parents=True, exist_ok=True)
    try:
        return bounded_prompt(instructions, payload, context_limit=context_limit,
                              prompt_limit=transport_limit, diagnostics=report)
    finally:
        atomic_json(folder/'context-budget.json', report)
