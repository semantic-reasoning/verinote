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
        raise ValueError(f'무손실 표현을 포함한 전체 요청이 입력 예산 {prompt_limit:,}바이트에 들어가지 않습니다. 검증된 사전은 보존됩니다.')
    # context_limit only selects the form; it never refuses. The smaller prompt necessarily fits prompt_limit here.
    wire = size <= raw_size
    diagnostics.update(status='over_context_limit', encoding='dictionary-wire-v1' if wire else 'plain',
                       transmitted_definition_bytes=encoded_size if wire else context_size,
                       prompt_bytes=size if wire else raw_size)
    return prompt if wire else raw


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
