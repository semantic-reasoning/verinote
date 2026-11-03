"""Opt-in generic evidence-link instructions; no fixture, gold or history inputs."""
import json

from ..ontology import extraction_prompts
from ..store import digest

CANDIDATE_ID = 'evidence_links_v1'
TEMPLATE = '''
추가 근거 연결 점검 (위 JSON 형식과 승인 사전의 의미를 그대로 유지):
- 이번 입력의 승인 관계 ID: {relation_ids}
- 원문에서 사실을 고르면서 부정·조건·과거/중단·시점·미등록 어휘 때문에 사실로 표현할 수 없는 관련 근거도 빠짐없이 claims에 보존하세요. 원문에 없는 제약이나 최소 개수를 만들지 마세요.
- 각 claim의 quote가 그 claim에서 설명하는 제약 자체를 직접 포함하는지 점검하세요. 관련 없는 사실 인용이나 text의 설명만으로 근거를 대신하지 마세요. 하나의 연속 인용이 여러 제약을 실제로 포함하면 한 claim에 함께 보존해도 됩니다.
- 승인 관계마다 coverage를 한 행씩 만드세요. 일부 사실을 추출했어도 남은 부정·조건·시점 등의 관련 근거 claim_indices를 extracted 행에 함께 연결하세요. 승인 관계에 관련된 근거가 미등록 개체·타입·기타 사전 제약 때문에 fact가 되지 못해도 그 근거 claim을 해당 관계의 coverage에 연결하세요. 직접 근거가 없는 no_evidence와 근거는 있으나 승인 정의로 의미를 보존할 수 없는 not_representable을 구분하세요.
- 각 dictionary_gaps 항목의 claim_indices는 해당 code의 제약을 직접 뒷받침하는 인용에 연결하세요. missing_entity/missing_relation은 실제 승인 어휘가 없는 경우이고, 이미 승인된 관계에 현재 직접 사실이 없다는 이유만으로 missing_relation을 만들지 마세요. 조건·시점 제약은 실제 의미에 맞는 code로 구분하세요.
- 마지막으로 claims의 관련 원문 근거 → coverage/gap의 claim_indices를 대조하세요. reason/detail의 설명으로 누락된 인용 연결을 대신하지 마세요. 승인 사전 밖의 사실이나 무조건 관계로 바꾸지 말고, 관련 없는 claim 참조를 제거하세요.
'''


def template_hash():
    return digest(TEMPLATE.encode())


def compile_prompt(original):
    """Keep the entire production payload byte-for-byte; inspect relation IDs only."""
    prefix = extraction_prompts(2)[0]
    if not isinstance(original, str) or not original.startswith(prefix):
        raise ValueError('candidate requires production v2 extraction prompt')
    payload_text = original[len(prefix):]
    payload = json.loads(payload_text)
    if not isinstance(payload, dict) or set(payload) != {'filename', 'version', 'offset', 'text', 'approved_dictionary'}:
        raise ValueError('candidate accepts only production extraction fields')
    pin = payload['approved_dictionary']
    if not isinstance(pin, dict) or not isinstance(pin.get('payload'), dict):
        raise ValueError('candidate requires approved dictionary')
    relations = pin['payload'].get('relations')
    if not isinstance(relations, list): raise ValueError('candidate requires approved relations')
    ids = [row.get('id') if isinstance(row, dict) else None for row in relations]
    if any(not isinstance(value, str) or not value for value in ids) or len(set(ids)) != len(ids):
        raise ValueError('candidate relation IDs must be unique strings')
    marker = '입력(JSON):\n'
    return prefix[:-len(marker)] + TEMPLATE.format(relation_ids=json.dumps(ids, ensure_ascii=False)) + marker + payload_text
