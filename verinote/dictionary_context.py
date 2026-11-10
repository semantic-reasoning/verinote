"""Dictionary definition wire-form threshold and lossless definition tables."""
import copy
import functools
import json

from .store import canonical, digest

COLUMNS = {'entities': ['id', 'name', 'type'], 'aliases': ['alias', 'entity'],
           'relations': ['id', 'description', 'subject_types', 'object_types']}
PROTOCOL = """Dictionary wire v2: a dictionary object marked dictionary_wire_version=2
is an exact positional table of definitions, not a summary. For each kind, pair
columns[kind] with each row in rows[kind]. Every scalar cell is its original
literal string; type arrays contain original literal strings. There is no string
reference table. Copy the id/alias strings exactly in responses, including numeric-
looking IDs. Row positions are not identifiers. Preserve definitions, order,
direction and allowed types; sha256 binds the expanded definition object.
Source quotations remain in sources/text/issues; the approved pin hash identifies
the unchanged stored dictionary including its original quotations.
"""
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

    def _other_agent_fits(self, available):
        from .ontology import MAX_PROMPT_BYTES
        return available and self.request_bytes <= MAX_PROMPT_BYTES

    def _other_agent_step(self, available):
        from .ontology import MAX_PROMPT_BYTES
        if self._other_agent_fits(available):
            return (f'입력 예산이 {MAX_PROMPT_BYTES:,}바이트인 다른 에이전트로 처음부터 실행'
                    '(완료분을 재사용하지 않고 새로 호출, 원문과 사전이 해당 제공자에게 전달됨)')
        return None

    def approval_warning(self, agent_name, other_agent, approval_revision):
        """This refusal was computed for approving the generated dictionary unchanged at approval_revision."""
        from .ontology import MAX_PROMPT_BYTES
        steps = []
        if self._other_agent_fits(other_agent):
            steps.append(f'입력 예산이 {MAX_PROMPT_BYTES:,}바이트인 다른 에이전트로 추출(원문과 사전이 해당 제공자에게 전달됨)')
        # Revising then approving adds two to approval_revision for an item that is already approved, one for a pending
        # one; the figure covers the larger growth, so it is exact in the first case and sufficient in the second.
        needed = self.excess + len(str(approval_revision + 2)) - len(str(approval_revision))
        if needed <= self.definition_bytes - EMPTY_DEFINITION_BYTES:
            steps.append(f'사전을 수정해 전송 정의 {self.definition_bytes:,}바이트 중 약 {needed:,}바이트 이상을 줄인 뒤 승인(뺀 정의는 추출에 쓸 수 없음)')
        steps.append('문서를 나누어 각각 사전 생성·승인(이 사전은 재사용되지 않음, 한 부분 밖에만 있는 용어는 그 부분에 쓸 수 없음, 부분 간 ID는 사람이 맞춰야 함)')
        others = (f'입력 예산은 에이전트에 따라 다르므로(앱 정책 최대 {MAX_PROMPT_BYTES:,}바이트) 다른 에이전트로는 거부되지 않을 수 있습니다.'
                  if self.request_bytes <= MAX_PROMPT_BYTES else
                  f'어느 에이전트의 입력 예산도 앱 정책 {MAX_PROMPT_BYTES:,}바이트를 넘지 않으므로 다른 에이전트로도 거부됩니다.')
        # Any later unedited approval has a revision at least this one, so request_bytes is a lower bound.
        return (f'추출 입력 한도 경고: 이 사전을 수정하지 않고 이 실행과 같은 에이전트({agent_name})로 추출하면 '
                f'전체 요청이 {self.request_bytes:,}바이트 이상으로 입력 예산 {self.limit:,}바이트를 넘어 첫 호출 전에 거부됩니다. {others} '
                f'다음 단계: {" 또는 ".join(steps)}.')

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
    """Keep semantic values literal; only repeated object field names are compressed."""
    if set(definitions) != set(COLUMNS): raise ValueError('사전 문맥 종류 오류')
    rows: dict[str, list] = {}
    for kind, columns in COLUMNS.items():
        rows[kind] = []
        for row in definitions[kind]:
            if set(row) != set(columns): raise ValueError('사전 문맥 필드 오류')
            rows[kind].append([copy.deepcopy(row[c]) for c in columns])
    result = {'dictionary_wire_version': 2, 'columns': copy.deepcopy(COLUMNS),
              'rows': rows, 'sha256': digest(definitions)}
    if decode(result) != definitions: raise ValueError('사전 문맥 무손실 복원 불일치')
    return result


def decode(value):
    from .ontology import DICTIONARY_TOTAL_LIMITS
    version = value.get('dictionary_wire_version') if isinstance(value, dict) else None
    fields = {'dictionary_wire_version', 'columns', 'rows', 'sha256'} | ({'strings'} if version == 1 else set())
    if (not isinstance(value, dict) or type(version) is not int or version not in (1, 2) or set(value) != fields
            or value['columns'] != COLUMNS or not isinstance(value['rows'], dict) or set(value['rows']) != set(COLUMNS)):
        raise ValueError('사전 문맥 표 형식 오류')
    if len(canonical(value).encode()) > MAX_DECODE_BYTES:
        raise ValueError('사전 문맥 표 크기 초과')
    table = value.get('strings'); used_bytes = 0
    if version == 1:
        if not isinstance(table, list) or any(not isinstance(s, str) for s in table):
            raise ValueError('사전 문맥 문자열 표 오류')
        if len(table) > MAX_DECODE_BYTES or sum(len(s.encode()) for s in table) > MAX_DECODE_BYTES:
            raise ValueError('사전 문맥 복원 크기 초과')
    def expand(item):
        nonlocal used_bytes
        if version == 1:
            assert isinstance(table, list)
            if type(item) is not int or not 0 <= item < len(table): raise ValueError('사전 문맥 참조 오류')
            result = table[item]
        else:
            if not isinstance(item, str): raise ValueError('사전 문맥 리터럴 문자열 오류')
            result = item
        used_bytes += len(result.encode())
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
        diagnostics.update(status='within_limit', encoding='dictionary-wire-v2',
                           transmitted_definition_bytes=encoded_size, prompt_bytes=size)
        return prompt
    if min(raw_size, size) > prompt_limit:
        raise RequestTooLarge(min(raw_size, size), prompt_limit, encoded_size if size <= raw_size else len(serialize(selected).encode()))
    # context_limit only selects the form; it never refuses. The smaller prompt necessarily fits prompt_limit here.
    wire = size <= raw_size
    diagnostics.update(status='over_context_limit', encoding='dictionary-wire-v2' if wire else 'plain',
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


ADMISSION_RECORD = 'extraction-admission.json'


def worst_refusal(cli, agent, instructions, payloads, context_limit):
    """Among (key, payload) pairs, the one admission refuses with the largest excess, as (key, RequestTooLarge), or None."""
    refused = [(key, exc) for key, payload in payloads
               if (exc := refusal(cli, agent, instructions, payload, context_limit)) is not None]
    return max(refused, key=lambda pair: pair[1].excess) if refused else None


def chunk_payload(doc, offset, chunk):
    return {'filename': doc['filename'], 'version': doc['version'], 'offset': offset, 'text': chunk}


def pinned_dictionary(pin):
    """The approved-dictionary part of every extraction chunk request; quotes are not guidance."""
    from .ontology import dictionary_context
    return {'approved_dictionary': pin | {'payload': dictionary_context(pin['payload'])}}


def chunk_requests(doc, pin, raw_chunks):
    """Extraction chunk requests built by the same two functions Service.extract dispatches with."""
    pinned = pinned_dictionary(pin)
    return [(raw, chunk_payload(doc, *raw[1]) | pinned) for raw in raw_chunks]


def extraction_pin(item_id, revision, payload, contract):
    """The approved-dictionary pin store.create_run stores on an extraction run; the caller passes its contract version."""
    return {'id': item_id, 'revision': revision, 'payload': payload, 'sha256': digest(payload), 'extraction_contract': contract}


def _largest_chunk(doc, raw_chunks):
    """The (index, (offset, text)) whose serialized chunk_payload fields are longest in UTF-8 bytes, first on ties.
    In build, both request forms of a chunk are a form-dependent constant plus these bytes (the pin, the prompt and
    the table encoding do not depend on the chunk), so whether a chunk is refused and its request and definition
    sizes are monotone in this length: this chunk is refused exactly when any chunk is, with the largest excess.
    This is a property of build and chunk_payload, not checked here. A test compares this choice with worst_refusal over all
    chunks on fixed documents where it differs from the first chunk and from ranking by characters, by \\u-escaped JSON
    or by text bytes without the offset; it does not cover every input."""
    return max(raw_chunks, key=lambda raw: len(json.dumps(chunk_payload(doc, *raw[1]), ensure_ascii=False, separators=(',', ':')).encode()))


def current_admission(cli, agent, other_agent, doc, item, context_limit):
    """The warning extraction admission gives, before its first call, for approving this current dictionary item unchanged
    and extracting with `agent` under the current policy; None when it fits. Writes no file and calls no model.
    An approved item is pinned at its revision, any other at revision + 1 (approving adds one)."""
    from types import SimpleNamespace

    from .agents import PROVIDERS
    from .documents import chunks
    from .ontology import EXTRACTION_CONTRACT_VERSION, extraction_prompts
    if agent == 'ollama':  # Ollama's options are fixed to the defaults, so its budget does not depend on the job.
        from .ollama import input_byte_limit
        cli = SimpleNamespace(prompt_byte_limit=lambda _agent: input_byte_limit())
    revision = item['revision'] if item['status'] == 'approved' else item['revision'] + 1
    pin = extraction_pin(item['id'], revision, item['payload'], EXTRACTION_CONTRACT_VERSION)
    largest = _largest_chunk(doc, list(enumerate(chunks(doc['text']), 1)))
    worst = worst_refusal(cli, agent, extraction_prompts(EXTRACTION_CONTRACT_VERSION)[0], chunk_requests(doc, pin, [largest]), context_limit)
    return worst[1].approval_warning(PROVIDERS[agent], other_agent, revision) if worst else None


def document_admission(service, doc, item):
    """The document API's extraction admission panel: recomputed for a non-rejected dictionary of the current version,
    otherwise the generation record through read_admission."""
    if item['version'] != doc['current_version'] or item['status'] == 'rejected':
        return read_admission(service.root, item)
    from .ontology import MAX_DICTIONARY_CONTEXT_BYTES
    try:
        agent = service.store.run(item['run_id'])['agent']
        context_limit = validate_context_bytes(service.store.settings().get('dictionary_context_bytes', MAX_DICTIONARY_CONTEXT_BYTES))
        warning = current_admission(service.cli, agent, service.other_agent_ready(agent), doc, item, context_limit)
    except Exception:
        return {'available': False, 'recomputed': True}
    return {'available': True, 'warning': warning, 'recomputed': True}


def _canary_dictionaries():
    """Fixed dictionaries: one whose plain form is the smaller request, one whose table form is."""
    kind = 'T' + 't' * 119
    plain = {'entities': [{'id': 'p-entity', 'name': '표본', 'type': 'Pt', 'quote': '표본 인용'}],
             'aliases': [{'alias': '별칭', 'entity': 'p-entity', 'quote': '별칭 인용', 'judged_alias': '별칭',
                          'judged_entity': 'p-entity', 'judged_name': '표본'}],
             'relations': [{'id': 'p-rel', 'description': '관계', 'subject_types': ['Pt'], 'object_types': ['Pt'], 'quote': '관계 인용'}]}
    table = {'entities': [{'id': f't-entity-{i:02}', 'name': f'표본 {i:02}', 'type': kind, 'quote': f'표본 인용 {i:02}'} for i in range(40)],
             'aliases': [{'alias': f't-alias-{i:02}', 'entity': f't-entity-{i:02}', 'quote': f'별칭 인용 {i:02}', 'judged_alias': f't-alias-{i:02}',
                          'judged_entity': f't-entity-{i:02}', 'judged_name': f'표본 {i:02}'} for i in range(10)],
             'relations': [{'id': f't-rel-{i}', 'description': '관계', 'subject_types': [kind, kind], 'object_types': [kind], 'quote': f'관계 인용 {i}'}
                           for i in range(5)]}
    return plain, table


@functools.cache
def _admission_canary():
    """Refusal sizes (regime, chunk index, request bytes, definition bytes) of a fixed document of more than one chunk, per dictionary above."""
    from .documents import chunks
    from .ontology import EXTRACTION_CONTRACT_VERSION, MAX_DICTIONARY_CONTEXT_BYTES

    class Budget:  # a budget no request fits, so every chunk is refused and reports its size
        prompt_byte_limit = staticmethod(lambda agent: 1)

    doc = {'filename': 'canary.md', 'version': 1, 'text': ''.join(f'문장 {i:04} sentence. ' for i in range(700))}
    sizes = []
    for regime, dictionary in zip(('plain', 'table'), _canary_dictionaries(), strict=True):
        pin = {'id': 'c' * 32, 'revision': 2, 'payload': dictionary, 'sha256': digest(dictionary),
               'extraction_contract': EXTRACTION_CONTRACT_VERSION}
        for (index, _), payload in chunk_requests(doc, pin, list(enumerate(chunks(doc['text']), 1))):
            exc = refusal(Budget(), 'canary', 'canary instructions\n', payload, MAX_DICTIONARY_CONTEXT_BYTES)
            sizes.append([regime, index, exc.request_bytes, exc.definition_bytes])
    return sizes


def admission_policy():
    """Digest of PROTOCOL, the current extraction prompt text, the extraction contract version, MAX_PROMPT_BYTES,
    the Ollama input budget, and the canary: the sizes of the refusals chunk_requests and build report for a fixed
    document and two fixed dictionaries (one per smaller-form regime), which move with the chunk size of
    documents.chunks, NOT_GUIDANCE, the keys chunk_payload adds, and build, encode and serialization.
    The canary builds its own pin, so it does not follow the pin keys store.create_run builds.
    A change that leaves those canary sizes unchanged is not detected, and the document, the dictionary and the agent are not in it."""
    from .ollama import input_byte_limit
    from .ontology import (
        EXTRACTION_CONTRACT_VERSION,
        MAX_PROMPT_BYTES,
        extraction_prompts,
    )
    return digest([PROTOCOL, extraction_prompts(EXTRACTION_CONTRACT_VERSION)[0], EXTRACTION_CONTRACT_VERSION,
                   MAX_PROMPT_BYTES, input_byte_limit(), _admission_canary()])


def read_admission(root, item):
    """None when the item's generation run kept no record, or the dictionary or admission_policy() differs from the record's; else whether it was readable and its warning.
    A hidden record is not recomputed here; the document API reads records only for rejected items and items of earlier
    versions, and recomputes the others with current_admission."""
    from .usage import safe_file
    path = root / 'runs' / item['run_id'] / ADMISSION_RECORD
    try:
        path.stat()
    except FileNotFoundError:
        return None
    except OSError:
        return {'available': False}
    try:
        value = json.loads(safe_file(root, f'runs/{item["run_id"]}/{ADMISSION_RECORD}', 64 * 1024) or b'')
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'run_id', 'dictionary_sha256', 'admission_policy', 'warning'}
                or value['schema_version'] != 1 or value['run_id'] != item['run_id']
                or not isinstance(value['dictionary_sha256'], str) or not (value['warning'] is None or isinstance(value['warning'], str))):
            raise ValueError('admission record shape')
    except (OSError, ValueError, TypeError):
        return {'available': False}
    try:
        policy = admission_policy()
    except Exception:
        return None  # A policy that cannot be computed cannot vouch for the record, so it is not shown.
    if value['dictionary_sha256'] != digest(item['payload']) or value['admission_policy'] != policy:
        return None  # Edited dictionary or changed extraction policy: not recomputed, so not shown.
    return {'available': True, 'warning': value['warning']}
