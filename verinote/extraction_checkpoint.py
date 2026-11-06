"""Persist validated extraction prefixes; candidates publish only after every chunk has validated."""
import copy
import json

from .documents import chunks
from .ontology import (
    dictionary_fact,
    extraction_prompts,
    extraction_quote_issues,
    quote_label,
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
        no_claims = isinstance(output, dict) and bool(output.get('excluded_claims'))
        if extraction_quote_issues(output, chunk, self.run['ontology']['payload'], require_coverage=True, contract_version=self.run['ontology']['extraction_contract'], allow_no_claims=no_claims):
            raise ValueError('checkpoint contains ungrounded quotes')
        page = validate_page(output, chunk, allow_no_claims=no_claims)
        facts = [dictionary_fact(fact, chunk, self.run['ontology']['payload']) for fact in output['facts']]
        result = page | {'facts': facts, 'coverage': copy.deepcopy(output['coverage']),
                         'dictionary_gaps': copy.deepcopy(output['dictionary_gaps'])}
        if output.get('excluded_facts'):
            result['excluded_facts'] = copy.deepcopy(output['excluded_facts'])
        if output.get('excluded_claims'):
            result['excluded_claims'] = copy.deepcopy(output['excluded_claims'])
        return result

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

    def exclusion_counts(self):
        """(response facts, facts excluded for a dictionary mismatch) over validated chunks, including reused ones."""
        rows = [row for output in self.outputs for row in output.get('excluded_facts', ())]
        return len(rows) + sum(len(output['facts']) for output in self.outputs), sum(row['code'] != 'unverifiable_quote' for row in rows)

    def quote_exclusions(self):
        """(claims, facts, per-chunk locations) excluded after a repair, over validated chunks, including reused ones."""
        claims, facts, places = 0, 0, []
        for number, output in enumerate(self.outputs, 1):
            found = [row['path'] for row in output.get('excluded_claims', ())]
            claims += len(found)
            rows = [row['path'] for row in output.get('excluded_facts', ()) if row['code'] == 'unverifiable_quote']
            facts += len(rows)
            labels = [quote_label(path) for path in found + rows]
            if labels:
                places.append(f'청크 {number}: ' + ', '.join(labels[:5]) + (f' 외 {len(labels) - 5}개' if len(labels) > 5 else ''))
        return claims, facts, places

    def quote_exclusion_notice(self):
        claims, facts, places = self.quote_exclusions()
        if not claims and not facts:
            return ''
        emitted_claims = claims + sum(len(output['claims']) for output in self.outputs)
        shown = '; '.join(places[:5]) + (f' 외 {len(places) - 5}개 청크' if len(places) > 5 else '')
        return (f'근거 불가로 저장하지 않음: 위키 주장 {emitted_claims}개 중 {claims}개·응답 관계 {self.exclusion_counts()[0]}개 중 {facts}개 '
                f'({shown} · 번호는 보정을 요청한 응답 기준). 보정 뒤에도 인용이 원문의 한 위치로 확인되지 않았습니다.')

    def nothing_to_publish(self, claims, facts):
        """Why a complete extraction cannot publish, or ''. No facts alone does not stop it unless a dictionary mismatch removed some."""
        emitted, mismatched = self.exclusion_counts()
        refused_claims, refused_facts, _ = self.quote_exclusions()
        if not claims and refused_claims:
            reason = (f'위키 주장 {refused_claims}개가 모두 원문 인용을 확인하지 못해 저장할 주장이 없습니다. 이번 실행의 위키 주장과 후보는 저장하지 않았고 이 사전 revision은 다시 추출할 수 있습니다. '
                      '모든 청크의 복구 기록이 남아 있으면 같은 에이전트·모델로 다시 실행해도 새 호출 없이 같은 결과로 끝납니다. '
                      '사전을 수정·재승인하면 모든 청크를 새로 호출하며 비용이 다시 들고 같은 결과가 나올 수 있습니다. '
                      '다른 모델로 바꿔도 모든 청크를 새로 호출하지만 그 모델의 복구 기록이 있으면 검증된 청크를 재사용합니다.')
            return ' · '.join(filter(None, (reason, self.quote_exclusion_notice(), self.exclusion_notice())))
        if not facts and mismatched:
            what = (f'응답 관계 {mismatched}개가 모두 승인 사전의 표기·타입과 일치하지 않아' if not refused_facts else
                    f'응답 관계 {emitted}개 중 {mismatched}개는 승인 사전의 표기·타입과 일치하지 않고 {refused_facts}개는 원문 인용을 확인하지 못해')
            reason = (f'{what} 저장할 후보가 없습니다. 이번 실행의 위키 주장과 후보는 저장하지 않았고 이 사전 revision은 다시 추출할 수 있습니다. '
                      '사전을 수정·재승인하면 모든 청크를 새로 호출합니다. 다른 모델로 바꿀 수도 있지만 그 모델의 복구 기록이 있으면 검증된 청크를 재사용하므로 같은 실패로 끝날 수 있습니다.')
            return ' · '.join(filter(None, (reason, self.quote_exclusion_notice())))
        return ''

    def exclusion_notice(self):
        emitted, excluded = self.exclusion_counts()
        if not excluded:
            return ''
        return f'응답 관계 {emitted}개 중 {excluded}개는 승인 사전의 표기·타입과 일치하지 않아 저장하지 않음 · 되살리려면 사전 수정·재승인 후 재추출'

    def summary(self):
        parts = [f'검증 완료 {len(self.outputs)}/{len(self.slices)}청크 · 새 호출 {self.state["calls_started"]}회']
        if self.resumed_chunks:
            parts.append(f'{self.resumed_chunks}청크 재사용 · 이전 실행 {self.state["resumed_from"]}')
        return ' · '.join(parts + self.warnings)
