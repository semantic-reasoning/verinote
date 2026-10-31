"""Separate versioned CLI experiment contract; never an HTTP execution option."""
import copy
import json

from .extraction_transport import ExtractionSpec

MAX_OPTIONS_BYTES = 16384


def extraction_options(value, *, registered=True, agent='claude', stage='extract', model=None):
    keys = {'schema_version', 'concurrency', 'transport'}
    if registered: keys.add('document_generation')
    if (agent != 'claude' or stage != 'extract' or not isinstance(value, dict) or set(value) != keys
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['concurrency']) is not int or value['concurrency'] not in (1, 2)
            or (registered and (type(value['document_generation']) is not int or value['document_generation'] < 1))):
        raise ValueError('추출 실험 실행 계약 오류')
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > MAX_OPTIONS_BYTES:
        raise ValueError('추출 실험 실행 계약 크기 제한')
    spec = ExtractionSpec.from_dict(value['transport'])
    if spec.capacity != value['concurrency'] or (model is not None and spec.model != model):
        raise ValueError('추출 실험 모델·동시성 계약 불일치')
    return copy.deepcopy(value) | {'transport': spec.to_dict()}


def experiment_request(spec):
    return extraction_options({'schema_version': 1, 'concurrency': spec.capacity, 'transport': spec.to_dict()},
                              registered=False, model=spec.model)
