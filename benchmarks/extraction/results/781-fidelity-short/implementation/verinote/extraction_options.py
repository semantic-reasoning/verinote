"""Separate versioned CLI experiment contract; never an HTTP execution option."""
import copy
import json
from dataclasses import replace

from .documents import chunks
from .extraction_transport import ExtractionSpec

MAX_OPTIONS_BYTES = 16384
CHUNK_THRESHOLD_POLICY = 'chunk_threshold_v1'


def threshold_concurrency(count):
    if type(count) is not int or count < 1: raise ValueError('원문 청크 수 오류')
    return 1 if count <= 3 else 2


def extraction_options(value, *, registered=True, agent='claude', stage='extract', model=None):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2):
        raise ValueError('추출 실험 실행 계약 오류')
    policy = value['schema_version'] == 2
    keys = {'schema_version', 'transport'}
    if policy:
        keys.add('policy')
        if registered: keys.update(('original_chunk_count', 'concurrency'))
    else: keys.add('concurrency')
    if registered: keys.add('document_generation')
    if (agent != 'claude' or stage != 'extract' or not isinstance(value, dict) or set(value) != keys
            or (policy and value['policy'] != CHUNK_THRESHOLD_POLICY)
            or ((registered or not policy) and (type(value['concurrency']) is not int or value['concurrency'] not in (1, 2)))
            or (registered and (type(value['document_generation']) is not int or value['document_generation'] < 1))):
        raise ValueError('추출 실험 실행 계약 오류')
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OPTIONS_BYTES:
        raise ValueError('추출 실험 실행 계약 크기 제한')
    spec = ExtractionSpec.from_dict(value['transport'])
    if ((registered or not policy) and spec.capacity != value['concurrency']) or (model is not None and spec.model != model):
        raise ValueError('추출 실험 모델·동시성 계약 불일치')
    if policy and registered and value['concurrency'] != threshold_concurrency(value['original_chunk_count']):
        raise ValueError('추출 정책 결정 불일치')
    return copy.deepcopy(value) | {'transport': spec.to_dict()}


def experiment_request(spec):
    return extraction_options({'schema_version': 1, 'concurrency': spec.capacity, 'transport': spec.to_dict()},
                              registered=False, model=spec.model)


def policy_request(spec, policy):
    return extraction_options({'schema_version': 2, 'policy': policy, 'transport': spec.to_dict()},
                              registered=False, model=spec.model)


def bind_extraction_options(value, source, generation):
    """Called under Store's registration transaction; never trust caller counts."""
    result = copy.deepcopy(value)
    if result['schema_version'] == 2:
        count = sum(1 for _ in chunks(source))
        concurrency = threshold_concurrency(count)
        spec = replace(ExtractionSpec.from_dict(result['transport']), capacity=concurrency)
        result.update(original_chunk_count=count, concurrency=concurrency, transport=spec.to_dict())
    result['document_generation'] = generation
    return extraction_options(result)


def validate_extraction_source(value, source):
    if value['schema_version'] == 2 and value['original_chunk_count'] != sum(1 for _ in chunks(source)):
        raise ValueError('등록된 원문 청크 수 불일치')
