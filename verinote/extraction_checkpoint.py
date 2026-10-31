"""Persist validated extraction prefixes without publishing partial candidates."""
import copy
import json

from .documents import chunks
from .ontology import (
    dictionary_fact,
    extraction_prompts,
    extraction_quote_issues,
)
from .store import canonical, digest
from .usage import atomic_json, safe_file

PIPELINE = 'approved-extraction-v1'
CHUNK_TIMEOUT = 360
CHECKPOINT_LIMIT = 18 * 1024 * 1024


def reuse_contract(contract):
    value = copy.deepcopy(contract)
    if isinstance(value, dict):
        # Deadlines do not change already validated extraction content.
        value.pop('chunk_timeout', None)
        if isinstance(value.get('execution_options'), dict):
            value['execution_options'].pop('timeouts', None)
    return canonical(value)


class ExtractionCheckpoint:
    def __init__(self, store, root, run, doc):
        self.root, self.run = root, run
        self.folder = root / 'runs' / run['id']
        self.folder.mkdir(parents=True, exist_ok=True)
        self.slices = list(chunks(doc['text']))
        self.outputs: list[dict] = []
        self.warnings: list[str] = []
        pin = run['ontology']
        prompt, repair_prompt = extraction_prompts(pin['extraction_contract'])
        self.contract = {
            'pipeline': PIPELINE, 'prompt': digest(prompt.encode()),
            'repair_prompt': digest(repair_prompt.encode()), 'chunk_size': 8000,
            'chunk_timeout': CHUNK_TIMEOUT, 'document_id': run['document_id'],
            'version': run['version'], 'source_sha256': doc['sha256'],
            'text_sha256': digest(doc['text'].encode()), 'agent': run['agent'], 'model': run['model'],
            'dictionary': {key: pin[key] for key in ('id', 'revision', 'sha256', 'extraction_contract')},
            'dictionary_payload_sha256': digest(pin['payload']),
        }
        if run['agent'] in ('openrouter', 'ollama'):
            from .transports import job_options
            self.contract['execution_options'] = job_options(run.get('execution_options'))
        if run.get('extraction_options') is not None:
            from .extraction_options import (
                extraction_options,
                validate_extraction_source,
            )
            self.contract['extraction_options'] = extraction_options(run['extraction_options'], agent=run['agent'], stage=run['stage'], model=run['model'])
            validate_extraction_source(self.contract['extraction_options'], doc['text'])
        previous, skipped = None, 0
        for prior in store.extraction_retries(run):
            raw = safe_file(root, f'runs/{prior["id"]}/extraction-checkpoint.json', CHECKPOINT_LIMIT)
            try:
                value = json.loads(raw) if raw else None
                if (not isinstance(value, dict)
                        or set(value) != {'schema_version', 'run_id', 'contract', 'outputs'}
                        or type(value['schema_version']) is not int or value['schema_version'] != 1
                        or value['run_id'] != prior['id']
                        or reuse_contract(value['contract']) != reuse_contract(self.contract)
                        or canonical(prior['ontology']) != canonical(pin)
                        or not isinstance(value['outputs'], list)
                        or not 1 <= len(value['outputs']) <= len(self.slices)):
                    raise ValueError('checkpoint identity/contract')
                outputs = [self.validated(output, self.slices[i][1])
                           for i, output in enumerate(value['outputs'])]
                if len(outputs) > len(self.outputs):
                    self.outputs, previous = outputs, prior['id']
            except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
                skipped += 1
        if skipped:
            self.warnings.append(f'이전 추출 복구 기록 {skipped}개가 없거나 호환·검증 조건을 충족하지 않아 제외')
        self.resumed_chunks = len(self.outputs)
        resumed_chars = sum(len(chunk) for _, chunk in self.slices[:self.resumed_chunks])
        self.state = {'schema_version': 1, 'pipeline': PIPELINE, 'run_id': run['id'],
                      'calls_started': 0, 'resumed_from': previous, 'resumed_chars': resumed_chars,
                      'completed_chars': resumed_chars, 'total_chars': len(doc['text']), 'status': 'running'}
        self.save()

    def validated(self, output, chunk):
        from .service import validate_page
        if extraction_quote_issues(output, chunk, self.run['ontology']['payload'], require_coverage=True, contract_version=self.run['ontology']['extraction_contract']):
            raise ValueError('checkpoint contains ungrounded quotes')
        page = validate_page(output, chunk)
        facts = [dictionary_fact(fact, chunk, self.run['ontology']['payload']) for fact in output['facts']]
        return page | {'facts': facts, 'coverage': copy.deepcopy(output['coverage']),
                       'dictionary_gaps': copy.deepcopy(output['dictionary_gaps'])}

    def write(self, name, value, limit):
        try:
            if len(json.dumps(value, ensure_ascii=False, indent=2).encode()) > limit:
                raise ValueError('checkpoint size limit')
            atomic_json(self.folder / name, value)
        except (OSError, ValueError):
            warning = '추출 복구 기록 저장 실패: 재시도 시 일부 청크를 다시 처리할 수 있습니다.'
            if warning not in self.warnings:
                self.warnings.append(warning)

    def save(self):
        if self.outputs:
            self.write('extraction-checkpoint.json',
                       {'schema_version': 1, 'run_id': self.run['id'], 'contract': self.contract,
                        'outputs': self.outputs}, CHECKPOINT_LIMIT)
        self.write('extraction-progress.json', self.state, 8192)

    def begin_call(self):
        self.state['calls_started'] += 1
        self.write('extraction-progress.json', self.state, 8192)

    def accept(self, output, chunk, *, before_accept=None):
        validated = self.validated(output, chunk)
        if before_accept is not None: before_accept()
        self.outputs.append(validated)
        self.state['completed_chars'] += len(chunk)
        self.save()

    def finish(self, status):
        self.state['status'] = status
        self.write('extraction-progress.json', self.state, 8192)

    def summary(self):
        parts = [f'검증 완료 {len(self.outputs)}/{len(self.slices)}청크 · 새 호출 {self.state["calls_started"]}회']
        if self.resumed_chunks:
            parts.append(f'{self.resumed_chunks}청크 재사용 · 이전 실행 {self.state["resumed_from"]}')
        return ' · '.join(parts + self.warnings)
